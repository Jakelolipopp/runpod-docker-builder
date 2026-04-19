# Use the kaniko debug image which includes a shell
FROM gcr.io/kaniko-project/executor:debug AS kaniko

# Use a python base for the RunPod worker logic
FROM python:3.10-slim

# Copy kaniko binary and certificates from the kaniko image
COPY --from=kaniko /kaniko/executor /kaniko/executor
COPY --from=kaniko /kaniko/ssl/certs/ /kaniko/ssl/certs/
COPY --from=kaniko /busybox/ /busybox/

# Set PATH to include busybox for basic shell commands (mkdir, cp, etc)
ENV PATH="/busybox:/kaniko:${PATH}"
ENV SSL_CERT_DIR=/kaniko/ssl/certs

# Install RunPod SDK
RUN pip install runpod

# Copy your worker script
COPY worker.py .

CMD [ "python", "-u", "/worker.py" ]