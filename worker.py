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
    # WORKSPACE PREPARATION (Inside the Shield)
    # ---------------------------------------------------------
    shield_workspace = "/__runpod_shield__/workspace"
    os.makedirs(shield_workspace, exist_ok=True)
    
    with tempfile.TemporaryDirectory(dir=shield_workspace) as tmp_dir:
        repo_dir = os.path.join(tmp_dir, "repo")
        clone_url = f"https://{github_token}@github.com/{github_repo}.git" if github_token else f"https://github.com/{github_repo}.git"
            
        print(f"Cloning {github_repo} (branch: {branch})...")
        try:
            # OPTIMIZATION: Shallow clone (depth=1) speeds up download significantly
            Repo.clone_from(clone_url, repo_dir, branch=branch, depth=1)
        except Exception as e:
            return {"error": f"Failed to clone repository: {str(e)}"}
        
        absolute_ctx_path = os.path.abspath(os.path.join(repo_dir, build_ctx_path))
        absolute_dockerfile_path = os.path.abspath(os.path.join(repo_dir, dockerfile_path))

        # ---------------------------------------------------------
        # ENVIRONMENT ROUTING
        # ---------------------------------------------------------
        
        # Check for the updated kaniko-engine path
        if os.path.exists("/kaniko-engine/executor"):
            print("Production environment detected. Using Shielded Kaniko...")
            
            docker_config_dir = os.path.join(tmp_dir, ".docker")
            os.makedirs(docker_config_dir, exist_ok=True)
            
            if dockerhub_token:
                auth_string = f"{dh_user}:{dockerhub_token}"
                encoded_auth = base64.b64encode(auth_string.encode('utf-8')).decode('utf-8')
                config_data = {"auths": {"https://index.docker.io/v1/": {"auth": encoded_auth}}}
                with open(os.path.join(docker_config_dir, "config.json"), "w") as f:
                    json.dump(config_data, f)
            
            # OPTIMIZATION: Hardcoded for known hardware environment
            cpu_count = 16
            
            kaniko_cmd = [
                "/kaniko-engine/executor",
                "--context", absolute_ctx_path,
                "--dockerfile", absolute_dockerfile_path,
                "--destination", full_image_tag,
                "--use-new-run",              
                "--compressed-caching=false", 
                "--ignore-path=/__runpod_shield__", 
                "--ignore-path=/kaniko-engine", 
                "--build-arg", f"MAKEFLAGS=-j{cpu_count}",
                "--build-arg", f"NPROC={cpu_count}",
                "--build-arg", f"MAX_JOBS={cpu_count}",
                "--snapshot-mode=redo" # OPTIMIZATION: Explicitly use high-RAM redo mode
            ]
            
            env = os.environ.copy()
            env["DOCKER_CONFIG"] = docker_config_dir
            # OPTIMIZATION: Maximum concurrency for Go execution
            env["GOMAXPROCS"] = str(cpu_count)
            # OPTIMIZATION: Delay Garbage Collection to utilize the 32GB RAM for faster builds
            env["GOGC"] = "1000"
            # OPTIMIZATION: Soft memory limit to prevent Go from utilizing 100% of RAM and crashing
            env["GOMEMLIMIT"] = "28000MiB"

            build_proc = subprocess.run(kaniko_cmd, env=env, capture_output=True, text=True)
            
            if build_proc.returncode != 0:
                return {"success": False, "error": "Kaniko build/push failed", "stdout": build_proc.stdout, "stderr": build_proc.stderr}
                
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