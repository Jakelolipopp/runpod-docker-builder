FROM python:3.11-slim

# Install system dependencies
# git: for cloning repositories
# buildah: for daemonless image building
# uidmap: often required for rootless/containerized buildah operations
RUN apt-get update && apt-get install -y \
    git \
    buildah \
    uidmap \
    && rm -rf /var/lib/apt/lists/*

# Set the working directory
WORKDIR /

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the worker script
COPY worker.py .

# Run the worker
# -u flag for unbuffered output to ensure logs appear immediately in RunPod
CMD ["python", "-u", "worker.py"]
