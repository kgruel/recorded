"""
02 — FastAPI integration: recording outbound API calls in a real service

A small FastAPI service with two endpoints:

    GET /users/{login}      Fetches a GitHub user profile, recording the
                            outbound call. Uses key= so the second hit
                            for the same login is served from the log
                            without a network call.

    GET /history?n=20       Exposes the recorded calls as JSON, with the
                            typed `data` slot rehydrated as a Pydantic
                            model.

This is the natural FastAPI shape: an endpoint handler awaits a recorded
function, and the recorded function calls a third-party API. Recording
sits *inside* the handler; nothing about FastAPI itself is special-cased.

We use Pydantic for both the recorded function's `data=` projection and
the endpoint's response model — that's how FastAPI users already think.
The library auto-detects Pydantic via duck typing; it's never a hard
import.

Setup (60 seconds):
    pip install recorded fastapi uvicorn httpx pydantic
    cd /path/to/sqlite-api-job
    uvicorn docs.examples.02_fastapi_app:app --reload --port 8000

Try it:
    curl http://localhost:8000/users/torvalds
    curl http://localhost:8000/users/torvalds       # same call — cache hit
    curl http://localhost:8000/users/gvanrossum
    curl http://localhost:8000/history

    sqlite3 jobs.db "
        SELECT kind, key, status,
               json_extract(data_json, '$.login') AS login,
               json_extract(data_json, '$.public_repos') AS repos
        FROM jobs ORDER BY submitted_at;
    "

What's interesting about this example:

  - `data=GitHubUser` projects the four fields you'll filter on (login,
    name, public_repos, followers) into queryable JSON columns. The full
    GitHub response — many more fields, avatar URLs, etc. — is *still
    recorded* in response_json. `data` is just the slice you'll grep on
    later.

  - `key=f"profile-{login}"` makes the second curl free: no HTTP call,
    no rate limit consumed, returns the recorded response from disk.
    This is `key=` used as a cache (legitimate idempotency) — same
    primitive that protects against double-charging in a side-effecting
    integration.

  - The endpoint's response model (`UserResponse`) is independent of
    the recorder's slots. The recorder captures what hit the wire;
    your handler shapes the API contract you expose to clients. Both
    can be Pydantic without colliding.
"""


import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from recorded import last, recorder

# ---- the slot model: the queryable projection of the GitHub response ----


class GitHubUser(BaseModel):
    login: str
    name: str | None = None
    public_repos: int
    followers: int


# ---- the recorded outbound call ----


@recorder(kind="github.users.get", data=GitHubUser)
async def fetch_github_user(login: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(
            f"https://api.github.com/users/{login}",
            headers={"User-Agent": "recorded-example/0.1"},
        )
        r.raise_for_status()
        return r.json()


# ---- FastAPI: handler shape independent of recorder shape ----


class UserResponse(BaseModel):
    login: str
    name: str | None
    repos: int
    followers: int


class HistoryEntry(BaseModel):
    id: str
    key: str | None
    status: str
    duration_ms: int | None
    completed_at: str | None
    github: GitHubUser | None


app = FastAPI(title="recorded-fastapi-example")


@app.get("/users/{login}", response_model=UserResponse)
async def get_user(login: str) -> UserResponse:
    """Fetch a GitHub user. Cached per-login via the recorded log."""
    try:
        data = await fetch_github_user(login, key=f"profile-{login}")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise HTTPException(404, f"GitHub user {login} not found") from e
        raise HTTPException(502, f"GitHub returned {e.response.status_code}") from e

    return UserResponse(
        login=data["login"],
        name=data.get("name"),
        repos=data["public_repos"],
        followers=data["followers"],
    )


@app.get("/history", response_model=list[HistoryEntry])
def history(n: int = 20) -> list[HistoryEntry]:
    """Show the last N recorded GitHub calls, with typed `data` rehydrated."""
    return [
        HistoryEntry(
            id=j.id,
            key=j.key,
            status=j.status,
            duration_ms=j.duration_ms,
            completed_at=j.completed_at,
            github=j.data,    # already a GitHubUser instance, or None
        )
        for j in last(n, kind="github.users.*")
    ]
