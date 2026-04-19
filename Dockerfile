FROM quay.io/buildah/stable

# Install Python 3 and Git
RUN dnf -y update && \
    dnf -y install python3 python3-pip git && \
    dnf clean all

# Ensure Buildah uses VFS globally to prevent overlayfs namespace errors
ENV STORAGE_DRIVER=vfs
ENV BUILDAH_ISOLATION=chroot

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN python3 -m pip install --no-cache-dir -r requirements.txt

# Copy the worker script
COPY worker.py .

# Run the worker unbuffered
CMD ["python3", "-u", "worker.py"]