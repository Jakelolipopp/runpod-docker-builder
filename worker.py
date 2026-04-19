import runpod
import subprocess
import os
import json

def handler(job):
    job_input = job['input']
    
    # Required inputs
    github_repo = job_input.get('github_repo')
    dockerhub_repo = job_input.get('dockerhub_repo')
    
    # Optional inputs / Defaults
    branch = job_input.get('branch', 'main')
    dockerfile_path = job_input.get('dockerfile_path', 'Dockerfile')
    build_ctx_path = job_input.get('build_ctx_path', '.')
    dockerhub_tag = job_input.get('dockerhub_tag', 'latest')
    
    # Authentication (Priority: Input > Environment/Secrets)
    gh_auth = job_input.get('github_access_token') or os.environ.get('github_pat_auth')
    dh_auth = job_input.get('dockerhub_access_token') or os.environ.get('dockerhub_pat_auth')

    # 1. Setup DockerHub Auth
    # Kaniko looks for /kaniko/.docker/config.json
    if dh_auth:
        user, token = dh_auth.split(':')
        auth_content = {
            "auths": {
                "https://index.docker.io/v1/": {
                    "auth": subprocess.check_output(f"echo -n {user}:{token} | base64", shell=True).decode().strip()
                }
            }
        }
        os.makedirs('/kaniko/.docker', exist_ok=True)
        with open('/kaniko/.docker/config.json', 'w') as f:
            json.dump(auth_content, f)

    # 2. Build Git Context URL
    # Format: git://[TOKEN]@github.com/user/repo#refs/heads/branch
    git_url = f"git://{gh_auth}@github.com/{github_repo}#refs/heads/{branch}"

    # 3. Construct Kaniko Command
    cmd = [
        "/kaniko/executor",
        f"--context={git_url}",
        f"--dockerfile={dockerfile_path}",
        f"--destination={dockerhub_repo}:{dockerhub_tag}",
        f"--context-sub-path={build_ctx_path}",
        "--force" # Required because we are running inside a container
    ]

    try:
        # Run kaniko
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode == 0:
            return {"status": "success", "image": f"{dockerhub_repo}:{dockerhub_tag}", "log": result.stdout}
        else:
            return {"status": "error", "message": result.stderr}
            
    except Exception as e:
        return {"status": "error", "message": str(e)}

runpod.serverless.start({"handler": handler})