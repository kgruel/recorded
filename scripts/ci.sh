#!/usr/bin/env bash
# Local CI — same checks GitHub Actions runs.
# Usage: scripts/ci.sh

set -euo pipefail

cd "$(dirname "$0")/.."

step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

step "ruff check"
uv run ruff check src tests

step "ruff format --check"
uv run ruff format --check src tests

step "ty check (baseline: ${TY_BASELINE:=3})"
ty_output=$(uv run ty check src 2>&1 || true)
echo "$ty_output"
ty_count=$(printf '%s\n' "$ty_output" | sed -n 's/^Found \([0-9][0-9]*\) diagnostics.*/\1/p' | tail -1)
ty_count=${ty_count:-0}
if [ "$ty_count" -gt "$TY_BASELINE" ]; then
  printf '\n\033[1;31m==> ty: %s diagnostics, baseline is %s — regression\033[0m\n' "$ty_count" "$TY_BASELINE" >&2
  exit 1
fi
if [ "$ty_count" -lt "$TY_BASELINE" ]; then
  printf '\n\033[1;33m==> ty: %s diagnostics, baseline is %s — lower the baseline in scripts/ci.sh\033[0m\n' "$ty_count" "$TY_BASELINE" >&2
fi

step "pytest"
uv run pytest -q

printf '\n\033[1;32m==> ci: ok\033[0m\n'
