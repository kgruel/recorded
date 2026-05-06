from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar

from recorded import copy_context_run


def test_threadpool_submit_does_not_propagate_contextvars_by_default():
    request_id: ContextVar[str] = ContextVar("request_id", default="missing")
    request_id.set("req-123")

    def read_request_id() -> str:
        return request_id.get()

    with ThreadPoolExecutor(max_workers=1) as pool:
        got = pool.submit(read_request_id).result(timeout=2)

    assert got == "missing"


def test_copy_context_run_propagates_contextvars_through_threadpool_submit():
    request_id: ContextVar[str] = ContextVar("request_id", default="missing")
    request_id.set("req-123")

    def read_request_id(prefix: str, *, suffix: str) -> str:
        return f"{prefix}{request_id.get()}{suffix}"

    with ThreadPoolExecutor(max_workers=1) as pool:
        got = pool.submit(copy_context_run(read_request_id), "id=", suffix="!").result(timeout=2)

    assert got == "id=req-123!"
