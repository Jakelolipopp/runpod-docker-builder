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

# --- Hardware Detection & Profiling ---
def get_container_memory_gb():
    try:
        with open('/sys/fs/cgroup/memory.max', 'r') as f:
            val = f.read().strip()
            if val != 'max':
                return int(val) / (1024 ** 3)
    except Exception:
        pass
    try:
        with open('/sys/fs/cgroup/memory/memory.limit_in_bytes', 'r') as f:
            val = f.read().strip()
            if int(val) < 9000000000000000000:
                return int(val) / (1024 ** 3)
    except Exception:
        pass
    try:
        pages = os.sysconf('SC_PHYS_PAGES')
        page_size = os.sysconf('SC_PAGE_SIZE')
        return (pages * page_size) / (1024 ** 3)
    except Exception:
        return 4.0 

def calculate_hardware_profile(cpu_count, ram_gb):
    use_redo = ram_gb >= 15.0
    safe_ram_per_thread = 1.5 
    max_ram_threads = max(1, int(ram_gb / safe_ram_per_thread))
    build_threads = min(cpu_count, max_ram_threads)
    
    return {
        "threads": build_threads,
        "mode": "redo" if use_redo else "time",
        "tier": "Performance" if use_redo else "Budget"
    }

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

# --- The Main Handler ---
def handler(job):
    job_input = job['input']
    
    github_repo = job_input.get('github_repo')
    if not github_repo:
        return {"error": "Missing 'github_repo' in input."}
    
    branch = job_input.get('branch', 'main')
    dockerfile_path = job_input.get('dockerfile_path', 'Dockerfile')
    build_ctx_path = job_input.get('build_ctx_path', '.')
    github_token = job_input.get('github_access_token')
    
    dockerhub_repo = job_input.get('dockerhub_repo')
    if not dockerhub_repo:
        return {"error": "Missing 'dockerhub_repo' in input."}
    
    dockerhub_tag = job_input.get('dockerhub_tag', 'latest')
    dockerhub_token = job_input.get('dockerhub_access_token')
    
    gh_auth_map = parse_auth_env('github_pat_auth')
    dh_auth_map = parse_auth_env('dockerhub_pat_auth')
    
    if not github_token:
        repo_owner = github_repo.split('/')[0]
        github_token = gh_auth_map.get(repo_owner)
        
    dh_user = dockerhub_repo.split('/')[0]
    if not dockerhub_token:
        dockerhub_token = dh_auth_map.get(dh_user)

    full_image_tag = f"{dockerhub_repo}:{dockerhub_tag}"
    
    # ---------------------------------------------------------
    # WORKSPACE PREPARATION 
    # ---------------------------------------------------------
    builder_workspace = "/tmp/builder_workspace"
    os.makedirs(builder_workspace, exist_ok=True)
    
    with tempfile.TemporaryDirectory(dir=builder_workspace) as tmp_dir:
        repo_dir = os.path.join(tmp_dir, "repo")
        clone_url = f"https://{github_token}@github.com/{github_repo}.git" if github_token else f"https://github.com/{github_repo}.git"
            
        print(f"Cloning {github_repo} (branch: {branch})...")
        try:
            Repo.clone_from(clone_url, repo_dir, branch=branch)
        except Exception as e:
            return {"error": f"Failed to clone repository: {str(e)}"}
        
        absolute_ctx_path = os.path.abspath(os.path.join(repo_dir, build_ctx_path))
        absolute_dockerfile_path = os.path.abspath(os.path.join(repo_dir, dockerfile_path))

        # ---------------------------------------------------------
        # ENVIRONMENT ROUTING
        # ---------------------------------------------------------
        kaniko_original_binary = shutil.which("executor")
        if not kaniko_original_binary:
            if os.path.exists("/kaniko-engine/executor"):
                kaniko_original_binary = "/kaniko-engine/executor"
            elif os.path.exists("/kaniko/executor"):
                kaniko_original_binary = "/kaniko/executor"

        if kaniko_original_binary:
            print("Production environment detected. Preparing Kaniko Ghost Engine...")
            
            # --- THE GHOST ENGINE FIX ---
            kaniko_original_dir = os.path.dirname(kaniko_original_binary)
            kaniko_ghost_dir = "/tmp/kaniko-ghost"
            
            if not os.path.exists(kaniko_ghost_dir):
                shutil.copytree(kaniko_original_dir, kaniko_ghost_dir)
                
            kaniko_ghost_binary = os.path.join(kaniko_ghost_dir, "executor")
            os.chmod(kaniko_ghost_binary, 0o755)

            # --- THE LIFELINE (Prevents RunPod heartbeat crash) ---
            cert_lifeline = "/tmp/cacert.pem"
            if os.path.exists("/__runpod_shield__/cacert.pem"):
                shutil.copy("/__runpod_shield__/cacert.pem", cert_lifeline)
            elif os.path.exists("/etc/ssl/certs/ca-certificates.crt"):
                shutil.copy("/etc/ssl/certs/ca-certificates.crt", cert_lifeline)
                
            os.environ["REQUESTS_CA_BUNDLE"] = cert_lifeline
            os.environ["SSL_CERT_FILE"] = cert_lifeline

            # --- HARDWARE PROFILING ---
            cpu_count = os.cpu_count() or 1
            actual_ram_gb = get_container_memory_gb()
            profile = calculate_hardware_profile(cpu_count, actual_ram_gb)
            
            print(f"--- Hardware Profile Detected ---")
            print(f"vCPUs: {cpu_count} | RAM: {actual_ram_gb:.2f} GB")
            print(f"Assigned Tier: {profile['tier']}")
            print(f"Snapshot Mode: {profile['mode']}")
            print(f"Safe Build Threads: {profile['threads']}")
            print(f"---------------------------------")
            
            docker_config_dir = os.path.join(tmp_dir, ".docker")
            os.makedirs(docker_config_dir, exist_ok=True)
            
            if dockerhub_token:
                auth_string = f"{dh_user}:{dockerhub_token}"
                encoded_auth = base64.b64encode(auth_string.encode('utf-8')).decode('utf-8')
                config_data = {"auths": {"https://index.docker.io/v1/": {"auth": encoded_auth}}}
                with open(os.path.join(docker_config_dir, "config.json"), "w") as f:
                    json.dump(config_data, f)
            
            kaniko_cmd = [
                kaniko_ghost_binary,
                "--context", absolute_ctx_path,
                "--dockerfile", absolute_dockerfile_path,
                "--destination", full_image_tag,
                "--use-new-run",              
                "--compressed-caching=false", 
                "--ignore-path=/tmp", # Protects our Workspace, Ghost Engine, and Lifeline Cert
                f"--snapshot-mode={profile['mode']}",
                "--build-arg", f"MAKEFLAGS=-j{profile['threads']}",
                "--build-arg", f"NPROC={profile['threads']}",
                "--build-arg", f"MAX_JOBS={profile['threads']}",
                "--build-arg", f"RAYON_NUM_THREADS={profile['threads']}"
            ]
            
            env = os.environ.copy()
            env["DOCKER_CONFIG"] = docker_config_dir
            env["GOMAXPROCS"] = str(profile['threads']) 

            print(f"Building {full_image_tag}...")
            build_proc = subprocess.run(kaniko_cmd, env=env, capture_output=True, text=True)
            
            if build_proc.returncode != 0:
                return {"success": False, "error": "Kaniko build/push failed", "stdout": build_proc.stdout, "stderr": build_proc.stderr}
            
            # --- KAMIKAZE PROTOCOL ---
            # Spin up a background thread to kill the container after returning success.
            # This ensures RunPod provisions a fresh container for the next job,
            # wiping away the filesystem damage Kaniko caused.
            def kamikaze():
                time.sleep(5)
                print("Kamikaze triggered: Killing corrupted worker to force a clean pod for the next job.")
                os._exit(0)
                
            threading.Thread(target=kamikaze, daemon=True).start()
                
            return {"success": True, "message": f"Successfully built and pushed {full_image_tag} via Kaniko", "build_log": build_proc.stdout}

        else:
            print("Local environment detected. Falling back to host Docker daemon...")
            if not shutil.which("docker"):
                return {"success": False, "error": "Neither Kaniko nor Docker was found. Cannot build image."}

            if dockerhub_token:
                print("Logging into DockerHub locally...")
                login_cmd = ["docker", "login", "-u", dh_user, "--password-stdin"]
                login_proc = subprocess.run(login_cmd, input=dockerhub_token, capture_output=True, text=True)
                if login_proc.returncode != 0:
                    return {"success": False, "error": "Local Docker login failed", "stderr": login_proc.stderr}

            print(f"Building {full_image_tag} locally...")
            build_cmd = ["docker", "build", "-t", full_image_tag, "-f", absolute_dockerfile_path, absolute_ctx_path]
            build_proc = subprocess.run(build_cmd, capture_output=True, text=True)
            if build_proc.returncode != 0:
                return {"success": False, "error": "Local Docker build failed", "stderr": build_proc.stderr}

            print(f"Pushing {full_image_tag} locally...")
            push_cmd = ["docker", "push", full_image_tag]
            push_proc = subprocess.run(push_cmd, capture_output=True, text=True)
            if push_proc.returncode != 0:
                return {"success": False, "error": "Local Docker push failed", "stderr": push_proc.stderr}

            return {"success": True, "message": f"Successfully built and pushed {full_image_tag} via Local Docker", "build_log": build_proc.stdout}

if __name__ == "__main__":
    print("RunPod Auto-Builder Worker Started.")
    runpod.serverless.start({"handler": handler})