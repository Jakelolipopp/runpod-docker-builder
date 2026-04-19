FROM gcr.io/kaniko-project/executor:latest AS kaniko
FROM python:3.11-slim

# 1. Install prerequisites
RUN apt-get update && apt-get install -y git ca-certificates && rm -rf /var/lib/apt/lists/*

# 2. Copy Kaniko binaries to a UNIQUE path to prevent ETXTBSY collisions
COPY --from=kaniko /kaniko /kaniko-engine
ENV PATH="$PATH:/kaniko-engine"

# 3. Create the hyper-isolated worker bubble (No venv to prevent OCI runc crashes)
RUN mkdir -p /__runpod_shield__
WORKDIR /__runpod_shield__

# 4. Shield the SSL Certificates
RUN cp /etc/ssl/certs/ca-certificates.crt /__runpod_shield__/cacert.pem
ENV REQUESTS_CA_BUNDLE="/__runpod_shield__/cacert.pem"
ENV SSL_CERT_FILE="/__runpod_shield__/cacert.pem"

# 5. Install worker dependencies into the shield globally
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 6. Copy worker script
COPY worker.py .

# Run the shielded worker using the global python binary
CMD ["python3", "-u", "worker.py"]