# Test image for the watcher's Linux tests: Python 3.12, pytest and git.
# The repository is mounted read-only at /src at run time; nothing is copied.
#   tests/watcher/run_linux.sh builds and runs it.
FROM python:3.12.16-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir pytest==9.2.0 \
 && groupadd --gid 1000 dev \
 && useradd --uid 1000 --gid 1000 --create-home --shell /bin/sh dev

USER 1000:1000
WORKDIR /src
