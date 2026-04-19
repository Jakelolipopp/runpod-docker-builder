FROM gcr.io/kaniko-project/executor:latest AS kaniko
FROM python:3.11-slim

# 1. Install prerequisites
RUN apt-get update && apt-get install -y git ca-certificates && rm -rf /var/lib/apt/lists/*

# 2. Copy Kaniko binaries to a UNIQUE path to prevent ETXTBSY collisions
COPY --from=kaniko /kaniko /kaniko-engine
ENV PATH="$PATH:/kaniko-engine"

# 3. Create the hyper-isolated worker bubble
RUN mkdir -p /__runpod_shield__/code
WORKDIR /__runpod_shield__/code

# 4. Create an isolated Virtual Environment
RUN python3 -m venv /__runpod_shield__/venv
ENV PATH="/__runpod_shield__/venv/bin:$PATH"

# 5. Shield the SSL Certificates
RUN cp /etc/ssl/certs/ca-certificates.crt /__runpod_shield__/cacert.pem
ENV REQUESTS_CA_BUNDLE="/__runpod_shield__/cacert.pem"
ENV SSL_CERT_FILE="/__runpod_shield__/cacert.pem"

# 6. Install worker dependencies into the shield
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 7. Copy worker script
COPY worker.py .

# Run the shielded worker
CMD ["/__runpod_shield__/venv/bin/python", "-u", "worker.py"]