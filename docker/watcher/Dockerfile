# Filesystem watcher: runs beside the agent's container, never inside it.
# Standard library only. See cohbench/watcher/README.md for the docker run
# command; in short: the workspace volume read-only at /srv/w/live, the output
# directory at /srv/w/out, no network, every capability dropped except SETUID
# and SETGID, read-only root filesystem, a tmpfs at /tmp.
#
# Build from the repository root:
#   docker build -f docker/watcher/Dockerfile -t <tag> .
#
# Users. The entry point starts as root only long enough to become two
# unprivileged users (cohbench/watcher/isolate.py):
#   reader  1000:1000  the watcher. The agent writes the workspace as uid 1000
#                      (`node`), with files that may be mode 600 inside mode
#                      700 directories; only the owner can read those, and as
#                      the owner it can also open them with O_NOATIME.
#   metric  1500:1500  runs the task's metric command on a copy of one batch,
#                      built at /work/project (a link to its private /tmp/m),
#                      so absolute links into the workspace land in the copy.
# /srv/w is mode 0700 and owned by reader, so the metric user can neither read
# nor traverse the live workspace or the output.
#
# The workspace mount point /srv/w/live is an empty directory, and the volume
# must be mounted with volume-nocopy: Docker copies an image directory's
# contents and ownership onto an empty named volume mounted over it, which
# would be a write into the workspace on the watcher's behalf.
FROM python:3.12.16-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 1000 reader \
 && useradd --uid 1000 --gid 1000 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin reader \
 && groupadd --gid 1500 metric \
 && useradd --uid 1500 --gid 1500 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin metric \
 && mkdir -p /srv/w/live /srv/w/out /work \
 && chown 1000:1000 /srv/w /srv/w/out \
 && chmod 0700 /srv/w \
 && ln -s /tmp/m/project /work/project

WORKDIR /app
COPY cohbench/__init__.py /app/cohbench/__init__.py
COPY cohbench/watcher/*.py /app/cohbench/watcher/

# No HEALTHCHECK: a health check makes Docker exec into the container on a
# timer, which on Docker Desktop walks the workspace volume. The launcher
# reads /srv/w/out/heartbeat.json from its side of the output bind instead.
ENTRYPOINT ["python", "-m", "cohbench.watcher"]
CMD ["run", "--root", "/srv/w/live", "--out", "/srv/w/out"]
