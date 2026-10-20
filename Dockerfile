FROM python:3.12-slim-bookworm

# wpasupplicant/udhcpc/iproute2/iw join camera access points; smbclient and nfs-common
# list shares during network discovery. BLE goes through the host's bluetoothd over
# D-Bus, so BlueZ itself is not needed in the image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends wpasupplicant udhcpc iproute2 iw smbclient nfs-common \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY ferry ./ferry
RUN pip install --no-cache-dir ".[media]"

LABEL org.opencontainers.image.title="Ferry" \
      org.opencontainers.image.description="Offload cameras, drones and 3D printers to network storage"

ENV FERRY_DATA=/data \
    FERRY_PORT=8780 \
    PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8780
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"FERRY_PORT\"]}/healthz', timeout=4)"
CMD ["python", "-m", "ferry"]
