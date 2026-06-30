FROM ghcr.io/home-assistant/base:latest

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apk add --no-cache \
    openjdk17-jre-headless \
    python3 \
    unzip \
    jq \
    netcat-openbsd \
    coreutils

COPY run.sh /run.sh
COPY upload_server.py /upload_server.py
RUN chmod a+x /run.sh /upload_server.py && mkdir -p /data/cgate /data/packages

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD nc -z 127.0.0.1 8099 || exit 1

CMD ["/run.sh"]
