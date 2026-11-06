#!/usr/bin/env bash
# Local reproduction of .github/workflows/ci.yml.
#
# GitHub Actions cannot run on this account (Actions budget), so this script
# performs the identical gate against a pristine clone of the repo:
#   - Python 3.13 (whatever 3.13 interpreter is available)
#   - ruff check, ruff format --check, mypy
#   - pytest with coverage floor
#   - python -m build + install wheel in a clean venv + console-script smoke
#
# Usage: ci-local.sh [ref]   (default: master)
# Exit:  0 all green, non-zero on first failure (details in the log).
#
# Every manual run uses ONE dedicated scratch worktree, regardless of ref.
# A global lock forbids overlap, and unsafe paths are rejected before deletion.
# The harness mirrors the Python 3.13-only support policy.
# Linux-only helper: requires Python 3, /proc/self/mountinfo, util-linux
# flock, and GNU rm (no external mount command or privileged operations).
set -uo pipefail

REPO_URL="${CI_REPO_URL:-https://github.com/matthewhand/trackie-assistant.git}"
REF="${1:-master}"
CI_ROOT="$HOME/.local/state/suno-ci-local"
WORK="${CI_WORKDIR:-$CI_ROOT/work}"
LOGDIR="${CI_LOGDIR:-$CI_ROOT/logs}"
PYTHONS="${CI_PYTHONS:-3.13}"

# Whitelist the ONLY deletion target before any user-supplied CI_WORKDIR is used.
case "$WORK" in
  "$CI_ROOT/work") ;;
  *) echo "FATAL: CI_WORKDIR must be the dedicated Suno CI scratch path" >&2; exit 78 ;;
esac
# Guard each path component against symlink redirection.
for dir in "$HOME" "$HOME/.local" "$HOME/.local/state" "$CI_ROOT" "$WORK"; do
  if [ -L "$dir" ]; then
    echo "FATAL: CI scratch path contains a symlink: $dir" >&2; exit 78
  fi
done
if [ -e "$WORK" ] && [ ! -d "$WORK" ]; then
  echo "FATAL: CI workspace exists but is not a directory" >&2; exit 78
fi
# Enumerate the complete kernel mount table, including child mounts below
# ordinary (non-mountpoint) work directories. Fail closed on parse/read errors.
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd -P)" || exit 4
MOUNT_GUARD="$SCRIPT_DIR/ci-mount-guard.py"
LOG_GUARD="$SCRIPT_DIR/ci-logdir-guard.py"
command -v python3 >/dev/null 2>&1 || {
  echo "FATAL: python3 required to inspect CI scratch mounts" >&2; exit 4;
}
python3 "$MOUNT_GUARD" "$WORK" || exit $?
python3 "$LOG_GUARD" "$WORK" "$LOGDIR" || exit $?

# No caller-controlled env marker may bypass the global lock.
# The flock supervisor keeps the lock, while -o closes its FD in the worker
# so spawned test helpers cannot accidentally hold the lock past CI completion.
mkdir -p "$CI_ROOT"
LOCKFILE="$CI_ROOT/.ci-local.lock"
if [ -L "$LOCKFILE" ] || { [ -e "$LOCKFILE" ] && [ ! -f "$LOCKFILE" ]; }; then
  echo "FATAL: CI lock must be a regular non-symlink file" >&2; exit 78
fi
command -v flock >/dev/null 2>&1 || {
  echo "FATAL: flock is required for exclusive CI runs" >&2; exit 4;
}

