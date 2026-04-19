# RunPod Docker Auto-Builder Worker

An on-demand, serverless Docker image builder for RunPod. This worker clones a GitHub repository, builds a Docker image using `buildah`, and pushes it to DockerHub.

## Features

- **Automated Authentication**: Securely handles GitHub and DockerHub credentials via RunPod Secrets.
- **Flexible Contexts**: Supports custom branches, Dockerfile paths, and build context subdirectories.
- **Isolated Workspace**: Each job runs in a unique temporary directory to prevent state leakage.

## Deployment

### 1. Build & Push the Worker Image
Build the worker image using the provided `Dockerfile` and push it to your preferred registry (e.g., DockerHub).

```bash
docker build -t youruser/runpod-docker-builder:latest .
docker push youruser/runpod-docker-builder:latest
```

### 2. Configure RunPod Secrets
Store your credentials in the **Secrets** section of the RunPod Console:

- **Secret Name**: `github_pat_auth`
  - **Value**: `username:your_token` (Multiple entries allowed, one per line)
- **Secret Name**: `dockerhub_pat_auth`
  - **Value**: `username:your_token`

### 3. Create a RunPod Serverless Endpoint
1. Create a new Template using your worker image.
2. Under **Environment Variables**, map the secrets:
   - `github_pat_auth` -> `{{ RUNPOD_SECRET_github_pat_auth }}`
   - `dockerhub_pat_auth` -> `{{ RUNPOD_SECRET_dockerhub_pat_auth }}`
3. Deploy the endpoint.

## API Usage

Send a POST request to your RunPod endpoint with the following JSON structure:

### Payload Schema

| Key | Type | Description | Default |
| :--- | :--- | :--- | :--- |
| `github_repo` | String | **Required**. Repo in `owner/name` format. | N/A |
| `dockerhub_repo` | String | **Required**. Target repo in `user/repo` format. | N/A |
| `branch` | String | Git branch to clone. | `main` |
| `dockerfile_path` | String | Path to Dockerfile relative to repo root. | `Dockerfile` |
| `build_ctx_path` | String | Path to build context relative to repo root. | `.` |
| `dockerhub_tag` | String | Tag for the pushed image. | `latest` |
| `github_access_token`| String | Optional override PAT for GitHub. | From Secrets |
| `dockerhub_access_token`| String | Optional override PAT for DockerHub. | From Secrets |

### Example Request

```json
{
  "input": {
    "github_repo": "jake/awesome-app",
    "dockerhub_repo": "jake/awesome-app-prod",
    "dockerhub_tag": "v1.2.3",
    "branch": "production",
    "build_ctx_path": "src"
  }
}
```
