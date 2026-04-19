FROM gcr.io/kaniko-project/executor:latest AS kaniko
FROM python:3.11-slim

# 1. System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 2. Copy the ENTIRE Kaniko directory (fixes the lstat error and brings SSL certs)
COPY --from=kaniko /kaniko /kaniko

# Set environment variables for Kaniko
ENV PATH="/kaniko:${PATH}"
ENV SSL_CERT_DIR="/kaniko/ssl/certs"

# 3. Install Python dependencies globally (no venv)
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 4. Copy worker
COPY worker.py .

CMD ["python3", "-u", "worker.py"]