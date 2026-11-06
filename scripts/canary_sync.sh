#!/usr/bin/env bash
# Live dry-run regression canary for suno-download-cli.
#
# Runs the real sync pipeline against the real archive in dry-run mode —
# discovery, parsing, blocklist, and dedup all execute; nothing is written
# and only page loads hit Suno (no CDN media requests). Catches the silent
# failure mode the offline suite cannot: Suno changing its page structure.
#
# Usage: scripts/canary_sync.sh [archive-dir]   (default ~/suno-archive)
#
# Exit codes: 0 = healthy, 1 = canary failed (see output), 2 = bad usage.
set -euo pipefail

ARCHIVE="${1:-$HOME/suno-archive}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ ! -f "$ARCHIVE/.env" ]]; then
    echo "canary: no .env in $ARCHIVE (pass the archive dir as the first argument)" >&2
    exit 2
fi

cd "$ARCHIVE"
OUTPUT="$(uv run --project "$REPO_ROOT" suno-dl --sync --extract-audio --dry-run 2>&1)" || {
    echo "canary: sync dry-run exited non-zero:" >&2
    echo "$OUTPUT" | tail -5 >&2
    exit 1
}

SUMMARY_LINE="$(echo "$OUTPUT" | grep -E '^Sync summary:' | tail -1)"
TOTAL="$(echo "$SUMMARY_LINE" | grep -oE 'total=[0-9]+' | cut -d= -f2)"
BLOCKED="$(echo "$SUMMARY_LINE" | grep -oE 'blocked=[0-9]+' | cut -d= -f2)"
FAILED="$(echo "$SUMMARY_LINE" | grep -oE 'failed=[0-9]+' | cut -d= -f2)"

echo "canary: $SUMMARY_LINE"

# Healthy = tracks are still being discovered (page structure intact), the
# blocklist is honored (no request ever reaches a blocked object), and no
# source failed outright (auth walls or page redesigns show up here).
if [[ "${TOTAL:-0}" -eq 0 ]]; then
    echo "canary: FAIL — 0 tracks discovered; Suno page structure likely changed" >&2
    exit 1
fi
if [[ "${FAILED:-1}" -ne 0 ]]; then
    echo "canary: FAIL — a sync source failed; check .env watch list and page layout" >&2
    exit 1
fi
if [[ "${BLOCKED:-0}" -eq 0 ]] && [[ -f "$ARCHIVE/.blocked" ]]; then
    echo "canary: FAIL — .blocked exists but blocked=0; blocklist wiring is broken" >&2
    exit 1
fi

echo "canary: OK — discovery, parsing, blocklist, and dry-run all healthy"
