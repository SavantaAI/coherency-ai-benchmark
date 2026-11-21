# Model API gateway: holds the API key and is the agent container's only
# route to the model APIs. Standard library only. Transparent mode also needs
# the openssl command, which the base image already ships.
#
# Build from the repository root:
#   docker build -f docker/gateway/Dockerfile -t gateway:dev .
FROM python:3.12.16-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --system --gid 10001 gateway \
 && useradd --system --uid 10001 --gid gateway --no-create-home --shell /usr/sbin/nologin gateway \
 && mkdir -p /var/log/gateway /run/secrets /run/gateway-tls \
 && chown gateway:gateway /var/log/gateway /run/gateway-tls \
 && chmod 0700 /var/log/gateway \
 && chmod 0700 /run/gateway-tls \
 && openssl version

WORKDIR /app
# An explicit list: only the modules the gateway runs. The host-side
# topology check (verify_topology.py) and the README stay out of the image.
COPY gateway/__init__.py gateway/__main__.py gateway/config.py gateway/config.json \
     gateway/proxy.py gateway/records.py gateway/screen.py gateway/summary.py gateway/tls.py gateway/usage.py \
     /app/gateway/

USER gateway
# 8080 is loopback-only health unless path-prefix mode is opted in.
EXPOSE 443
VOLUME ["/var/log/gateway"]
HEALTHCHECK --interval=10s --timeout=3s --start-period=2s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).status == 200 else 1)"]
ENTRYPOINT ["python", "-m", "gateway"]
