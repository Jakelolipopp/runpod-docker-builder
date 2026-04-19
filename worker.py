import os
import subprocess
import tempfile
import shutil
import runpod
from git import Repo

"""
RunPod Serverless Secret Mapping Instructions:
---------------------------------------------
The fallback credentials 'github_pat_auth' and 'dockerhub_pat_auth' are intended 
to be stored as RunPod Secrets and mapped to Environment Variables in your 
Endpoint / Template configuration.

To map them, go to your RunPod Endpoint Settings -> Environment Variables and add:
- Key: github_pat_auth
  Value: {{ RUNPOD_SECRET_github_pat_auth }}
- Key: dockerhub_pat_auth
  Value: {{ RUNPOD_SECRET_dockerhub_pat_auth }}

Format for the secret value:
user1:token1
user2:token2
"""

# --- Helper Functions ---

def parse_auth_env(env_name):
    """
    Parses environment variables formatted as 'user:token\nuser2:token2'
    into a dictionary of {user: token}.
    """
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

def run_command(command, input_str=None):
    """
    Executes a shell command and returns (stdout, stderr, returncode).
    """
    # Merge current environment with BUILDAH_ISOLATION
    env = os.environ.copy()
    env["BUILDAH_ISOLATION"] = "chroot"
    
    result = subprocess.run(
        command,
        input=input_str,
        capture_output=True,
        text=True,
        shell=False,
        env=env # Force isolation mode for all commands
    )
    return result.stdout, result.stderr, result.returncode

# --- The Handler ---

def handler(job):
    """
    The main RunPod handler for the Docker Auto-Builder.
    """
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
    
    # 3. Resolve Credentials from Environment if not provided in payload
    gh_auth_raw = os.environ.get('github_pat_auth')
    dh_auth_raw = os.environ.get('dockerhub_pat_auth')
    
    if not gh_auth_raw and not github_token:
        print("INFO: 'github_pat_auth' environment variable not found. Ensure it is mapped from RunPod Secrets.")
    if not dh_auth_raw and not dockerhub_token:
        print("INFO: 'dockerhub_pat_auth' environment variable not found. Ensure it is mapped from RunPod Secrets.")

    gh_auth_map = parse_auth_env('github_pat_auth')
    dh_auth_map = parse_auth_env('dockerhub_pat_auth')
    
    # Resolve GitHub Token
    if not github_token:
        repo_owner = github_repo.split('/')[0]
        github_token = gh_auth_map.get(repo_owner)
        
    # Resolve DockerHub Token
    dh_user = dockerhub_repo.split('/')[0]
    if not dockerhub_token:
        dockerhub_token = dh_auth_map.get(dh_user)

    # 4. Preparation & Build Process
    full_image_tag = f"{dockerhub_repo}:{dockerhub_tag}"
    
    with tempfile.TemporaryDirectory() as tmp_dir:
        # A. Clone the Repository
        repo_dir = os.path.join(tmp_dir, "repo")
        
        # Construct authenticated URL if token exists
        if github_token:
            clone_url = f"https://{github_token}@github.com/{github_repo}.git"
        else:
            clone_url = f"https://github.com/{github_repo}.git"
            
        print(f"Cloning {github_repo} (branch: {branch})...")
        try:
            Repo.clone_from(clone_url, repo_dir, branch=branch)
        except Exception as e:
            return {"error": f"Failed to clone repository: {str(e)}"}
        
        # B. DockerHub Login (via Buildah)
        if dockerhub_token:
            print(f"Logging into DockerHub as {dh_user}...")
            login_cmd = [
                "buildah", "login", 
                "--storage-driver", "vfs",
                "--isolation", "chroot",
                "-u", dh_user, 
                "--password-stdin", "docker.io"
            ]
            _, err, code = run_command(login_cmd, input_str=dockerhub_token)
            if code != 0:
                return {"error": "DockerHub login failed", "stderr": err}
        else:
            print("Warning: No DockerHub token provided or found in environment. Push might fail.")

        # 5. Optimization: Detect CPU Count and Configure Parallelism
        cpu_count = os.cpu_count() or 1
        print(f"Optimization: Detected {cpu_count} vCPUs. Configuring for parallel build.")
        
        # C. Build the Image
        # We need to change CWD to the build context inside the repo
        absolute_ctx_path = os.path.abspath(os.path.join(repo_dir, build_ctx_path))
        # Ensure dockerfile path is absolute relative to the repo root
        absolute_dockerfile_path = os.path.abspath(os.path.join(repo_dir, dockerfile_path))
        
        print(f"Building image {full_image_tag}...")
        build_cmd = [
            "buildah", "bud",
            "--storage-driver", "vfs",
            "--isolation", "chroot",
            "--jobs", str(cpu_count), # Parallelize build stages if possible
            # Inject parallelism flags as build args so tools inside the Dockerfile (make, pip, npm) use all cores
            "--build-arg", f"MAKEFLAGS=-j{cpu_count}",
            "--build-arg", f"NPROC={cpu_count}",
            "--build-arg", f"MAX_JOBS={cpu_count}",
            "-t", full_image_tag,
            "-f", absolute_dockerfile_path,
            "." # We will run this from the absolute_ctx_path
        ]
        
        # Run build from the context directory
        build_proc = subprocess.run(
            build_cmd,
            cwd=absolute_ctx_path,
            capture_output=True,
            text=True
        )
        
        if build_proc.returncode != 0:
            return {
                "success": False,
                "error": "Build failed",
                "stdout": build_proc.stdout,
                "stderr": build_proc.stderr
            }
        
        # D. Push the Image
        print(f"Pushing image {full_image_tag} to DockerHub using zstd compression...")
        push_cmd = [
            "buildah", "push",
            "--storage-driver", "vfs",
            "--isolation", "chroot",
            "--compression-format", "zstd", # Multi-threaded, high-performance compression
            "--threads", str(cpu_count),    # Use all cores for compression
            full_image_tag,
            f"docker://docker.io/{full_image_tag}"
        ]
        
        push_stdout, push_stderr, push_code = run_command(push_cmd)
        
        if push_code != 0:
            return {
                "success": False,
                "error": "Push failed",
                "stdout": push_stdout,
                "stderr": push_stderr
            }
            
        return {
            "success": True,
            "message": f"Successfully built and pushed {full_image_tag}",
            "build_log": build_proc.stdout
        }

# Start the serverless worker
if __name__ == "__main__":
    print("RunPod Docker Auto-Builder Worker Started.")
    runpod.serverless.start({"handler": handler})
