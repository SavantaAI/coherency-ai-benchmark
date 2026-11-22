#!/bin/sh
# Run the watcher tests that need real inotify, inside a Linux container.
#
#   tests/watcher/run_linux.sh [pytest args...]
#
# Builds the test image (tests/watcher/linux.Dockerfile), then runs the tests
# (all of tests/watcher, or the test paths given) as uid 1000 with the repository mounted read-only. On
# Linux the `linux_inotify` tests are never skipped, so a broken inotify
# fails the run. Exit status is pytest's.
#
# IMAGE overrides the image tag (default wdev-watcher-test:dev).
set -eu
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
image=${IMAGE:-wdev-watcher-test:dev}

docker build -q -f "$here/linux.Dockerfile" -t "$image" "$here" >/dev/null

# Default target: the whole watcher suite, unless a test path is given.
target=tests/watcher
for a in "$@"; do
  case "$a" in tests/*) target= ;; esac
done

docker run --rm \
  --name "wdev-wtest-$$" \
  --network none \
  --security-opt seccomp=builtin \
  --security-opt no-new-privileges \
  --cap-drop ALL \
  --user 1000:1000 \
  --mount "type=bind,src=$repo,dst=/src,readonly" \
  --tmpfs /tmp:rw,exec,size=1g \
  -e HOME=/tmp \
  -e WATCHER_PROPERTY_SEEDS -e WATCHER_PROPERTY_SEED_START -e WATCHER_PROPERTY_BURSTS -e WATCHER_DEBUG_ROUNDS \
  "$image" \
  python -m pytest -p no:cacheprovider -q $target "$@"

# A separate container gives the metric server a dedicated uid. Its cleanup
# must not run as the shared user of the host tests or the main pytest run.
docker run --rm \
  --name "wdev-wstorage-$$" \
  --read-only \
  --network none \
  --security-opt seccomp=builtin \
  --security-opt no-new-privileges \
  --cap-drop ALL --cap-add SETUID --cap-add SETGID \
  --user 0:0 \
  --mount "type=bind,src=$repo,dst=/src,readonly" \
  --tmpfs /tmp:rw,exec,size=1g \
  "$image" python -m pytest -p no:cacheprovider -q tests/watcher/test_production_storage.py
