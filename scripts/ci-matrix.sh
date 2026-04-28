#!/usr/bin/env bash
# Local CI matrix — runs the standard CI bar (lint/format/ty + current-py
# pytest + coverage) once, then runs pytest across the supported Python
# matrix via tox. Use before pushing changes that could be Python-version
# sensitive (typing, stdlib behavior, async semantics).
#
# Usage: scripts/ci-matrix.sh

set -euo pipefail

cd "$(dirname "$0")/.."

step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

step "ci: standard bar (current python)"
scripts/ci.sh

step "ci: pytest matrix (3.10 / 3.11 / 3.12 / 3.13)"
uv run --extra matrix tox

printf '\n\033[1;32m==> ci-matrix: ok\033[0m\n'
