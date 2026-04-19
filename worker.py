import os
import subprocess
import tempfile
import base64
import json
import shutil
import runpod
from git import Repo

# --- Authentication Helpers ---
def parse_auth_env(env_name):
    val = os.environ.get(env_name, "")
    auth_map = {}
    if not val:
        return auth_map
    for line in val.strip().split('\n'):
        if ':' in line:
            parts = line.split(':', 1)
            if len(parts) == 2:
                user, token = parts
                auth_map[user.strip()] = token.strip()
    return auth_map

# --- Logging Helpers ---
def log(message, level="INFO", job_id="SYSTEM"):
    print(f"[{level}] {job_id} | {message}", flush=True)

def run_command_streaming(cmd, env=None, job_id="BUILD"):
    log(f"Executing: {' '.join(cmd)}", job_id=job_id)
    process = subprocess.Popen(
        cmd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )
    
    output_lines = []
    for line in iter(process.stdout.readline, ""):
        # We print directly to stdout for real-time visibility in RunPod logs
        print(f"  {line}", end="", flush=True)
        output_lines.append(line)
    
    process.stdout.close()
    return_code = process.wait()
    return return_code, "".join(output_lines)

# --- Hardware Detection Helpers ---
def get_memory_info():
    try:
        with open('/sys/fs/cgroup/memory.max', 'r') as f:
            val = f.read().strip()
            if val != 'max':
                return int(val) / (1024 ** 3)
    except Exception:
        pass
    try:
        pages = os.sysconf('SC_PHYS_PAGES')
        page_size = os.sysconf('SC_PAGE_SIZE')
        return (pages * page_size) / (1024 ** 3)
    except Exception:
        return 4.0

