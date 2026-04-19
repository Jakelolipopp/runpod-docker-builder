import os
import subprocess
import tempfile
import runpod
from git import Repo

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
        
        absolute_ctx_path = os.path.abspath(os.path.join(repo_dir, build_ctx_path))
        absolute_dockerfile_path = os.path.abspath(os.path.join(repo_dir, dockerfile_path))

        # ---------------------------------------------------------
        # THE BUILDAH EXECUTION ENGINE (Safe for RunPod Serverless)
        # ---------------------------------------------------------
        print("Starting isolated daemonless build via Buildah...")
        cpu_count = os.cpu_count() or 1
        
        # Step B: Secure DockerHub Login
        if dockerhub_token:
            print(f"Authenticating with DockerHub as {dh_user}...")
            login_cmd = [
                "buildah", "login", 
                "--storage-driver", "vfs",
                "-u", dh_user, 
                "--password-stdin", "docker.io"
            ]
            login_proc = subprocess.run(login_cmd, input=dockerhub_token, capture_output=True, text=True)
            if login_proc.returncode != 0:
                return {"success": False, "error": "Buildah login failed", "stderr": login_proc.stderr}

        # Step C: Build the Image
        print(f"Building {full_image_tag} using {cpu_count} vCPUs...")
        build_cmd = [
            "buildah", "bud", 
            "--storage-driver", "vfs", 
            "--isolation", "chroot",
            "--jobs", str(cpu_count),
            "--build-arg", f"MAKEFLAGS=-j{cpu_count}",
            "--build-arg", f"NPROC={cpu_count}",
            "--build-arg", f"MAX_JOBS={cpu_count}",
            "-t", full_image_tag, 
            "-f", absolute_dockerfile_path, 
            absolute_ctx_path
        ]
        
        build_proc = subprocess.run(build_cmd, capture_output=True, text=True)
        if build_proc.returncode != 0:
            return {"success": False, "error": "Buildah build failed", "stdout": build_proc.stdout, "stderr": build_proc.stderr}

        # Step D: Push the Image
        print(f"Pushing {full_image_tag} to DockerHub (zstd compressed)...")
        push_cmd = [
            "buildah", "push", 
            "--storage-driver", "vfs",
            "--compression-format", "zstd", 
            full_image_tag, 
            f"docker://docker.io/{full_image_tag}"
        ]
        
        push_proc = subprocess.run(push_cmd, capture_output=True, text=True)
        if push_proc.returncode != 0:
            return {"success": False, "error": "Buildah push failed", "stdout": push_proc.stdout, "stderr": push_proc.stderr}

        return {
            "success": True, 
            "message": f"Successfully built and pushed {full_image_tag}", 
            "build_log": build_proc.stdout
        }

if __name__ == "__main__":
    print("RunPod Auto-Builder Worker Started.")
    runpod.serverless.start({"handler": handler})