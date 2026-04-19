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
            # Ensure the directory exists
            os.makedirs('/kaniko/.docker', exist_ok=True)
            
            # DockerHub requires the raw string "username:password" to be base64 encoded
            # If your secret is just the token, you MUST provide "username:token"
            auth_bytes = dh_auth.encode('utf-8')
            auth_base64 = base64.b64encode(auth_bytes).decode('utf-8')
            
            config = {
                "auths": {
                    "https://index.docker.io/v1/": {
                        "auth": auth_base64
                    }
                }
            }
            
            with open('/kaniko/.docker/config.json', 'w') as f:
                json.dump(config, f)
            print("Successfully configured /kaniko/.docker/config.json")
        except Exception as e:
            print(f"Auth Configuration Error: {e}")
    else:
        print("Warning: No DockerHub credentials found in 'dockerhub_pat_auth' environment variable.")

    # 4. Construct Git Context
    if gh_auth:
        token_only = gh_auth.split(':')[-1]
        safe_token = urllib.parse.quote(token_only)
        context_url = f"git://{safe_token}@github.com/{github_repo}#refs/heads/{branch}"
        print(f"Context: git://[REDACTED]@github.com/{github_repo}")
    else:
        context_url = f"git://github.com/{github_repo}#refs/heads/{branch}"
        print(f"Context: {context_url}")

    # 5. Build Kaniko Arguments
    cmd = [
        "/kaniko/executor",
        "--context", context_url,
        "--dockerfile", dockerfile_path,
        "--destination", f"{dockerhub_repo}:{dockerhub_tag}",
        "--force",
        "--skip-push-permission-check" # Prevents premature auth failure before build starts
    ]

    if build_ctx_path and build_ctx_path.strip():
        cmd.extend(["--context-sub-path", build_ctx_path])

    print(f"Running Kaniko with args: {cmd}")

    # 6. Execution
    try:
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
            return {
                "status": "success", 
                "image": f"{dockerhub_repo}:{dockerhub_tag}"
            }
        else:
            error_snippet = "".join(logs[-15:]) if logs else "No logs captured."
            return {
                "status": "error", 
                "message": f"Kaniko Exit Code {process.returncode}",
                "details": error_snippet
            }

    except Exception as e:
        return {"status": "error", "message": f"Internal Error: {str(e)}"}

runpod.serverless.start({"handler": handler})