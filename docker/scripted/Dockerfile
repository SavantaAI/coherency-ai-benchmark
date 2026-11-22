# Scripted stand-in for the model API (decision D-015). Standard library only.
#
# Build from the repository root:
#   docker build -f docker/scripted/Dockerfile -t sdev-scripted:dev .
#
# The API listens on 0.0.0.0:8080 for the gateway. The admin listener binds
# 127.0.0.1:8081 inside this container, so only `docker exec` from the host
# reaches it; see cohbench/scripted/README.md.
FROM python:3.12.16-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --system --gid 10002 app \
 && useradd --system --uid 10002 --gid app --no-create-home --shell /usr/sbin/nologin app \
 && mkdir -p /var/log/scripted /app/cohbench \
 && chown app:app /var/log/scripted \
 && chmod 0700 /var/log/scripted \
 && touch /app/cohbench/__init__.py

WORKDIR /app
# Only this package: the rest of cohbench belongs to other components.
COPY cohbench/scripted/__init__.py cohbench/scripted/__main__.py cohbench/scripted/engine.py \
     cohbench/scripted/ids.py cohbench/scripted/script.py cohbench/scripted/server.py \
     cohbench/scripted/wire.py \
     /app/cohbench/scripted/

USER app
EXPOSE 8080
VOLUME ["/var/log/scripted"]
HEALTHCHECK --interval=10s --timeout=3s --start-period=2s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8081/healthz', timeout=2).status == 200 else 1)"]
ENTRYPOINT ["python", "-m", "cohbench.scripted", "serve", "--api", "0.0.0.0:8080", "--admin", "127.0.0.1:8081", "--log-dir", "/var/log/scripted"]
