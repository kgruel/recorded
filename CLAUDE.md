# Notes for Claude

## Before declaring a task complete

Run `scripts/ci.sh` (ruff, ruff format, ty, pytest with coverage). It
fails fast on `set -euo pipefail` and is the source of truth for the
CI bar. Treat a non-zero exit as the task not being done.

For changes that could be Python-version sensitive (typing, stdlib
behavior, async semantics), also run `scripts/ci-matrix.sh` to
exercise the 3.10–3.13 pytest matrix via tox.
