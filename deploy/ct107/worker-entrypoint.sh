#!/usr/bin/env bash
# CT107 worker: the only thing allowed to run a real sync.
#
# TRACKIE_REAL_SYNC_APPROVED is an approval gate, not a bug:
#   * unset/0 -> standby. The worker logs that sync is disabled, keeps its
#     heartbeat fresh, and never contacts Suno. (#136 read a long
#     health=starting worker as a fault; it was this standby.)
#   * 1       -> run one sync immediately, then every
#     TRACKIE_SYNC_INTERVAL_MINUTES.
#
# Rotation stays off unless the operator sets SUNO_ROTATE=1 *and* rotation has
# been qualified for this host (#144/#145). This script never passes --rotate.
set -euo pipefail

ARCHIVE="${TRACKIE_ARCHIVE:-/data/archive}"
MEDIA="${TRACKIE_MEDIA:-/media/suno}"
INTERVAL_MIN="${TRACKIE_SYNC_INTERVAL_MINUTES:-360}"
HEARTBEAT=/tmp/worker-heartbeat
APPROVED="${TRACKIE_REAL_SYNC_APPROVED:-0}"

mkdir -p "$ARCHIVE" "$MEDIA"
touch "$HEARTBEAT"

# The CLI reads its profiles/output dir from the archive .env. Create an empty
# one only when the host has none: an approved sync then fails with a clear
# "no profiles configured" instead of an unexplained missing file.
if [ ! -f "$ARCHIVE/.env" ]; then
  echo "[ct107-worker] no .env in $ARCHIVE; creating an empty one"
  : > "$ARCHIVE/.env"
  chmod 600 "$ARCHIVE/.env" 2>/dev/null || true
fi

echo "[ct107-worker] cli: $(suno-dl --version 2>&1 || echo 'version unknown')"
echo "[ct107-worker] archive=$ARCHIVE media=$MEDIA interval=${INTERVAL_MIN}m real_sync_approved=$APPROVED"

run_sync() {
  echo "[ct107-worker] sync starting at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  cd "$ARCHIVE"
  # --no-browser unless the image was built with INSTALL_BROWSER=1: latent
  # browser fallback would fail late and noisily without Chromium.
  EXTRA=()
  if [ -z "${TRACKIE_BROWSER_INSTALLED:-}" ]; then
    EXTRA+=(--no-browser)
  fi
  # An empty style preference means "all models"; never pass an empty flag.
  if [ -n "${TRACKIE_MODEL_VERSION:-}" ]; then
    EXTRA+=(--model-version "$TRACKIE_MODEL_VERSION")
  fi
  suno-dl --sync --output-dir "$MEDIA" --extract-audio --covers --no-videos \
    "${EXTRA[@]}" \
    || echo "[ct107-worker] sync failed; the next interval retries"
}

while true; do
  touch "$HEARTBEAT"
  if [ "$APPROVED" = "1" ] || [ "${TRACKIE_REAL_SYNC_APPROVED:-0}" = "1" ]; then
    run_sync
  else
    echo "[ct107-worker] sync disabled (TRACKIE_REAL_SYNC_APPROVED=$APPROVED); standby"
  fi
  # Sleep in the background and wait so SIGTERM interrupts immediately instead
  # of holding the container open for up to one full interval.
  sleep $(( INTERVAL_MIN * 60 )) &
  wait $!
done
