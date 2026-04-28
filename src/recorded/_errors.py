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


# --- Warnings ---------------------------------------------------------------


class RecordedWarning(RuntimeWarning):
    """Lifecycle / usage hazards the library surfaces via `warnings.warn`.

    Subclasses `RuntimeWarning` so `python -W error::RuntimeWarning` and
    `pytest -W error::RuntimeWarning` catch all of them; the dedicated
    subclass also lets users filter recorded-specific warnings precisely
    via `warnings.filterwarnings("error", category=recorded.RecordedWarning)`.

    Policy (see `docs/HOW.md::Warnings policy`):

    - **Lifecycle / usage hazards** (e.g. dirty-recorder at exit, leader
      shutdown drain timeout, leader heartbeat resurrection) emit BOTH a
      `_logger.warning(...)` AND a
      `warnings.warn(..., RecordedWarning)`. Logger covers structured-
      logging deployments; warnings.warn covers test-discipline users
      (`pytest -W error`) and interactive surfacing.
    - **Informational telemetry** (e.g. data-projection drift) emits
      logger only — these are observations the user may act on, not
      misuse the library is signalling.
    """


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


class AttachKeyError(UsageError):
    """`attach(key, ...)` against a typed `data=Model` slot used a key
    the model does not declare.

    Raised at the `attach()` call site, before any DB write. Either
    declare the key on the model, or drop `data=Model` from the
    decorator to use the bare passthrough data slot for free-form
    annotations.
    """

    def __init__(
        self,
        *,
        kind: str,
        model: type,
        key: str,
        declared: frozenset[str],
    ) -> None:
        self.kind = kind
        self.model = model
        self.key = key
        self.declared = declared
        sorted_declared = sorted(declared)
        super().__init__(
            f"attach({key!r}, ...) on kind={kind!r}: "
            f"{key!r} is not declared on data model {model.__name__}. "
            f"Declared fields: {sorted_declared}. "
            f"Either add {key!r} to {model.__name__}, or drop "
            f"data={model.__name__} from the decorator to use the "
            f"free-form passthrough data slot."
        )


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


class RowDisappearedError(IdempotencyError):
    """A waiter is parked on a row that no longer exists.

    Surfaces in `JobHandle.wait()` / `wait_sync()` and the bare-call
    idempotency-join path when the row is deleted between subscribe and
    terminal-write — operationally rare (DROP TABLE, manual cleanup,
    foreign-process delete) but the wait loop would otherwise run to
    `JoinTimeoutError` (default 30 s) instead of failing in milliseconds.
    """

    def __init__(
        self,
        *,
        kind: str,
        key: str | None,
        sibling_job_id: str,
    ) -> None:
        self.kind = kind
        self.key = key
        self.sibling_job_id = sibling_job_id
        super().__init__(
            f"Row {sibling_job_id} (kind={kind!r}, key={key!r}) disappeared "
            "before reaching terminal status — likely deleted by an external "
            "process or DROP TABLE."
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
