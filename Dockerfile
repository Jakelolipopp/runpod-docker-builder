FROM gcr.io/kaniko-project/executor:latest AS kaniko
FROM python:3.11-slim

# 1. System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 2. Build the Kaniko Jail
RUN mkdir -p /kaniko-jail/workspace /kaniko-jail/tmp /kaniko-jail/etc
COPY --from=kaniko /kaniko /kaniko-jail/kaniko

# Kaniko needs SSL certs inside its jail to push to Docker Hub/GitHub
RUN cp -r /etc/ssl /kaniko-jail/etc/ssl

# 3. Install Python dependencies
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 4. Copy worker
COPY worker.py .

CMD ["python3", "-u", "worker.py"]