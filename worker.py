import os
import subprocess
import tempfile
import base64
import json
import shutil
import time
import threading
import runpod
from git import Repo

# ---------------------------------------------------------------------------
# HARDWARE PROFILING
# ---------------------------------------------------------------------------

def get_container_memory_gb() -> float:
    """
    Read the container's true memory limit from cgroups.
    Falls back through cgroups v1, then physical RAM, then a safe default.
    """
    # cgroups v2
    try:
        with open("/sys/fs/cgroup/memory.max") as f:
            val = f.read().strip()
            if val != "max":
                return int(val) / (1024 ** 3)
    except Exception:
        pass
    # cgroups v1
    try:
        with open("/sys/fs/cgroup/memory/memory.limit_in_bytes") as f:
            val = int(f.read().strip())
            if val < 9_000_000_000_000_000_000:
                return val / (1024 ** 3)
    except Exception:
        pass
    # Physical RAM fallback
    try:
        return (os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")) / (1024 ** 3)
    except Exception:
        return 4.0


def build_hardware_profile(cpu_count: int, ram_gb: float) -> dict:
    """
    Returns safe build parameters for the detected hardware.
    Assumes ~1.5 GB RAM per heavy compiler thread (GCC / Rust).
    """
    max_ram_threads  = max(1, int(ram_gb / 1.5))
    threads          = min(cpu_count, max_ram_threads)
    snapshot_mode    = "redo" if ram_gb >= 15.0 else "time"
    tier             = "Performance" if ram_gb >= 15.0 else "Budget"
    return {"threads": threads, "snapshot_mode": snapshot_mode, "tier": tier}


# ---------------------------------------------------------------------------
# AUTHENTICATION HELPERS
# ---------------------------------------------------------------------------

def parse_pat_env(env_name: str) -> dict:
    """
    Reads a multi-line env var of the form:
        username1:token1
        username2:token2
    Returns a dict mapping username -> token.
    """
    result = {}
    for line in os.environ.get(env_name, "").strip().splitlines():
        if ":" in line:
            user, _, token = line.partition(":")
            result[user.strip()] = token.strip()
    return result


# ---------------------------------------------------------------------------
# KANIKO BUILD (Production via chroot Jail)
# ---------------------------------------------------------------------------

def run_kaniko_jailed(
    repo_dir:        str,
    build_ctx_path:  str,
    dockerfile_path: str,
    destination:     str,
    docker_config:   str,
    profile:         dict,
) -> subprocess.CompletedProcess:
    """
    Executes Kaniko inside a chroot jail. This physically isolates Kaniko 
    from the Python environment, preventing file locks and bad snapshots.
    """
    jail_root = "/kaniko-jail"
    jail_workspace = f"{jail_root}/workspace"

    # 1. Clear previous workspace and copy the new repo into the jail
    shutil.rmtree(jail_workspace, ignore_errors=True)
    shutil.copytree(repo_dir, jail_workspace)

    # 2. Copy Docker auth into the jail so Kaniko can authenticate
    jail_docker_cfg = f"{jail_root}/kaniko/.docker"
    os.makedirs(jail_docker_cfg, exist_ok=True)
    shutil.copy(os.path.join(docker_config, "config.json"), os.path.join(jail_docker_cfg, "config.json"))

    # 3. Resolve paths relative to the INSIDE of the chroot jail.
    # The `repo_dir` was copied to `/workspace` inside the jail.
    chroot_ctx        = os.path.normpath(f"/workspace/{build_ctx_path}")
    chroot_dockerfile = os.path.normpath(f"/workspace/{dockerfile_path}")

    # 4. Execute Kaniko inside the chroot jail.
    cmd = [
        "chroot", jail_root,
        "/kaniko/executor",
        "--context",        chroot_ctx,
        "--dockerfile",     chroot_dockerfile,
        "--destination",    destination,
        f"--snapshot-mode={profile['snapshot_mode']}",
        "--compressed-caching=false",
        "--build-arg", f"MAKEFLAGS=-j{profile['threads']}",
        "--build-arg", f"NPROC={profile['threads']}",
        "--build-arg", f"MAX_JOBS={profile['threads']}",
        "--build-arg", f"RAYON_NUM_THREADS={profile['threads']}",
    ]

    env = os.environ.copy()
    env["GOMAXPROCS"] = str(profile["threads"])

    return subprocess.run(cmd, env=env, capture_output=True, text=True)


# ---------------------------------------------------------------------------
# LOCAL DOCKER BUILD (Development fallback)
# ---------------------------------------------------------------------------

def run_local_docker(
    context_path:    str,
    dockerfile_path: str,
    destination:     str,
    dh_user:         str,
    dh_token:        str | None,
) -> dict:
    if dh_token:
        login = subprocess.run(
            ["docker", "login", "-u", dh_user, "--password-stdin"],
            input=dh_token, capture_output=True, text=True
        )
        if login.returncode != 0:
            return {"success": False, "error": "Docker login failed", "stderr": login.stderr}

    build = subprocess.run(
        ["docker", "build", "-t", destination, "-f", dockerfile_path, context_path],
        capture_output=True, text=True
    )
    if build.returncode != 0:
        return {"success": False, "error": "Docker build failed", "stderr": build.stderr}

    push = subprocess.run(["docker", "push", destination], capture_output=True, text=True)
    if push.returncode != 0:
        return {"success": False, "error": "Docker push failed", "stderr": push.stderr}

    return {"success": True, "message": f"Built and pushed {destination} via local Docker", "build_log": build.stdout}


# ---------------------------------------------------------------------------
# HANDLER
# ---------------------------------------------------------------------------

def handler(job: dict) -> dict:
    inp = job["input"]

    # ── Required inputs ──────────────────────────────────────────────────────
    github_repo = inp.get("github_repo")
    if not github_repo:
        return {"error": "Missing 'github_repo' in input."}

    dockerhub_repo = inp.get("dockerhub_repo")
    if not dockerhub_repo:
        return {"error": "Missing 'dockerhub_repo' in input."}

    # ── Optional inputs ───────────────────────────────────────────────────────
    branch          = inp.get("branch", "main")
    dockerfile_path = inp.get("dockerfile_path", "Dockerfile")
    build_ctx_path  = inp.get("build_ctx_path", ".")
    dockerhub_tag   = inp.get("dockerhub_tag", "latest")

    # ── Token resolution (payload → env PAT map) ─────────────────────────────
    gh_pat_map = parse_pat_env("GITHUB_PAT_AUTH")
    dh_pat_map = parse_pat_env("DOCKERHUB_PAT_AUTH")

    github_token = inp.get("github_access_token") or gh_pat_map.get(github_repo.split("/")[0])
    dh_user      = dockerhub_repo.split("/")[0]
    dh_token     = inp.get("dockerhub_access_token") or dh_pat_map.get(dh_user)

    destination = f"{dockerhub_repo}:{dockerhub_tag}"

    # ── Clone repo to a temp workspace ────────────────────────────────────────
    work_root = "/tmp/builder_workspace"
    os.makedirs(work_root, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=work_root) as tmp_dir:
        repo_dir = os.path.join(tmp_dir, "repo")
        clone_url = (
            f"https://{github_token}@github.com/{github_repo}.git"
            if github_token
            else f"https://github.com/{github_repo}.git"
        )

        print(f"[clone] {github_repo} @ {branch}")
        try:
            Repo.clone_from(clone_url, repo_dir, branch=branch)
        except Exception as exc:
            return {"error": f"Clone failed: {exc}"}

        # ── Route: Production (Kaniko chroot) vs Local (Docker) ──────────────
        # We check for the jail root rather than just the binary now
        is_production = os.path.exists("/kaniko-jail")

        if is_production:
            # ── Hardware profiling ────────────────────────────────────────────
            cpu_count = os.cpu_count() or 1
            ram_gb    = get_container_memory_gb()
            profile   = build_hardware_profile(cpu_count, ram_gb)

            print(f"[hw]    vCPUs={cpu_count}  RAM={ram_gb:.1f}GB  "
                  f"tier={profile['tier']}  mode={profile['snapshot_mode']}  "
                  f"threads={profile['threads']}")

            # ── Docker auth config ────────────────────────────────────────────
            docker_cfg_dir = os.path.join(tmp_dir, ".docker")
            os.makedirs(docker_cfg_dir, exist_ok=True)

            if dh_token:
                encoded = base64.b64encode(f"{dh_user}:{dh_token}".encode()).decode()
                cfg = {"auths": {"https://index.docker.io/v1/": {"auth": encoded}}}
                with open(os.path.join(docker_cfg_dir, "config.json"), "w") as f:
                    json.dump(cfg, f)

            # ── Build ─────────────────────────────────────────────────────────
            print(f"[build] {destination} via Kaniko Jail")
            
            # Pass the relative strings, let run_kaniko_jailed map them to the chroot
            result = run_kaniko_jailed(
                repo_dir=repo_dir,
                build_ctx_path=build_ctx_path,
                dockerfile_path=dockerfile_path,
                destination=destination,
                docker_config=docker_cfg_dir,
                profile=profile
            )

            if result.returncode != 0:
                return {
                    "success": False,
                    "error":   "Kaniko build/push failed",
                    "stdout":  result.stdout,
                    "stderr":  result.stderr,
                }

            # Clean up the jail workspace to free up disk space immediately
            shutil.rmtree("/kaniko-jail/workspace", ignore_errors=True)

            return {
                "success":   True,
                "message":   f"Built and pushed {destination} via Kaniko",
                "build_log": result.stdout,
            }

        else:
            # ── Local Docker fallback ─────────────────────────────────────────
            if not shutil.which("docker"):
                return {"success": False, "error": "Neither Kaniko Jail nor Docker found."}

            print("[local] Falling back to host Docker daemon")
            abs_ctx        = os.path.abspath(os.path.join(repo_dir, build_ctx_path))
            abs_dockerfile = os.path.abspath(os.path.join(repo_dir, dockerfile_path))
            return run_local_docker(abs_ctx, abs_dockerfile, destination, dh_user, dh_token)


# ---------------------------------------------------------------------------
# ENTRYPOINT
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("RunPod Docker Auto-Builder Worker v4 (Jailed) — starting.")
    runpod.serverless.start({"handler": handler})