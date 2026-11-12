#!/usr/bin/env bash
# Trackie Assistant add-on (legacy slug suno_dl): webui + scheduled sync loop.
#
# Profiles are edited in the add-on's Options panel (a list of handles).
# Audio lands directly in /media/suno/<profile>/ where Music Assistant's
# filesystem provider reads it — same disk, no copies.
set -euo pipefail

ARCHIVE=/data/archive
MEDIA=/media/suno
OPTIONS=/data/options.json

# ── options.json -> environment (no bashio dependency) ────────────────
# Snapshot once so validation and every derived setting use identical bytes.
# JSON escapes controls and is one line; clear the variable before children run.
TRACKIE_ADDON_OPTIONS_SNAPSHOT="$(python3 -c 'import json; print(json.dumps(json.load(open("/data/options.json")), ensure_ascii=True, separators=(",", ":")))')"
export TRACKIE_ADDON_OPTIONS_SNAPSHOT
# Remote mode is an ingress client only: exit into the proxy before creating
# an archive, writing dotenv settings, or starting any local sync loop.
BACKEND_MODE="$(python3 -c '
import json, os, sys
mode = json.loads(os.environ["TRACKIE_ADDON_OPTIONS_SNAPSHOT"]).get("backend_mode", "local")
if mode not in ("local", "remote"):
    sys.exit("Invalid backend mode")
print(mode)
')"
if [ "$BACKEND_MODE" = "remote" ]; then
  unset TRACKIE_ADDON_OPTIONS_SNAPSHOT
  exec python3 -m suno_dl.ha_backend_proxy --options "$OPTIONS"
fi

# SUNO_WATCH is comma-delimited and is also stored in a dotenv file. Reject
# control characters, delimiters and malformed URLs before emitting either.
PROFILES="$(python3 -c '
import json, re, sys
entries = json.loads(__import__("os").environ["TRACKIE_ADDON_OPTIONS_SNAPSHOT"])["profiles"]
handle = r"[A-Za-z0-9_][A-Za-z0-9_.-]*"
entry = re.compile(r"(?:@?" + handle + r"|https://suno\.com/(?:@" + handle + r"|(?:song|playlist)/[A-Za-z0-9_-]+)/?)", re.ASCII)
if not isinstance(entries, list) or any(
    not isinstance(value, str) or len(value) > 256 or not entry.fullmatch(value)
    for value in entries
):
    print("Invalid profile option: use handles or public https://suno.com profile, playlist, or song URLs", file=sys.stderr)
    sys.exit(1)
print(",".join(entries))
')"
# Validate raw optional text before command substitution strips newlines.
python3 -c '
import json, sys
options = json.loads(__import__("os").environ["TRACKIE_ADDON_OPTIONS_SNAPSHOT"])
for key in ("sync_on_start", "audio_only", "download_videos", "covers", "rotate"):
    if key in options and type(options[key]) is not bool:
        print("Invalid " + key + " option: expected a boolean", file=sys.stderr)
        sys.exit(1)
for key, lower, upper in (("sync_interval_minutes", 15, 1440), ("max_tracks", 0, 100000),
                          ("max_disk_mb", 0, 1000000), ("min_free_mb", 0, None)):
    if key in options:
        value = options[key]
        if type(value) is not int or value < lower or (upper is not None and value > upper):
            print("Invalid " + key + " option: integer outside allowed range", file=sys.stderr)
            sys.exit(1)
for key in ("jev_url", "session_cookie", "model_version"):
    value = options.get(key)
    if value is not None and (not isinstance(value, str) or any(ord(ch) < 32 or ord(ch) == 127 for ch in value) or "${" in value):
        print("Invalid " + key + " option: control characters and dotenv interpolation are not allowed", file=sys.stderr)
        sys.exit(1)
'
# Traverse from the root without following links; only the final archive
# directory may be created. This checks existing components only: subsequent
# processes reopen the path. The archive and all parents must remain exclusively
# controlled and must not be replaced while the add-on is running.
python3 - "$ARCHIVE" <<'PYTHON'
import os, pathlib, sys
if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
    sys.exit("Cannot safely verify archive directory")
parts = pathlib.Path(sys.argv[1]).parts
if not parts or parts[0] != "/" or len(parts) < 2:
    sys.exit("Archive directory must be an absolute path")
flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
fd = os.open("/", flags)
try:
    for index, part in enumerate(parts[1:]):
        try:
            child = os.open(part, flags, dir_fd=fd)
        except FileNotFoundError:
            if index != len(parts) - 2:
                raise
            os.mkdir(part, mode=0o700, dir_fd=fd)
            child = os.open(part, flags, dir_fd=fd)
        os.close(fd)
        fd = child
except OSError as exc:
    raise SystemExit("Cannot safely open archive directory") from exc
finally:
    os.close(fd)
PYTHON
mkdir -p "$MEDIA"
SYNC_ON_START="$(python3 -c 'import json; print(int(json.loads(__import__("os").environ["TRACKIE_ADDON_OPTIONS_SNAPSHOT"]).get("sync_on_start", False)))')"
INTERVAL_MIN="$(python3 -c 'import json; print(int(json.loads(__import__("os").environ["TRACKIE_ADDON_OPTIONS_SNAPSHOT"]).get("sync_interval_minutes", 360)))')"
# Videos OFF / covers ON by default (#85). Legacy audio_only=true means no videos.
eval "$(python3 -c '
import json
o = json.loads(__import__("os").environ["TRACKIE_ADDON_OPTIONS_SNAPSHOT"])
if "download_videos" in o:
    videos = bool(o["download_videos"])
elif "audio_only" in o:
    videos = not bool(o["audio_only"])
else:
    videos = False
covers = bool(o.get("covers", True))
print("DOWNLOAD_VIDEOS=%d" % int(videos))
print("COVERS=%d" % int(covers))
')"
JEV_URL="$(python3 -c 'import json; print(json.loads(__import__("os").environ["TRACKIE_ADDON_OPTIONS_SNAPSHOT"]).get("jev_url") or "")')"
COOKIE="$(python3 -c 'import json; print(json.loads(__import__("os").environ["TRACKIE_ADDON_OPTIONS_SNAPSHOT"]).get("session_cookie") or "")')"
# Rolling library caps (#93): 0 = off, and rotate only prunes with a cap set.
MAX_TRACKS="$(python3 -c 'import json; print(int(json.loads(__import__("os").environ["TRACKIE_ADDON_OPTIONS_SNAPSHOT"]).get("max_tracks", 0) or 0))')"
MAX_DISK_MB="$(python3 -c 'import json; print(int(json.loads(__import__("os").environ["TRACKIE_ADDON_OPTIONS_SNAPSHOT"]).get("max_disk_mb", 0) or 0))')"
MIN_FREE_MB="$(python3 -c 'import json; print(int(json.loads(__import__("os").environ["TRACKIE_ADDON_OPTIONS_SNAPSHOT"]).get("min_free_mb", 2048)))')"
ROTATE="$(python3 -c 'import json; print(int(bool(json.loads(__import__("os").environ["TRACKIE_ADDON_OPTIONS_SNAPSHOT"]).get("rotate", False))))')"
MODEL_VERSION="$(python3 -c 'import json; print(json.loads(__import__("os").environ["TRACKIE_ADDON_OPTIONS_SNAPSHOT"]).get("model_version") or "")')"
unset TRACKIE_ADDON_OPTIONS_SNAPSHOT

# Supervisor-managed settings are authoritative, including empty optional values.
export SUNO_OUTPUT_DIR="$MEDIA" SUNO_WATCH="$PROFILES"
export SUNO_DOWNLOAD_VIDEOS="$DOWNLOAD_VIDEOS" SUNO_COVERS="$COVERS"
export SUNO_MAX_TRACKS="$MAX_TRACKS" SUNO_MAX_DISK_MB="$MAX_DISK_MB"
export SUNO_MIN_FREE_MB="$MIN_FREE_MB" SUNO_ROTATE="$ROTATE"
export SUNO_JEV_URL="$JEV_URL" SUNO_SESSION_COOKIE="$COOKIE" SUNO_MODEL_VERSION="$MODEL_VERSION"

# Create or verify the blocklist without following a link or changing an
# existing file. Special files and multiply linked files require review.
python3 - "$ARCHIVE/.blocked" <<'PYTHON'
import os, stat, sys
if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_NONBLOCK"):
    sys.exit("Cannot safely verify archive blocklist")
