"""`python -m recorded` entry point. See `_cli.py` for subcommand handlers."""

from __future__ import annotations

import sys

from ._cli import main

if __name__ == "__main__":
    sys.exit(main())
