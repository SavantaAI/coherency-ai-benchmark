#!/bin/sh
set -eu

SETTINGS_SEED="/etc/t3-settings.json"
STATE_DIR="${T3_BASE_DIR}/userdata"

if [ "$#" -gt 0 ]; then
  exec "$@"
fi

if [ -d /tmp/.w/project ]; then
  cd /
  rm -rf /work/project
  mv /tmp/.w/project /work/project
  rmdir /tmp/.w
  cd /work/project
fi

mkdir -p "$STATE_DIR"
if [ ! -f "$STATE_DIR/settings.json" ] && [ -f "$SETTINGS_SEED" ]; then
  cp "$SETTINGS_SEED" "$STATE_DIR/settings.json"
fi

t3 serve \
  --host 127.0.0.1 \
  --port "$T3_PORT" \
  --base-dir "$T3_BASE_DIR" \
  /work/project &
wait $! || true

while :; do
  sleep 3600
done
