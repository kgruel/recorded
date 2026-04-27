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


def test_fastapi_capture_request_rejects_non_request_shaped():
    """Duck-typing failure surfaces a clear `TypeError`, not an opaque
    `AttributeError` from deep inside the helper."""
    import asyncio

    with pytest.raises(TypeError, match="missing"):
        asyncio.run(recorded_fastapi.capture_request(object()))
