# Examples

Five runnable scripts. Each is self-contained — `pip install`, run,
watch the rows land in `./jobs.db` (or the example's own DB).

If `recorded` reads to you as "for I/O / job-queue territory only,"
start with **`05_codebase_scan.py`** — it's the example where the
wrapped function is pure computation, and the recorded table is the
result space you query.

## `01_claude_turns.py` — record LLM-CLI turns

Demos `recorded` against the AI CLI you already have installed. Three
calls to `claude` (or `gemini`/`codex`) about this library's design
doc, then pulls the prior turns back out and threads them into a
follow-up question. The library demoing itself.

Shows: bare `@recorder` usage, `last(N, kind=...)` reads, the
LLM-threading pattern (record turns → query recent → paste back as
context).

```bash
pip install recorded   # plus your CLI of choice
python docs/examples/01_claude_turns.py
```

## `02_fastapi_app.py` — FastAPI service with audit history

A small FastAPI service wrapping the GitHub users API. One endpoint
fetches a user (cached per-login via `key=`); a second endpoint exposes
the recorded history with the typed `data` slot rehydrated as a Pydantic
model.

Shows: lifespan integration via `async with recorded.configure(...)`,
idempotency-as-cache with a key per resource, the two-shape pattern
(recorder slot vs API response model).

```bash
pip install recorded fastapi uvicorn httpx pydantic
uvicorn docs.examples.02_fastapi_app:app --reload --port 8000

curl http://localhost:8000/users/torvalds       # first hit — fetches
curl http://localhost:8000/users/torvalds       # second hit — cache hit
curl http://localhost:8000/history
```

## `03_api_consumer.py` — batch HTTP with a queryable cache

Fetches the top stories from Hacker News, captures each as a recorded
call, then runs aggregations over the recorded log via
`recorded.connection()`. Run twice — the second run does zero new HTTP
because the idempotency keys fold into the existing rows.

Shows: concurrent recorded calls, idempotency as a per-resource cache,
raw-SQL aggregations on the log.

```bash
pip install recorded httpx
python docs/examples/03_api_consumer.py
python docs/examples/03_api_consumer.py        # again — see idempotency
```

## `04_treasury_snapshot.py` — pulling it all together

Stitches three U.S. Treasury Fiscal Data endpoints (debt-to-the-penny,
average interest rates by security, FX rates per currency) into a single
`FiscalSnapshot` Pydantic model. Three `@recorder` functions, one per
endpoint, each with its own typed `data=` slot. `key=` is stamped with
today's date — second run same day is a free cache hit. The synthesis
beat at the end runs `recorded.query(kind="treasury.*", since=TODAY)`
and folds today's rows into the snapshot shape.

Shows: multiple typed slots side-by-side, `attach()` per call,
date-stamped idempotency keys for daily-refresh semantics, glob
`kind=` filters, the recorded log composing into a derived shape via
the read API. No inline comments — read the code.

```bash
pip install recorded httpx pydantic
python docs/examples/04_treasury_snapshot.py
python docs/examples/04_treasury_snapshot.py   # second run — zero HTTP
```

## `05_codebase_scan.py` — pure analysis, no I/O

Walks `src/recorded/` itself, parses each `.py` file's AST, projects
four metrics into a typed `data=` slot. Then queries the recorded
space — "files with async, ranked by LOC" is read out of SQLite, no
recompute. `key=path:mtime` makes the sweep incremental: re-run after
editing one file and only that file is re-measured.

Shows: the wrapped function as **pure computation** rather than I/O.
The recorded table is the result space; the read API is the analytical
surface. This is the example to read first if the others made the
library feel like ops/job-queue territory.

```bash
pip install recorded pydantic
python docs/examples/05_codebase_scan.py
python docs/examples/05_codebase_scan.py   # second run — zero parse work
```
