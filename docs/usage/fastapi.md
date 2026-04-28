# FastAPI integration

Two pieces wire `recorded` into FastAPI cleanly: `Recorder` is an async
context manager (drops into a lifespan), and `recorded.fastapi.capture_request`
serializes a `Request` into a JSON-safe dict for the `request` slot.

`recorded.fastapi` is **stdlib-only** — the helper duck-types its argument
rather than importing FastAPI/Starlette. `fastapi` is in `[dev]` extras only.

> Working example: [`docs/examples/02_fastapi_app.py`](../examples/02_fastapi_app.py)

## Lifespan integration

`Recorder` implements `__aenter__` / `__aexit__`, and `recorded.configure(...)`
returns the configured Recorder, so an `async with` inside a FastAPI lifespan
binds the recorder's lifetime to the app:

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
import recorded

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with recorded.configure(path="/var/lib/app/jobs.db"):
        yield
    # connection closed on exit

app = FastAPI(lifespan=lifespan)
```

On enter: connection bootstrapped (which also runs the reaper sweep). On
exit: connection closes.

You can use `configure()` outside a lifespan too — `atexit` handles the
shutdown then. The lifespan pattern is preferred for production because
the cleanup runs during graceful shutdown.

> **`.submit()` requires a leader process.** If your endpoints call
> `fn.submit(req)` (rather than awaiting `fn(req)` directly inline), run
> `python -m recorded run --path <jobs.db> --import myapp.tasks` as a
> sibling process — typically under the same supervisor (systemd unit,
> Kubernetes Deployment, Docker Compose service). The FastAPI app
> process inserts pending rows; the leader process executes them. See
> [workers](workers.md). Without a leader, `.submit()` raises
> `ConfigurationError` on the first call.

## Recording outbound calls

The most common shape: an endpoint handler awaits a `@recorder`-decorated
function that calls a third-party API. Recording sits *inside* the handler;
nothing about FastAPI itself is special-cased.

```python
from pydantic import BaseModel
import httpx
from recorded import recorder

class GitHubUser(BaseModel):
    login: str
    public_repos: int
    followers: int

@recorder(kind="github.users.get", data=GitHubUser)
async def fetch_github_user(login: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"https://api.github.com/users/{login}")
        r.raise_for_status()
        return r.json()
```

In the endpoint:

```python
@app.get("/users/{login}")
async def get_user(login: str):
    data = await fetch_github_user(login, key=f"profile-{login}")
    return {"login": data["login"], "repos": data["public_repos"]}
```

`key=f"profile-{login}"` turns the second hit for the same login into a
cache hit — no network call, no rate limit consumed, returns the prior
response from disk. See [idempotency](idempotency.md) for the full story.

## Recording inbound requests with `capture_request`

When you want the inbound HTTP request itself in the `request` slot:

```python
from fastapi import FastAPI, Request
from recorded import recorder
from recorded.fastapi import capture_request

@recorder(kind="api.checkout")
async def handle_checkout(envelope: dict) -> dict:
    payload = envelope["body"]   # accessing the captured body
    ...

@app.post("/checkout")
async def checkout(request: Request):
    envelope = await capture_request(request)
    return await handle_checkout(envelope)
```

`capture_request(request)` returns:

```python
{
    "method":  "POST",
    "path":    "/checkout",
    "query":   {"foo": "bar"},   # request.query_params
    "headers": {"content-type": "application/json", ...},  # case-folded keys
    "body":    "{...}",           # utf-8 decoded text, or None if empty
}
```

It's `async` because Starlette's `request.body()` is async. Headers are
case-folded to lowercase (HTTP headers are case-insensitive; mixing cases
across requests would corrupt downstream queries on `request_json`).

`capture_request` is **duck-typed**. It checks for `method`, `url`, `headers`,
`query_params`, `body` attributes and raises `TypeError` if any are missing.
Anything that quacks like a Starlette `Request` works — Litestar, raw ASGI
helpers, custom shims, all welcome. The library never imports FastAPI or
Starlette.

## Two response shapes — kept separate

The recorder captures what hit the wire (full third-party response, exact
inbound request envelope). The endpoint's response shape is what you choose
to expose to clients. Keep them separate:

```python
class UserResponse(BaseModel):     # the API contract for /users/{login}
    login: str
    repos: int

class GitHubUser(BaseModel):       # the queryable projection in `data`
    login: str
    public_repos: int

@recorder(kind="github.users.get", data=GitHubUser)
async def fetch_github_user(login: str) -> dict:
    ...   # returns the raw GitHub dict — full audit faithfulness

@app.get("/users/{login}", response_model=UserResponse)
async def get_user(login: str) -> UserResponse:
    data = await fetch_github_user(login, key=f"profile-{login}")
    return UserResponse(login=data["login"], repos=data["public_repos"])
```

Both can be Pydantic without colliding. The decorator's `data=GitHubUser`
projects only the fields you'll grep on later (`recorded.query(where_data=
{"login": ...})` works); the full GitHub response stays in `response_json`.

## Exposing recorded history through an endpoint

```python
from recorded import last

class HistoryEntry(BaseModel):
    id: str
    key: str | None
    status: str
    duration_ms: int | None
    github: GitHubUser | None

@app.get("/history", response_model=list[HistoryEntry])
def history(n: int = 20) -> list[HistoryEntry]:
    return [
        HistoryEntry(
            id=j.id,
            key=j.key,
            status=j.status,
            duration_ms=j.duration_ms,
            github=j.data,   # already a GitHubUser instance (rehydrated)
        )
        for j in last(n, kind="github.users.*")
    ]
```

Because the decorator declared `data=GitHubUser`, `j.data` is already a
`GitHubUser` instance — no manual rehydration. FastAPI's response_model
validates the shape on the way out.

## Background work in handlers

For long-running endpoints, `.submit()` returns control to the client
immediately and lets the leader process do the actual work. **Run the
leader as a sibling process — the FastAPI app itself does not execute
submitted jobs.**

```python
# myapp/tasks.py — both the app process and the leader process import this
@recorder(kind="reports.build")
def build_report(spec): ...
```

```python
# Endpoint handlers (run in the FastAPI app process)
@app.post("/reports")
def create_report(spec: ReportSpec):
    handle = build_report.submit(spec, key=f"daily-{spec.date}")
    return {"job_id": handle.job_id, "status": "submitted"}

@app.get("/reports/{job_id}")
def get_report(job_id: str):
    job = recorded.get(job_id)
    if job is None:
        raise HTTPException(404)
    return {"status": job.status, "response": job.response}
```

```bash
# Leader (separate process, same DB path, same task module imported)
python -m recorded run --path /var/lib/myapp/jobs.db --import myapp.tasks
```

```python
# Optional health endpoint to surface leader presence to your supervisor
@app.get("/health/leader")
def leader_health():
    if not recorded.get_default().is_leader_running():
        raise HTTPException(503, "no recorded leader running")
    return {"ok": True}
```

See [workers](workers.md) for the full background-execution story.
