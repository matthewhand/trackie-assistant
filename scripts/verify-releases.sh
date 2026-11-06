#!/usr/bin/env bash
# verify-releases.sh — one command to verify all releases once the GitHub
# Actions budget resets. Encodes docs/release-verification.md: reruns every
# budget-blocked release/CI run, waits for completion, classifies each as
# BUDGET-STILL-BLOCKED (jobs never started) or executed (real result).
#
# Usage: bash scripts/verify-releases.sh
# Exit codes: 0 = all runs verified green, 1 = at least one real failure,
#             2 = everything still budget-blocked, 3 = mixed/incomplete.
set -u

# Run IDs from docs/release-verification.md (three release runs, two CI runs
# from the #44 handoff-note merge — the five runs affected by the outage).
RUNS=(
  "36296629295:release:v1.0.0"
  "36305586532:release:v1.1.0"
  "36307505930:release:v1.1.1"
  "36315479490:ci:#44"
  "36315486838:ci:#44"
)

budget=0
real_fail=0
green=0

echo "== Rerunning ${#RUNS[@]} budget-blocked runs =="

for entry in "${RUNS[@]}"; do
  id="${entry%%:*}"
  rest="${entry#*:}"
  kind="${rest%%:*}"
  label="${rest#*:}"

  echo "--- $kind run $id ($label)"
  if ! gh run rerun "$id" >/dev/null 2>&1; then
    echo "    rerun rejected — skipping to status check"
  fi

  # Give the run a moment to pick up, then wait for it to finish.
  sleep 5
  if ! gh run watch "$id" --exit-status --interval 10 >/dev/null 2>&1; then
    watch_rc=$?
    # Distinguish budget-blocked from real failure: a run that executed has
    # at least one started step on its non-skipped jobs.
    steps=$(gh run view "$id" --json jobs \
      --jq '[.jobs[] | select(.conclusion != "skipped")][0].steps | length' \
      2>/dev/null || echo "?")
    if [ "$steps" = "0" ]; then
      echo "    BUDGET-STILL-BLOCKED (0 steps executed)"
      budget=$((budget + 1))
    else
      echo "    EXECUTED AND FAILED (steps=$steps) — investigate logs:"
      echo "      gh run view $id --log-failed"
      real_fail=$((real_fail + 1))
    fi
    [ "$watch_rc" = "0" ] || true
  else
    echo "    GREEN"
    green=$((green + 1))
  fi
done

echo
echo "== Summary: $green green / $real_fail real-failure / $budget budget-blocked (of ${#RUNS[@]}) =="

if [ "$real_fail" -gt 0 ]; then
  exit 1
elif [ "$green" -eq "${#RUNS[@]}" ]; then
  echo "All release/CI runs verified. The Actions outage had no code impact."
  exit 0
elif [ "$budget" -eq "${#RUNS[@]}" ]; then
  echo "Budget not yet reset — re-run this script later."
  exit 2
else
  exit 3
fi
