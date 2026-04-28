"""`recorded.fastapi.capture_request` (Stream C 2.C.2).

Skipped cleanly when FastAPI isn't installed. The helper is duck-typed
on Starlette-shaped Request objects; this test exercises it via FastAPI's
`TestClient` to keep the surface honest. Core code never imports FastAPI.
"""

from __future__ import annotations

import json

import pytest

fastapi = pytest.importorskip("fastapi")

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import recorded.fastapi as recorded_fastapi
from recorded import recorder


def _make_app(captured: list):
    app = FastAPI()

    @app.api_route("/echo", methods=["GET", "POST"])
    async def echo(request: Request):
        envelope = await recorded_fastapi.capture_request(request)
        captured.append(envelope)
        return {"ok": True}

    return app


def test_fastapi_capture_request_serializes_envelope():
    """Method, path, query, headers, body present and correctly typed."""
    captured: list = []
    client = TestClient(_make_app(captured))
    resp = client.post(
        "/echo?x=1&y=two",
        content=b'{"hello": "world"}',
        headers={"Content-Type": "application/json", "X-Custom": "hello"},
    )
    assert resp.status_code == 200
    assert len(captured) == 1
    env = captured[0]
    assert env["method"] == "POST"
    assert env["path"] == "/echo"
    assert env["query"] == {"x": "1", "y": "two"}
    # Header keys are case-folded.
    assert "x-custom" in env["headers"]
    assert env["headers"]["x-custom"] == "hello"
    assert env["headers"]["content-type"] == "application/json"
    # Body decoded as utf-8 text.
    assert isinstance(env["body"], str)
    assert json.loads(env["body"]) == {"hello": "world"}


def test_fastapi_capture_request_empty_body_is_none():
    """An empty request body returns `None`, not the empty string."""
    captured: list = []
    client = TestClient(_make_app(captured))
    resp = client.get("/echo")
    assert resp.status_code == 200
    env = captured[0]
    assert env["method"] == "GET"
    assert env["body"] is None


