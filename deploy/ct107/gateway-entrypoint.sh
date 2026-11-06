#!/usr/bin/env bash
# CT107 gateway: serve the WebUI. Never starts a sync by itself.
#
# The gateway is a read-mostly surface: the web panel can *request* a run, but
# on this deployment real syncing belongs to the worker and to an explicit
# operator decision (TRACKIE_REAL_SYNC_APPROVED on the worker). Both containers
# mount the same archive, so the archive lock serialises whatever runs.
set -euo pipefail

ARCHIVE="${TRACKIE_ARCHIVE:-/data/archive}"
MEDIA="${TRACKIE_MEDIA:-/media/suno}"
PORT="${TRACKIE_GATEWAY_PORT:-8760}"
# UI inventory and disk capacity must describe the same destination as the worker.
export SUNO_OUTPUT_DIR="$MEDIA"

mkdir -p "$ARCHIVE" "$MEDIA"

# suno-webui anchors its config on the archive .env and refuses to start
# without one. Create an empty, operator-owned file if the host has none, so a
# fresh deployment can come up; the real watch list and output dir belong in
# that file (or in this project's .env), not in the image.
if [ ! -f "$ARCHIVE/.env" ]; then
  echo "[ct107-gateway] no .env in $ARCHIVE; creating an empty one"
  : > "$ARCHIVE/.env"
  chmod 600 "$ARCHIVE/.env" 2>/dev/null || true
fi

# Log the installed CLI once so the running wheel is auditable from logs.
echo "[ct107-gateway] cli: $(suno-dl --version 2>&1 || echo 'version unknown')"
echo "[ct107-gateway] archive=$ARCHIVE media=$MEDIA port=$PORT"

INGRESS=()
# `--ingress` is Home Assistant-specific: it resolves the Supervisor's Docker
# DNS name and then refuses every other peer, so on a plain Compose host it
# cannot start at all. CT107 keeps the Host guard on; the TLS proxy rewrites
# Host to localhost (see RUNBOOK.md). Set TRACKIE_GATEWAY_INGRESS=1 only on an
# HA add-on, where the Supervisor really is the only peer.
if [ "${TRACKIE_GATEWAY_INGRESS:-0}" = "1" ]; then
  INGRESS+=(--ingress)
fi

cd "$ARCHIVE"
exec suno-webui \
  --host 0.0.0.0 \
  --port "$PORT" \
  --archive "$ARCHIVE" \
  "${INGRESS[@]}"
