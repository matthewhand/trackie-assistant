#!/usr/bin/env bash
# Regenerate every diagram from its spec into docs/diagrams/, then validate.
set -euo pipefail
cd "$(dirname "$0")/.."

for s in docs/diagrams/specs/*.json; do
  name="$(basename "$s" .json)"
  out="docs/diagrams/${name}.html"
  case "$name" in
    *sequence*) .venv/bin/python scripts/sequence_gen.py "$s" --out "$out" ;;
    *state*)    .venv/bin/python scripts/state_gen.py    "$s" --out "$out" ;;
    *)          .venv/bin/python scripts/diagram_gen.py  "$s" --out "$out" ;;
  esac
done

python3 ~/.config/opencode/skills/diagram-design/scripts/self_check.py docs/diagrams/*.html
