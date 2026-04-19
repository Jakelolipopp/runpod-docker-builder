import os
import subprocess
import tempfile
import base64
import json
import runpod
from git import Repo

# --- Hardware Detection Helpers ---

def get_container_memory_gb():
    """
    Reads container memory limits directly from Linux cgroups to get 
    the true allocated RAM, avoiding false reporting from the host machine.
    """
    # 1. Try cgroups v2 (Modern Docker/Kubernetes)
    try:
        with open('/sys/fs/cgroup/memory.max', 'r') as f:
            val = f.read().strip()
            if val != 'max':
                return int(val) / (1024 ** 3)
    except Exception:
        pass

    # 2. Try cgroups v1 (Older Docker/Kubernetes)
    try:
        with open('/sys/fs/cgroup/memory/memory.limit_in_bytes', 'r') as f:
            val = f.read().strip()
            if int(val) < 9000000000000000000: # Ignore unconstrained defaults
                return int(val) / (1024 ** 3)
    except Exception:
        pass

    # 3. Fallback: Host system memory
    try:
        pages = os.sysconf('SC_PHYS_PAGES')
        page_size = os.sysconf('SC_PAGE_SIZE')
        return (pages * page_size) / (1024 ** 3)
    except Exception:
        return 4.0 # Absolute safety fallback

# --- Authentication Helpers ---

def parse_auth_env(env_name):
    """Parses multiline 'user:token' secrets mapped from RunPod environment variables."""
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
    
    # 1. Parse GitHub Inputs
    github_repo = job_input.get('github_repo')
    if not github_repo:
        return {"error": "Missing 'github_repo' in input."}
    
    branch = job_input.get('branch', 'main')
    dockerfile_path = job_input.get('dockerfile_path', 'Dockerfile')
    build_ctx_path = job_input.get('build_ctx_path', '.')
    github_token = job_input.get('github_access_token')
    
    # 2. Parse DockerHub Inputs
    dockerhub_repo = job_input.get('dockerhub_repo')
    if not dockerhub_repo:
        return {"error": "Missing 'dockerhub_repo' in input."}
    
    dockerhub_tag = job_input.get('dockerhub_tag', 'latest')
    dockerhub_token = job_input.get('dockerhub_access_token')
    
    # 3. Resolve Credentials
    gh_auth_map = parse_auth_env('github_pat_auth')
    dh_auth_map = parse_auth_env('dockerhub_pat_auth')
    
    if not github_token:
        repo_owner = github_repo.split('/')[0]
        github_token = gh_auth_map.get(repo_owner)
        
    dh_user = dockerhub_repo.split('/')[0]
    if not dockerhub_token:
        dockerhub_token = dh_auth_map.get(dh_user)

    full_image_tag = f"{dockerhub_repo}:{dockerhub_tag}"
    
    # 4. Secure Workspace Preparation
    with tempfile.TemporaryDirectory() as tmp_dir:
        
        # A. Clone the Repository
        repo_dir = os.path.join(tmp_dir, "repo")
        clone_url = f"https://{github_token}@github.com/{github_repo}.git" if github_token else f"https://github.com/{github_repo}.git"
            
        print(f"Cloning {github_repo} (branch: {branch})...")
        try:
            Repo.clone_from(clone_url, repo_dir, branch=branch)
        except Exception as e:
            return {"error": f"Failed to clone repository: {str(e)}"}
        
        # B. Generate Docker Authentication for Kaniko
        docker_config_dir = os.path.join(tmp_dir, ".docker")
        os.makedirs(docker_config_dir, exist_ok=True)
        
        if dockerhub_token:
            print(f"Preparing DockerHub authentication for {dh_user}...")
            auth_string = f"{dh_user}:{dockerhub_token}"
            encoded_auth = base64.b64encode(auth_string.encode('utf-8')).decode('utf-8')
            config_data = {"auths": {"https://index.docker.io/v1/": {"auth": encoded_auth}}}
            
            with open(os.path.join(docker_config_dir, "config.json"), "w") as f:
                json.dump(config_data, f)
        else:
            print("Warning: No DockerHub token found. Push will fail unless pushing to a local/unauthenticated registry.")

        # C. Execute Kaniko Build & Push (Hardware-Aware)
        cpu_count = os.cpu_count() or 1
        actual_ram_gb = get_container_memory_gb()
        
        print(f"Hardware Check: Detected {cpu_count} vCPUs and {actual_ram_gb:.2f} GB of allocated RAM.")

        absolute_ctx_path = os.path.abspath(os.path.join(repo_dir, build_ctx_path))
        absolute_dockerfile_path = os.path.abspath(os.path.join(repo_dir, dockerfile_path))
        
        print(f"Configuring Kaniko build for {full_image_tag}...")
        
        # Base commands applicable to ALL worker sizes
        kaniko_cmd = [
            "/kaniko/executor",
            "--context", absolute_ctx_path,
            "--dockerfile", absolute_dockerfile_path,
            "--destination", full_image_tag,
            "--use-new-run",              
            "--compressed-caching=false", 
            
            # Fan out internal build tasks across all available cores
            "--build-arg", f"MAKEFLAGS=-j{cpu_count}",
            "--build-arg", f"NPROC={cpu_count}",
            "--build-arg", f"MAX_JOBS={cpu_count}",
            "--build-arg", f"RAYON_NUM_THREADS={cpu_count}"
        ]

        # Explicit Memory-Based Conditional Scaling
        if actual_ram_gb >= 15.0:
            print("Action: RAM >= 15GB. Enabling high-performance 'redo' snapshotting.")
            kaniko_cmd.extend(["--snapshot-mode=redo"])
        else:
            print("Action: RAM < 15GB. Using safer 'time' snapshotting to prevent OOM errors.")
            kaniko_cmd.extend(["--snapshot-mode=time"])
        
        env = os.environ.copy()
        env["DOCKER_CONFIG"] = docker_config_dir
        env["GOMAXPROCS"] = str(cpu_count) # Force Kaniko to use all vCPUs

        print(f"Executing Kaniko build...")
        build_proc = subprocess.run(
            kaniko_cmd,
            env=env,
            capture_output=True,
            text=True
        )
        
        if build_proc.returncode != 0:
            return {
                "success": False,
                "error": "Kaniko build/push failed",
                "stdout": build_proc.stdout,
                "stderr": build_proc.stderr
            }
            
        return {
            "success": True,
            "message": f"Successfully built and pushed {full_image_tag}",
            "build_log": build_proc.stdout
        }

if __name__ == "__main__":
    print("RunPod Kaniko Auto-Builder Worker Started.")
    runpod.serverless.start({"handler": handler})