def test_fastapi_capture_request_round_trips_through_request_slot(
    default_recorder,
):
    """End-to-end: capture, pass to a `@recorder` function as the
    `request=`, recorded `request_json` matches the envelope."""

    @recorder(kind="t.fastapi.roundtrip")
    async def handle_request(envelope):
        return {"echo_method": envelope["method"]}

    captured: list = []
    app = FastAPI()

    @app.post("/handle")
    async def handle(request: Request):
        envelope = await recorded_fastapi.capture_request(request)
        captured.append(envelope)
        return await handle_request(envelope)

    client = TestClient(app)
    resp = client.post(
        "/handle?a=b",
        content=b"payload-bytes",
        headers={"X-Trace": "abc"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"echo_method": "POST"}
    captured_env = captured[0]

    rows = default_recorder.last(1, kind="t.fastapi.roundtrip")
    assert len(rows) == 1
    job = rows[0]
    # request_json was captured from the envelope (single-positional-arg
    # shape, so the captured shape is the envelope itself).
    assert job.request == captured_env
    assert job.request["method"] == "POST"
    assert job.request["path"] == "/handle"
    assert job.request["query"] == {"a": "b"}
    assert job.request["body"] == "payload-bytes"


def test_fastapi_capture_request_redacts_secret_headers_by_default():
    """`Authorization`, `Cookie`, `X-Api-Key` etc. are replaced with a marker
    rather than stored verbatim — operator+DB are not implicit secret stores."""
    captured: list = []
    client = TestClient(_make_app(captured))
    resp = client.post(
        "/echo",
        content=b"{}",
        headers={
            "Authorization": "Bearer s3cr3t",
            "Cookie": "session=abc123",
            "X-Api-Key": "key-42",
            "X-Custom": "kept",
        },
    )
    assert resp.status_code == 200
    h = captured[0]["headers"]
    assert h["authorization"] == "<redacted>"
    assert h["cookie"] == "<redacted>"
    assert h["x-api-key"] == "<redacted>"
    # Non-secret headers pass through.
    assert h["x-custom"] == "kept"


def test_fastapi_capture_request_redact_headers_override():
    """Custom `redact_headers=` replaces the default set."""
    app = FastAPI()
    captured: list = []

    @app.post("/x")
    async def handler(request: Request):
        env = await recorded_fastapi.capture_request(
            request, redact_headers=["x-custom"], redact_value="***"
        )
        captured.append(env)
        return {"ok": True}

    client = TestClient(app)
    client.post(
        "/x",
        content=b"",
        headers={"X-Custom": "secret", "Authorization": "Bearer kept"},
    )
    h = captured[0]["headers"]
    assert h["x-custom"] == "***"
    # Authorization is no longer in the override set, so it passes through.
    assert h["authorization"] == "Bearer kept"


def test_fastapi_capture_request_allow_headers_drops_others():
    """`allow_headers=` is an allowlist — anything not listed is dropped."""
    app = FastAPI()
    captured: list = []

    @app.post("/x")
    async def handler(request: Request):
        env = await recorded_fastapi.capture_request(request, allow_headers=["x-trace"])
        captured.append(env)
        return {"ok": True}

    client = TestClient(app)
    client.post(
        "/x",
        content=b"",
        headers={"X-Trace": "abc", "Authorization": "Bearer s3cr3t"},
    )
    h = captured[0]["headers"]
    assert h == {"x-trace": "abc"}


def test_fastapi_capture_request_allow_and_redact_mutually_exclusive():
    import asyncio

    class _Stub:
        method = "GET"
        url = type("U", (), {"path": "/"})()
        headers: dict = {}
        query_params: dict = {}

        async def body(self):
            return b""

    with pytest.raises(TypeError, match="not both"):
        asyncio.run(
            recorded_fastapi.capture_request(_Stub(), redact_headers=["a"], allow_headers=["b"])
        )


def test_fastapi_capture_request_max_body_bytes_truncates_body():
    """`max_body_bytes=N` caps the recorded body and limits peak memory by
    streaming. Truncated bodies append an explicit marker."""
    captured: list = []
    app = FastAPI()

    @app.post("/x")
    async def handler(request: Request):
        env = await recorded_fastapi.capture_request(request, max_body_bytes=8)
        captured.append(env)
        # Downstream body() should still work — capture_request re-primed
        # request._body with the truncated bytes.
        body = await request.body()
        return {"body_len": len(body)}

    client = TestClient(app)
    resp = client.post("/x", content=b"AAAAAAAAAAAAAAAAAAAA")  # 20 bytes
    assert resp.status_code == 200
    assert resp.json() == {"body_len": 8}

    body = captured[0]["body"]
    assert body.startswith("AAAAAAAA")
    assert "<truncated to 8 bytes>" in body


def test_fastapi_capture_request_max_body_bytes_under_cap_no_marker():
    """A body shorter than the cap is unchanged — no marker, full content."""
    captured: list = []
    app = FastAPI()

    @app.post("/x")
    async def handler(request: Request):
        env = await recorded_fastapi.capture_request(request, max_body_bytes=1024)
        captured.append(env)
        return {"ok": True}

    client = TestClient(app)
    client.post("/x", content=b"short body")
    body = captured[0]["body"]
    assert body == "short body"
    assert "<truncated" not in body


def test_fastapi_capture_request_max_body_bytes_rejects_negative():
    import asyncio

    class _Stub:
        method = "GET"
        url = type("U", (), {"path": "/"})()
        headers: dict = {}
        query_params: dict = {}

        async def body(self):
            return b""

    with pytest.raises(TypeError, match="non-negative"):
        asyncio.run(recorded_fastapi.capture_request(_Stub(), max_body_bytes=-1))


def test_fastapi_capture_request_rejects_non_request_shaped():
    """Duck-typing failure surfaces a clear `TypeError`, not an opaque
    `AttributeError` from deep inside the helper."""
    import asyncio

    with pytest.raises(TypeError, match="missing"):
        asyncio.run(recorded_fastapi.capture_request(object()))
