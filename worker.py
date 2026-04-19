import runpod
import subprocess
import os
import json
import urllib.parse
import base64

def handler(job):
    job_input = job.get('input', {})
    
    # 1. Inputs
    github_repo = job_input.get('github_repo')
    dockerhub_repo = job_input.get('dockerhub_repo')
    
    if not github_repo or not dockerhub_repo:
        return {"status": "error", "message": "Missing github_repo or dockerhub_repo"}
    
    branch = job_input.get('branch', 'main')
    dockerfile_path = job_input.get('dockerfile_path', 'Dockerfile')
    build_ctx_path = job_input.get('build_ctx_path', '')
    dockerhub_tag = job_input.get('dockerhub_tag', 'latest')
    
    # 2. Authentication Secrets
    gh_auth = job_input.get('github_access_token') or os.environ.get('github_pat_auth')
    dh_auth = job_input.get('dockerhub_access_token') or os.environ.get('dockerhub_pat_auth')

    # 3. Configure DockerHub Auth (For Pushing)
    if dh_auth:
        try:
            os.makedirs('/kaniko/.docker', exist_ok=True)
            # Support both "user:token" and "token" (for Hub, usually user:token)
            auth_str = base64.b64encode(dh_auth.encode()).decode()
            config = {"auths": {"https://index.docker.io/v1/": {"auth": auth_str}}}
            with open('/kaniko/.docker/config.json', 'w') as f:
                json.dump(config, f)
        except Exception as e:
            print(f"Auth Setup Error: {e}")

    # 4. Construct Git Context
    # If using a PAT, the most reliable Kaniko format is: git://<token>@github.com/<repo>
    if gh_auth:
        # If the secret is "username:token", we just need the token part for the URL username field
        token_only = gh_auth.split(':')[-1]
        safe_token = urllib.parse.quote(token_only)
        context_url = f"git://{safe_token}@github.com/{github_repo}#refs/heads/{branch}"
        print(f"Context: git://[REDACTED]@github.com/{github_repo}#refs/heads/{branch}")
    else:
        context_url = f"https://github.com/{github_repo}.git#refs/heads/{branch}"

    # 5. Build Kaniko Arguments
    # We use list format to avoid shell injection and quoting issues
    cmd = [
        "/kaniko/executor",
        "--context", context_url,
        "--dockerfile", dockerfile_path,
        "--destination", f"{dockerhub_repo}:{dockerhub_tag}",
        "--force"
    ]

    # Add optional sub-path if provided
    if build_ctx_path:
        cmd.extend(["--context-sub-path", build_ctx_path])

    print(f"Running Kaniko with args: {cmd}")

    # 6. Execution
    try:
        # capture_output=False so it streams to RunPod system logs automatically
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )

        logs = []
        for line in process.stdout:
            print(line, end='')
            logs.append(line)
        
        process.wait()

        if process.returncode == 0:
            return {"status": "success", "image": f"{dockerhub_repo}:{dockerhub_tag}"}
        else:
            # Return the last few lines of the actual error to RunPod
            error_snippet = "".join(logs[-10:]) if logs else "No logs captured."
            return {
                "status": "error", 
                "message": f"Exit Code {process.returncode}",
                "details": error_snippet
            }

    except Exception as e:
        return {"status": "error", "message": str(e)}

runpod.serverless.start({"handler": handler})