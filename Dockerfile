# Stage 1: Grab Kaniko binaries and SSL certificates
FROM gcr.io/kaniko-project/executor:latest AS kaniko

# Stage 2: Your RunPod Worker
FROM python:3.11-slim

# Install git for cloning, and ca-certificates for secure DockerHub pushes
RUN apt-get update && apt-get install -y \
    git \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy the Kaniko executor binary and its required assets
COPY --from=kaniko /kaniko /kaniko
ENV PATH="$PATH:/kaniko"

# Set the working directory
WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the worker script
COPY worker.py .

# Run the worker with unbuffered logs so they stream in real-time to the RunPod UI
CMD ["python", "-u", "worker.py"]