# A separate, unconditionally locked worker prevents forgeable reentry flags.
ci_run() {
  REPO_URL="$1"
  REF="$2"
  WORK="$3"
  LOGDIR="$4"
  PYTHONS="$5"
  MOUNT_GUARD="$6"
  LOG_GUARD="$7"
  python3 "$LOG_GUARD" "$WORK" "$LOGDIR" || exit $?
  mkdir -p "$LOGDIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG="$LOGDIR/run-$STAMP.log"

exec > >(tee -a "$LOG") 2>&1
echo "=== suno-download-cli local CI @ $STAMP (ref=$REF) ==="

if ! command -v uv >/dev/null 2>&1; then
  echo "FATAL: 'uv' not on PATH ($PATH)"; exit 4
fi

FAIL=0
note() { printf '\n----- %s -----\n' "$1"; }

# --- fresh checkout (mirrors actions/checkout) -------------------------------
note "checkout"
# Recheck inside the lock immediately before deleting the disposable clone.
# This does not make concurrent same-user pathname swaps atomic.
python3 "$MOUNT_GUARD" "$WORK" || exit $?
rm -rf --one-file-system -- "$WORK"
git clone --quiet --branch "$REF" --single-branch "$REPO_URL" "$WORK" || {
  echo "CHECKOUT FAILED"; exit 3;
}
cd "$WORK"
SHA="$(git rev-parse --short HEAD)"
echo "commit: $SHA"

# --- per-Python matrix --------------------------------------------------------
for PY in $PYTHONS; do
  if ! uv python find "$PY" >/dev/null 2>&1; then
    echo "python $PY: SKIP (not installed)"
    continue
  fi
  note "matrix python $PY"
  VENV="$WORK/.ci-venv-$PY"
  uv venv --python "$PY" --quiet "$VENV" || { echo "venv $PY FAILED"; FAIL=1; continue; }
  PY_BIN="$VENV/bin/python"
  uv pip install --python "$PY_BIN" --quiet -e ".[dev]" || { echo "install $PY FAILED"; FAIL=1; continue; }

  if "$PY_BIN" -m ruff check src tests; then echo "ruff check: OK"; else echo "ruff check: FAIL"; FAIL=1; fi
  if "$PY_BIN" -m ruff format --check src tests; then echo "ruff format: OK"; else echo "ruff format: FAIL"; FAIL=1; fi
  if "$PY_BIN" -m mypy src; then echo "mypy: OK"; else echo "mypy: FAIL"; FAIL=1; fi
  if "$PY_BIN" -m pytest -q --cov=suno_dl --cov-report=term-missing --cov-fail-under=70; then
    echo "pytest: OK"
  else
    echo "pytest: FAIL"; FAIL=1
  fi
done

# --- build job ---------------------------------------------------------------
note "build + wheel smoke"
BUILD_PY="${CI_BUILD_PYTHON:-3.13}"
if ! uv python find "$BUILD_PY" >/dev/null 2>&1; then
  BUILD_PY="$(uv python find 3.13 2>/dev/null || true)"
fi
echo "build python: ${BUILD_PY:-<none found>}"
BUILD_VENV="$WORK/.ci-venv-build"
uv venv --python "$BUILD_PY" --quiet "$BUILD_VENV" || FAIL=1
BVP="$BUILD_VENV/bin/python"
uv pip install --python "$BVP" --quiet build setuptools wheel || FAIL=1
rm -rf "$WORK/dist" "$WORK/build"
if "$BVP" -m build --no-isolation >/dev/null 2>&1; then
  echo "build: OK"
else
  echo "build: FAIL"; FAIL=1
fi
WHEEL=$(ls "$WORK"/dist/*.whl 2>/dev/null | head -1 || true)
if [ -n "$WHEEL" ]; then
  RUN_VENV="$WORK/.ci-venv-run"
  uv venv --python "$BUILD_PY" --quiet "$RUN_VENV" || FAIL=1
  uv pip install --python "$RUN_VENV/bin/python" --quiet "$WHEEL" || { echo "wheel install: FAIL"; FAIL=1; }
  if "$RUN_VENV/bin/python" -m suno_dl.cli --version && \
     "$RUN_VENV/bin/python" -m suno_dl.cli --help >/dev/null; then
    echo "wheel smoke: OK"
  else
    echo "wheel smoke: FAIL"; FAIL=1
  fi
else
  echo "no wheel produced"; FAIL=1
fi

# --- summary -----------------------------------------------------------------
note "summary"
if [ "$FAIL" -eq 0 ]; then
  echo "RESULT: GREEN ($SHA)"
  echo "GREEN $STAMP ref=$REF sha=$SHA" >> "$LOGDIR/summary.txt"
else
  echo "RESULT: RED ($SHA) — see $LOG"
  echo "RED   $STAMP ref=$REF sha=$SHA" >> "$LOGDIR/summary.txt"
fi
exit "$FAIL"

}

# Bash function body comes only from this script, not environment variables.
# All arguments remain separate positional parameters; the worker never opens
# the lock file and cannot prolong its lifetime through child inheritance.
exec flock -n -o -E 75 "$LOCKFILE" bash -uo pipefail -c \
  "$(declare -f ci_run); ci_run \"\$@\"" ci-worker \
  "$REPO_URL" "$REF" "$WORK" "$LOGDIR" "$PYTHONS" "$MOUNT_GUARD" "$LOG_GUARD"