try:
    fd = os.open(sys.argv[1], os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
except OSError as exc:
    raise SystemExit("Cannot safely open archive blocklist") from exc
try:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        sys.exit("Archive blocklist must be a regular single-link file")
finally:
    os.close(fd)
PYTHON

# Write quoted dotenv values without interpolation or line injection. The
# file always has private permissions, whether or not a cookie is configured.
python3 - "$ARCHIVE/.env" <<'PYTHON'
import os, sys, tempfile
keys = ("SUNO_OUTPUT_DIR", "SUNO_WATCH", "SUNO_DOWNLOAD_VIDEOS", "SUNO_COVERS",
        "SUNO_MAX_TRACKS", "SUNO_MAX_DISK_MB", "SUNO_MIN_FREE_MB", "SUNO_ROTATE",
        "SUNO_JEV_URL", "SUNO_SESSION_COOKIE", "SUNO_MODEL_VERSION")
target = sys.argv[1]
if os.path.islink(target):
    sys.exit("Archive configuration must not be a symlink")
fd, temporary = tempfile.mkstemp(prefix=".env-", dir=os.path.dirname(target))
try:
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)
        for key in keys:
            value = os.environ[key].replace("\\", "\\\\").replace("'", "\\'")
            stream.write(f"{key}='{value}'\n")
    os.replace(temporary, target)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PYTHON

SYNC_FLAGS=(--sync --output-dir "$MEDIA" --extract-audio)
if [ "$DOWNLOAD_VIDEOS" = "1" ]; then
  SYNC_FLAGS+=(--videos)
else
  SYNC_FLAGS+=(--no-videos)
fi
if [ "$COVERS" = "1" ]; then
  SYNC_FLAGS+=(--covers)
else
  SYNC_FLAGS+=(--no-covers)
fi
# Optional: only keep tracks from one model version (e.g. v6); empty = all.
[ -n "$MODEL_VERSION" ] && SYNC_FLAGS+=(--model-version "$MODEL_VERSION")
# Caps are opt-in: the flags are only passed when set, so the default
# invocation is unchanged.
if [ "$MAX_TRACKS" -gt 0 ]; then
  SYNC_FLAGS+=(--max-tracks "$MAX_TRACKS")
fi
if [ "$MAX_DISK_MB" -gt 0 ]; then
  SYNC_FLAGS+=(--max-disk-mb "$MAX_DISK_MB")
fi
if [ "$ROTATE" = "1" ]; then
  SYNC_FLAGS+=(--rotate)
fi

run_sync() {
  echo "[addon] sync starting: profiles=$PROFILES download_videos=$DOWNLOAD_VIDEOS covers=$COVERS model_version=${MODEL_VERSION:-all} max_tracks=$MAX_TRACKS max_disk_mb=$MAX_DISK_MB rotate=$ROTATE"
  cd "$ARCHIVE"
  # Profiles come from SUNO_WATCH (written into the archive .env above).
  suno-dl "${SYNC_FLAGS[@]}" \
    || echo "[addon] sync failed (will retry on schedule)"
}

# ── web UI (ingress mode: HA's proxy is the trust boundary) ───────────
# The CLI lives only inside the image (vendored wheel). The web UI binds on
# the internal add-on network for HA ingress; no host port is published.
# Log the CLI version once at startup so the installed wheel is auditable.
echo "[addon] cli: $(suno-dl --version 2>&1 || echo 'version unknown')"
suno-webui --host 0.0.0.0 --port 8760 --archive "$ARCHIVE" --ingress &
WEBUI_PID=$!
echo "[addon] webui up on 8760 (ingress)"

# Initial sync shortly after start so the panel has data fast.
if [ "$SYNC_ON_START" = "1" ]; then
  sleep 3
  run_sync
fi

INTERVAL=$(( INTERVAL_MIN * 60 ))
(
  while true; do
    sleep "$INTERVAL"
    run_sync
  done
) &
LOOP_PID=$!

term() {
  echo "[addon] stopping"
  kill "$WEBUI_PID" "$LOOP_PID" 2>/dev/null || true
}
trap term TERM INT
wait "$WEBUI_PID"
