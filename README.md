# 🛡️ RunPod Docker Auto-Builder (Shielded Kaniko)

A high-performance, daemonless, serverless Docker image builder for RunPod. This worker leverages **Kaniko** with a "shielded" architecture to provide secure, isolated, and hardware-optimized image builds on-demand.

## 🚀 Key Features

- **Daemonless Building**: Uses Kaniko to build images inside a container without requiring a Docker daemon (`dind`) or privileged mode.
- **🛡️ Shielded Architecture**: Runs in a hyper-isolated workspace (`/__runpod_shield__`) to prevent state leakage and ensure clean builds.
- **⚡ Hardware-Aware Optimization**: 
    - Automatically detects available CPU cores and sets `MAKEFLAGS`, `NPROC`, and `MAX_JOBS` for faster builds.
    - Dynamically switches between `snapshot-mode=redo` (for high-RAM environments >15GB) and `snapshot-mode=time` to maximize performance.
- **🔑 Seamless Authentication**: Supports multi-user PATs for GitHub and DockerHub via RunPod Secrets.
- **🔄 Flexible Contexts**: Full support for custom branches, subdirectories, and specific Dockerfile paths.

---

## 🛠️ Deployment Guide

### 1. Build & Push the Worker
Build the worker image and push it to your registry.

```bash
docker build -t youruser/runpod-docker-builder:latest .
docker push youruser/runpod-docker-builder:latest
```

### 2. Configure RunPod Secrets
Add your credentials to the **Secrets** section in the RunPod Console:

| Secret Name | Expected Format |
| :--- | :--- |
| `github_pat_auth` | `username:token` (One per line for multiple accounts) |
| `dockerhub_pat_auth` | `username:token` (One per line for multiple accounts) |

### 3. Create the Endpoint
1. Create a **Template** using your worker image.
2. Map the environment variables to your secrets:
   - `github_pat_auth` → `{{ RUNPOD_SECRET_github_pat_auth }}`
   - `dockerhub_pat_auth` → `{{ RUNPOD_SECRET_dockerhub_pat_auth }}`
3. Deploy as a **Serverless Endpoint**.

---

## 📡 API Usage

### Payload Schema

| Key | Type | Description | Default |
| :--- | :--- | :--- | :--- |
| `github_repo` | `string` | **Required**. Repository in `owner/repo` format. | N/A |
| `dockerhub_repo` | `string` | **Required**. Target repository in `user/repo` format. | N/A |
| `branch` | `string` | Git branch to clone. | `main` |
| `dockerfile_path` | `string` | Path to Dockerfile relative to repo root. | `Dockerfile` |
| `build_ctx_path` | `string` | Path to build context relative to repo root. | `.` |
| `dockerhub_tag` | `string` | Tag for the pushed image. | `latest` |
| `github_access_token`| `string` | Optional override PAT for GitHub. | (From Secrets) |
| `dockerhub_access_token`| `string` | Optional override PAT for DockerHub. | (From Secrets) |

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

---

## ⚙️ Performance & Optimization

This worker is designed for high-concurrency environments. It performs several optimizations at runtime:

- **Parallelism**: Automatically detects CPU cores to pass `-jN` to build processes.
- **Snapshotting**: Adjusts Kaniko's snapshot strategy based on available container RAM to avoid OOMs while maintaining speed.
- **Cache Handling**: Disables compressed caching by default to prioritize build speed over disk usage (tunable in `worker.py`).
- **Isolation**: Each build occurs in a unique temporary directory within the shield, which is wiped after completion.

---

## 🛡️ Shielded Environment Details
The worker runs within a specialized environment defined in the `Dockerfile`:
- **Isolated VENV**: All Python logic runs in `/__runpod_shield__/venv`.
- **Cert Shielding**: SSL certificates are localized to prevent dependency on host CA stores.
- **Engine Isolation**: The Kaniko engine is stored in `/kaniko-engine` to prevent it from being snapshotted into your target image.

