"""Optional FastAPI helper.

`capture_request(request)` returns a serializable envelope (method, path,
query, headers, body) suitable for stashing in the `request` slot of a
`@recorder`-decorated handler:

    @app.post("/orders")
    @recorder(kind="api.place_order")
    async def place_order(request: Request) -> dict:
        envelope = await recorded.fastapi.capture_request(request)
        # ...

This module is **stdlib-only**. We accept any object that quacks like a
Starlette `Request` (FastAPI's `Request` is a subclass): we duck-type
check `method`, `url`, `headers`, `query_params`, `body` rather than
importing `starlette` or `fastapi`. Users on raw ASGI / litestar /
custom frameworks get the helper for free as long as their request
object exposes the same attribute surface.

FastAPI is in `[dev]` extras only — importing this module on a system
without FastAPI installed must not blow up the rest of the package, and
doesn't, because we don't import FastAPI here.
"""

from __future__ import annotations

from typing import Any

_REQUIRED_ATTRS = ("method", "url", "headers", "query_params")


async def capture_request(request: Any) -> dict[str, Any]:
    """Snapshot an HTTP request into a JSON-serializable envelope.

    Returns:
        {
            "method": str,
            "path":   str,                 # request.url.path
            "query":  dict[str, str],
            "headers": dict[str, str],     # case-folded keys
            "body":   str | None,          # utf-8 decoded, or None if empty
        }

    Raises `TypeError` if the request object doesn't expose the expected
    Starlette-shaped attributes — this is how we duck-type without
    importing FastAPI/Starlette.
    """
    missing = [a for a in _REQUIRED_ATTRS if not hasattr(request, a)]
    if missing:
        raise TypeError(
            f"capture_request() expected a Starlette/FastAPI-shaped Request "
            f"object; got {type(request).__name__} missing {missing!r}."
        )

    raw_body = await request.body()
    body: str | None
    if raw_body:
        # Starlette returns bytes. We surface utf-8 text; binary uploads
        # outside utf-8 are rare for handlers worth recording, and the
        # caller can compose their own envelope if they need bytes.
        body = raw_body.decode("utf-8", errors="replace")
    else:
        body = None

    return {
        "method": request.method,
        "path": request.url.path,
        "query": dict(request.query_params),
        # Case-fold header keys: HTTP headers are case-insensitive, and
        # mixing cases across requests would corrupt downstream queries.
        "headers": {k.lower(): v for k, v in request.headers.items()},
        "body": body,
    }
