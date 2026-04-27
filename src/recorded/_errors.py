"""Exception hierarchy.

All library-raised exceptions inherit from `RecordedError`. Two main
subtrees:

- `UsageError` — programming or configuration mistakes the caller should
  fix in their code. Catch this when you want any "you used the API wrong"
  error.
- `IdempotencyError` — failures specific to idempotent calls (joined-sibling
  failures, timeouts, the rare lookup race). Catch this when you want to
  handle idempotency-specific outcomes uniformly.

Exceptions to the rule:

- The wrapped function's own exceptions propagate verbatim. The library
  records them and re-raises; we do not wrap user-domain errors.
- `JoinTimeoutError` multi-inherits `TimeoutError` so `except TimeoutError`
  still catches it, in addition to `except IdempotencyError`.
"""

from __future__ import annotations

from typing import Any


class RecordedError(Exception):
    """Root for library-raised errors."""


# --- Usage errors -----------------------------------------------------------


class UsageError(RecordedError):
    """The caller used the API incorrectly. Fix the calling code."""


class ConfigurationError(UsageError):
    """Bad decorator configuration.

    Examples:
    - `key=` passed at a call site whose decorator has an auto-derived kind
    - A model passed to `request=`/`response=`/`data=`/`error=` is neither a
      dataclass nor a Pydantic v2 model
    """


class SyncInLoopError(UsageError):
    """`.sync()` invoked from within a running event loop.

    Use `await fn(...)` from the async context, or move the call to a
    synchronous entry point.
    """


class RecorderClosedError(UsageError):
    """An operation was attempted on a Recorder whose `shutdown()` has run."""


class SerializationError(UsageError):
    """The recorder couldn't serialize a value into a slot.

    Raised when:
    - a value passed to a typed slot doesn't satisfy the registered model
    - the wrapped function's return value is not JSON-serializable
    """

    def __init__(
        self,
        message: str,
        *,
        slot: str | None = None,
        model: type | None = None,
        value: Any = None,
    ) -> None:
        self.slot = slot
        self.model = model
        self.value = value
        super().__init__(message)


# --- Idempotency errors -----------------------------------------------------


class IdempotencyError(RecordedError):
    """Base for idempotency-keyed call failures."""


class JoinedSiblingFailedError(IdempotencyError):
    """Idempotency join found / waited on a sibling that terminated as failed.

    Carries the joined sibling's identifying info plus its recorded error
    payload so the caller can decide what to do (alert, retry, surface).
    """

    def __init__(
        self,
        *,
        kind: str,
        key: str,
        sibling_job_id: str,
        sibling_error: dict[str, Any] | None,
    ) -> None:
        self.kind = kind
        self.key = key
        self.sibling_job_id = sibling_job_id
        self.sibling_error = sibling_error
        msg = (sibling_error or {}).get("message", "<no message recorded>")
        super().__init__(
            f"Joined sibling job for (kind={kind!r}, key={key!r}) "
            f"(job_id={sibling_job_id}) terminated as failed: {msg}"
        )


class JoinTimeoutError(IdempotencyError, TimeoutError):
    """Timed out waiting for a sibling idempotent call to reach terminal status.

    Multi-inherits stdlib `TimeoutError` so `except TimeoutError` callers
    still catch it.
    """

    def __init__(
        self,
        *,
        kind: str,
        key: str | None,
        sibling_job_id: str,
        timeout_s: float,
    ) -> None:
        self.kind = kind
        self.key = key
        self.sibling_job_id = sibling_job_id
        self.timeout_s = timeout_s
        super().__init__(
            f"Timed out after {timeout_s:.1f}s waiting for sibling job "
            f"{sibling_job_id} (kind={kind!r}, key={key!r}) to reach terminal "
            "status."
        )


class IdempotencyRaceError(IdempotencyError):
    """Lost the post-INSERT collision lookup race.

    Practically reachable only if the active row is gone by the time we
    look it up — a narrow window where the prior holder failed and was
    cleaned out between our INSERT-violation and our lookup. The caller's
    safe response is to retry the call.
    """

    def __init__(self, *, kind: str, key: str | None) -> None:
        self.kind = kind
        self.key = key
        super().__init__(
            f"Lost idempotency-collision lookup race for (kind={kind!r}, "
            f"key={key!r}). Retry the call."
        )
