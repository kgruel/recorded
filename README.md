# recorded

Typed function-call recorder backed by SQLite. See `DESIGN.md` for the full design.

## Local development

We use [`uv`](https://github.com/astral-sh/uv) for environment management.

```bash
uv venv
uv pip install -e ".[dev]"
uv run pytest
```

`pip install -e ".[dev]"` works equivalently if `uv` is unavailable.

## Requirements

- Python 3.10+
- SQLite 3.35.0+ (for `UPDATE ... RETURNING`). The Python `sqlite3` stdlib module on
  most modern platforms qualifies; RHEL 8 / old Debian may need a SQLite update.

Pydantic is auto-detected at import time and used for typed slots when present;
it is never a hard dependency.