# --- The Main Handler ---
def handler(job):
    job_id = job.get('id', 'unknown')
    job_input = job['input']
    
    log("Job Received.", job_id=job_id)
    
    github_repo = job_input.get('github_repo')
    if not github_repo:
        log("Error: Missing github_repo", "ERROR", job_id)
        return {"error": "Missing 'github_repo' in input."}
    
    branch = job_input.get('branch', 'main')
    dockerfile_path = job_input.get('dockerfile_path', 'Dockerfile')
    build_ctx_path = job_input.get('build_ctx_path', '.')
    github_token = job_input.get('github_access_token')
    
    dockerhub_repo = job_input.get('dockerhub_repo')
    if not dockerhub_repo:
        log("Error: Missing dockerhub_repo", "ERROR", job_id)
        return {"error": "Missing 'dockerhub_repo' in input."}
    
    dockerhub_tag = job_input.get('dockerhub_tag', 'latest')
    dockerhub_token = job_input.get('dockerhub_access_token')
    
    log(f"Target: {dockerhub_repo}:{dockerhub_tag}", job_id=job_id)
    log(f"Source: {github_repo} (branch: {branch})", job_id=job_id)

    gh_auth_map = parse_auth_env('github_pat_auth')
    dh_auth_map = parse_auth_env('dockerhub_pat_auth')
    
    if not github_token:
        repo_owner = github_repo.split('/')[0]
        github_token = gh_auth_map.get(repo_owner)
        if github_token:
            log("GitHub token retrieved from environment secrets.", job_id=job_id)
        
    dh_user = dockerhub_repo.split('/')[0]
    if not dockerhub_token:
        dockerhub_token = dh_auth_map.get(dh_user)
        if dockerhub_token:
            log("DockerHub token retrieved from environment secrets.", job_id=job_id)

    full_image_tag = f"{dockerhub_repo}:{dockerhub_tag}"
    
    # ---------------------------------------------------------
    # WORKSPACE PREPARATION (The "Warp Drive" Optimization)
    # ---------------------------------------------------------
    actual_ram_gb = get_memory_info()
    shield_workspace = "/__runpod_shield__/workspace"
    
    # If we have 30GB+ RAM, move the entire build workspace to RAM Disk (/dev/shm)
    # This makes Kaniko's snapshotting and extraction phases near-instant.
    if actual_ram_gb >= 30.0:
        log(f"High RAM detected ({actual_ram_gb:.1f}GB). Activating Warp Drive (RAM Disk)...", job_id=job_id)
        shield_workspace = "/dev/shm/runpod_build"
    else:
        log(f"Standard RAM detected ({actual_ram_gb:.1f}GB). Using persistent storage.", job_id=job_id)

    os.makedirs(shield_workspace, exist_ok=True)
    
    with tempfile.TemporaryDirectory(dir=shield_workspace) as tmp_dir:
        repo_dir = os.path.join(tmp_dir, "repo")
        clone_url = f"https://{github_token}@github.com/{github_repo}.git" if github_token else f"https://github.com/{github_repo}.git"
            
        log(f"Cloning repository...", job_id=job_id)
        try:
            Repo.clone_from(clone_url, repo_dir, branch=branch, depth=1)
            log("Clone successful.", job_id=job_id)
        except Exception as e:
            log(f"Clone failed: {str(e)}", "ERROR", job_id)
            return {"error": f"Failed to clone repository: {str(e)}"}
        
        absolute_ctx_path = os.path.abspath(os.path.join(repo_dir, build_ctx_path))
        absolute_dockerfile_path = os.path.abspath(os.path.join(repo_dir, dockerfile_path))

        # ---------------------------------------------------------
        # ENVIRONMENT ROUTING
        # ---------------------------------------------------------
        
        if os.path.exists("/kaniko-engine/executor"):
            log("Production environment (Shielded Kaniko) detected.", job_id=job_id)
            
            docker_config_dir = os.path.join(tmp_dir, ".docker")
            os.makedirs(docker_config_dir, exist_ok=True)
            
            if dockerhub_token:
                log("Configuring DockerHub credentials...", job_id=job_id)
                auth_string = f"{dh_user}:{dockerhub_token}"
                encoded_auth = base64.b64encode(auth_string.encode('utf-8')).decode('utf-8')
                config_data = {"auths": {"https://index.docker.io/v1/": {"auth": encoded_auth}}}
                with open(os.path.join(docker_config_dir, "config.json"), "w") as f:
                    json.dump(config_data, f)
            
            cpu_count = 16 # Optimized for 16/32 hardware
            
            # Persistent Cache Directory (if volume is mounted to /__runpod_shield__)
            cache_dir = "/__runpod_shield__/cache"
            os.makedirs(cache_dir, exist_ok=True)
            
            kaniko_cmd = [
                "/kaniko-engine/executor",
                "--context", absolute_ctx_path,
                "--dockerfile", absolute_dockerfile_path,
                "--destination", full_image_tag,
                "--use-new-run",              
                "--compressed-caching=false", 
                "--ignore-path=/__runpod_shield__", 
                "--ignore-path=/kaniko-engine", 
                "--ignore-path=/dev/shm",
                "--build-arg", f"MAKEFLAGS=-j{cpu_count}",
                "--build-arg", f"NPROC={cpu_count}",
                "--build-arg", f"MAX_JOBS={cpu_count}",
                "--snapshot-mode=redo",
                "--cache=true",
                f"--cache-dir={cache_dir}",
                "--cache-ttl=24h"
            ]
            
            env = os.environ.copy()
            env["DOCKER_CONFIG"] = docker_config_dir
            env["GOMAXPROCS"] = str(cpu_count)
            env["GOGC"] = "1000"
            env["GOMEMLIMIT"] = "28000MiB"
            # Optimization: Force HF Transfer if not explicitly disabled
            env["HF_HUB_ENABLE_HF_TRANSFER"] = "1" 

            log("Starting Optimized Kaniko build...", job_id=job_id)
            rc, build_log = run_command_streaming(kaniko_cmd, env=env, job_id=job_id)
            
            if rc != 0:
                log("Kaniko execution failed.", "ERROR", job_id)
                return {"success": False, "error": "Kaniko build/push failed", "build_log": build_log}
                
            log("Build and push completed successfully.", job_id=job_id)
            return {"success": True, "message": f"Successfully built and pushed {full_image_tag} via Kaniko", "build_log": build_log}

        else:
            log("Local environment detected. Falling back to host Docker daemon...", job_id=job_id)
            if not shutil.which("docker"):
                log("Docker not found in path.", "ERROR", job_id)
                return {"success": False, "error": "Neither Kaniko nor Docker was found. Cannot build image."}

            if dockerhub_token:
                log("Logging into DockerHub locally...", job_id=job_id)
                login_cmd = ["docker", "login", "-u", dh_user, "--password-stdin"]
                login_proc = subprocess.run(login_cmd, input=dockerhub_token, capture_output=True, text=True)
                if login_proc.returncode != 0:
                    log("Local Docker login failed.", "ERROR", job_id)
                    return {"success": False, "error": "Local Docker login failed", "stderr": login_proc.stderr}

            log(f"Building {full_image_tag} locally...", job_id=job_id)
            build_cmd = ["docker", "build", "-t", full_image_tag, "-f", absolute_dockerfile_path, absolute_ctx_path]
            rc, build_log = run_command_streaming(build_cmd, job_id=job_id)
            if rc != 0:
                log("Local build failed.", "ERROR", job_id)
                return {"success": False, "error": "Local Docker build failed", "build_log": build_log}

            log(f"Pushing {full_image_tag} locally...", job_id=job_id)
            push_cmd = ["docker", "push", full_image_tag]
            rc, push_log = run_command_streaming(push_cmd, job_id=job_id)
            if rc != 0:
                log("Local push failed.", "ERROR", job_id)
                return {"success": False, "error": "Local Docker push failed", "build_log": push_log}

            log("Local build and push complete.", job_id=job_id)
            return {"success": True, "message": f"Successfully built and pushed {full_image_tag} via Local Docker", "build_log": build_log}

if __name__ == "__main__":
    log("RunPod Auto-Builder Worker Started.", level="INFO")
    runpod.serverless.start({"handler": handler})