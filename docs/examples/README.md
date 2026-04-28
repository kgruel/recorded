# Examples

Three runnable scripts. Each is self-contained — `pip install`, run,
watch the rows land in `./jobs.db`.

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
