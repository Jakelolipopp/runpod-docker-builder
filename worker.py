import runpod
import subprocess
import os
import json
import urllib.parse

def handler(job):
    """
    RunPod handler to build and push Docker images using Kaniko.
    """
    job_input = job.get('input', {})
    
    # 1. Extract and Validate Input
    github_repo = job_input.get('github_repo')
    dockerhub_repo = job_input.get('dockerhub_repo')
    
    if not github_repo or not dockerhub_repo:
        return {
            "status": "error", 
            "message": "Missing required fields: 'github_repo' and 'dockerhub_repo' are mandatory."
        }
    
    # Optional parameters with defaults
    branch = job_input.get('branch', 'main')
    dockerfile_path = job_input.get('dockerfile_path', 'Dockerfile')
    build_ctx_path = job_input.get('build_ctx_path', '.')
    dockerhub_tag = job_input.get('dockerhub_tag', 'latest')
    
    # 2. Handle Credentials (Prioritize direct input over RunPod Secrets)
    # github_pat_auth format: "username:token" or just "token"
    gh_auth = job_input.get('github_access_token') or os.environ.get('github_pat_auth')
    # dockerhub_pat_auth format: "username:token"
    dh_auth = job_input.get('dockerhub_access_token') or os.environ.get('dockerhub_pat_auth')

    # 3. Configure Docker Registry Auth (for Pushing)
    if dh_auth and ':' in dh_auth:
        try:
            os.makedirs('/kaniko/.docker', exist_ok=True)
            # Create the base64 auth string
            # We use a shell-less approach for safety
            import base64
            encoded_auth = base64.b64encode(dh_auth.encode()).decode()
            
            auth_config = {
                "auths": {
                    "https://index.docker.io/v1/": {
                        "auth": encoded_auth
                    }
                }
            }
            with open('/kaniko/.docker/config.json', 'w') as f:
                json.dump(auth_config, f)
        except Exception as e:
            return {"status": "error", "message": f"Failed to configure DockerHub auth: {str(e)}"}
    else:
        print("Warning: No valid DockerHub credentials found. Push may fail if repo is private.")

    # 4. Construct Authenticated Git URL
    # We URL-encode the auth string to handle special characters in tokens
    if gh_auth:
        encoded_gh = urllib.parse.quote(gh_auth)
        # Pattern: git://user:token@github.com/owner/repo#refs/heads/branch
        git_url = f"git://{encoded_gh}@github.com/{github_repo}#refs/heads/{branch}"
    else:
        git_url = f"https://github.com/{github_repo}.git#refs/heads/{branch}"

    # 5. Execute Kaniko
    # --force is required to run Kaniko inside another container (RunPod)
    # --context-sub-path allows building from a sub-folder in the repo
    cmd = [
        "/kaniko/executor",
        f"--context={git_url}",
        f"--dockerfile={dockerfile_path}",
        f"--destination={dockerhub_repo}:{dockerhub_tag}",
        f"--context-sub-path={build_ctx_path}",
        "--force"
    ]

    print(f"Executing Kaniko: {' '.join(cmd)}")

    try:
        # We use Popen to stream logs in real-time to the RunPod console
        process = subprocess.Popen(
            cmd, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.STDOUT, 
            text=True
        )
        
        full_output = []
        for line in process.stdout:
            print(line, end='')  # Stream to RunPod logs
            full_output.append(line)
        
        process.wait()
        
        if process.returncode == 0:
            return {
                "status": "success",
                "image": f"{dockerhub_repo}:{dockerhub_tag}",
                "job_id": job.get('id')
            }
        else:
            return {
                "status": "error",
                "message": "Kaniko build failed. Check logs for details.",
                "logs": "".join(full_output[-20:]) # Return last 20 lines of error
            }

    except Exception as e:
        return {"status": "error", "message": f"System error: {str(e)}"}

# Start the RunPod worker
runpod.serverless.start({"handler": handler})