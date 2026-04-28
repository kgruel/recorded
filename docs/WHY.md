# `recorded` — why it's shaped this way

A decision log. Each entry: a load-bearing call, the reasoning behind
it, and (where applicable) what later superseded or scoped it. The
dissolution test is the connective tissue — every survivor passed it.

> **What's in:** rationale — why we chose this primitive over that
> one, why we cut what we cut, what we reshaped after RESHAPE.
> **What's not:** *how the library works mechanically* (see
> [HOW.md](HOW.md)) or *how to use it* (see [usage/](usage/)).

This doc is **living.** Decisions get reshaped over time; superseded
entries stay (so the reasoning is preserved) but are clearly marked,
with forward links to the section in [HOW.md](HOW.md) that describes
the current state.

---

## The dissolution test

One rule decides what survives: **can the proposed feature be expressed
as a property of what already exists?** If yes, the proposed thing
dissolves and the existing thing carries it. If no, it earns a real
entry on the surface.

The single-table schema, the slot-with-adapter primitive, the
lifecycle, the one-notify-primitive — these survived because they
passed dissolution. Honker, schema_version, ULID generation, dry-run
mode, aggregation helpers, the contextvar-routed Recorder dispatch —
these didn't. The library got smaller through design, not bigger.

## The three-tier opt-in cost model

Every feature falls into one of three tiers, ordered by cost:

1. **Basic / wrap-transparent.** Bare `@recorder` (sync or async),
   `key=` for idempotency on bare-call, `attach()`, `attach_error()`.
   Removable along with `@recorder` without rewriting call sites.
   Cost = one SQLite write.
2. **Inspection.** `recorded.last`/`get`/`query`/`connection`, the CLI.
   Names the library, but no infrastructure cost.
3. **Cross-process.** `.submit()`, `JobHandle.wait()`. Requires a
   leader process (`python -m recorded run`) running against the same
   `jobs.db` — durable cross-process handoff is what this tier *is*.

A script that only ever uses Tier 1 has zero background threads, no
leader-detection cost, and no operational dependencies — the library
never imposes itself. Tier 3 is opt-in infrastructure: `.submit()`
raises `ConfigurationError` if no leader heartbeat is fresh, so a
misconfigured deployment fails loudly on its first submit rather than
degrading silently.

This tier model gives every proposal a falsifiable place to land: Tier
1 features must be wrap-transparent; Tier 3 can be loud. A proposal
that fails dissolution and doesn't fit any tier is a signal the
proposal isn't ready — or the tier model is missing something.

---

## The slot primitive

**One JSON column per audit role, each optionally validated by an
adapter.** Four slots: request, response, data, error. Each
independently typeable.

The slot is the entire conceptual surface. Everything else — typed
responses, queryable projections, structured errors, mid-execution
annotations — collapsed into "another slot" or "a method on a slot."
The dissolution test is what kept reducing proposals to this shape.

**Type-adapter tiers** (priority order, all v1):

- **stdlib `dataclasses`** — first-class, no extra deps.
- **Pydantic v2** — auto-detected if installed; never imported.
- **Plain dict / primitive** — accepted as-is, no validation.

Why duck-typed pydantic and not a hard dep: making pydantic optional
forces the slot machinery to be uniform across all three tiers, which
makes the `data=Model` / `request=Model` / `response=Model` /
`error=Model` symmetry actually hold. A pydantic-required design
would silently rot the dataclass and passthrough paths.

> **Reshaped — Adapter is now an ABC.** The original implementation
> used a string discriminator (`Adapter._kind`) re-read by a
> free-function projector (`_project_response`) elsewhere. Both
> halves dissolved into per-subclass `.project()` methods on
> `_PassthroughAdapter` / `_PydanticAdapter` / `_DataclassAdapter`,
> with `make_adapter(model)` as the public factory. Implementation in
> [HOW.md → the slot primitive](HOW.md#the-single-primitive-the-slot).

> **Reshaped — cross-shape projection dropped.** The original
> projection logic accepted any response with field names overlapping
> the data model. Returning a *different* model with overlapping
> fields silently produced a partial projection — exactly the kind of
> drift hard to debug after the fact. Auto-projection now requires
> `isinstance(response, data_model)` OR a dict that validates against
> the model. The dropped cases surface as a deduped warning on the
> `recorded` logger, configurable via `warn_on_data_drift=`.

## Schema

**One table, JSON slots, no migrations API.** Small enough to read the
source in an afternoon. If you outgrow it, migrations stay your
problem — you have direct SQLite access. Schema in
[HOW.md → the schema](HOW.md#the-schema).

The partial unique index `uq_jobs_kind_key_active` does the
load-bearing work for idempotency: at most one
`pending`/`running`/`completed` row per `(kind, key)`, multiple
`failed` rows allowed (history preserved). This is the trick that
makes idempotency work without a separate dedup table.

**SQLite 3.35.0+ required** for `UPDATE ... RETURNING` (the
atomic-claim primitive). Stdlib `sqlite3` on most modern platforms
qualifies; RHEL 8 / older Debian may need a SQLite update.

## Lifecycle

**Original decision: three writes per call.** `pending → running →
terminal`, identical regardless of who orchestrates (bare or
submitted). Same observability, same reaper behavior, same
idempotency semantics, same `attach()`. The wait mechanism becomes
one piece of code, used everywhere.

This costs the bare call ~1ms (one extra UPDATE vs an "insert
directly as running" optimization). At broker-API scale that's <1%,
and the consistency was judged worth it.

> **Superseded — bare and submitted now diverge.** A code review
> surfaced a structural bug: the leader could claim and double-execute
> bare-call rows. The "consistency in mental model" argument was
> outweighed by the bug class it admitted. Current shape:
>
> - **Bare**: `INSERT running → UPDATE terminal`. Two writes.
> - **Submitted**: `INSERT pending → UPDATE running` (atomic claim) `→
>   UPDATE terminal`. Three writes.
>
> `pending` exclusively means "queued for the leader." `_mark_running`
> no longer exists. The leader can never see a bare-call row, so the
> double-claim race is structurally impossible. Trace details in
> [HOW.md → the lifecycle](HOW.md#the-lifecycle).

> **Heartbeat-row exception.** The leader process inserts one
> `_recorded.leader` row keyed by `host:pid` and updates its
> `started_at` periodically as a liveness signal. That row stays
> `running` indefinitely while its `started_at` is refreshed; it never
> reaches a terminal status under normal operation. The reaper handles
> dead-leader cleanup automatically — a stale `running` row with old
> `started_at` gets flipped to `failed` on the next bootstrap, and
> `is_leader_running()` then sees no fresh row. Reusing `started_at`
> as the staleness clock was a deliberate choice on dissolution
> grounds: a separate `heartbeat_at` column was considered and
> rejected because the reaper already does the right thing for stale
> `running` rows. See [Worker → the leader](#worker-the-leader).

## Wrap-transparency

**Bare-call is transparent; the decorator is a side effect, not a
value transformation.** Removing `@recorder` (and `key=`) leaves
working code with the same return-type and exception-type shape.

This is the core promise. It's what makes `@recorder` safe to add to
a function whose call sites you don't want to think about. The
library records, it doesn't transform.

> **Scoped to basic.** Advanced surfaces (`JobHandle.wait()`,
> `JoinedSiblingFailedError` on keyed-call failure paths) are not
> wrap-transparent — you opted into the queue surface, you handle the
> queue's exceptions. Three concrete consequences enforced in code:
>
> - `attach()` / `attach_error()` are silent no-ops outside a
>   recorded context. No `AttachOutsideJobError`. Removing
>   `@recorder` and forgetting to remove an `attach()` call should
>   not explode the caller; side-effect-only APIs are silent when
>   there's nothing to act on.
> - **Recording-failure on the success path does not raise.** If the
>   wrapped function returned successfully but `_write_completion`
>   raised, the row is marked `failed`, a warning is logged on the
>   `recorded` logger, and the function's natural value is propagated.
>   The bare equivalent would never raise — neither does the decorated
>   form.
> - **`error=Model` adapter rejecting an `attach_error` payload does
>   not raise.** Falls back to `{type, message}` of the original
>   exception, logs a warning. Raising a `SerializationError` would
>   itself violate wrap-transparency by replacing the user's exception
>   with one the bare function couldn't produce.

## Idempotency

**Mechanism: partial unique index on `(kind, key)`.** Storage-layer
"at most one active." Multi-process safe — two processes' INSERTs
both hit the same constraint; one wins, the other catches
`IntegrityError` and joins via the wait primitive. Mechanics in
[HOW.md → idempotency](HOW.md#idempotency--the-partial-unique-index-trick).

### `key=` requires explicit `kind=`

**Refuse-to-compile when `key=` is used against a function whose
decorator has an auto-derived kind.** The auto-derived kind is
`f"{fn.__module__}.{fn.__qualname__}"`; renaming or moving the
function silently changes the kind, and the idempotency keyspace
silently changes with it. A retry after a refactor would re-execute
work that should have been deduplicated — exactly the failure mode
idempotency was supposed to prevent.

We refuse rather than warn. Forces a one-time deliberate choice: when
you opt into idempotency, you also opt into naming the *logical
action* explicitly, independent of what your function happens to be
called. The cost is one explicit name once per idempotent action;
the value is "this won't silently break across renames."

### `retry_failed=` and `JoinedSiblingFailedError`

**Failed rows retry by default; `retry_failed=False` joins the
failure rather than retrying.** When a join lands on a failed row
(by `retry_failed=False`, or by `JobHandle.wait()` against a row
that terminated as failed), the call raises
`JoinedSiblingFailedError` carrying the prior `error_json`.

Consistent with wrap-transparency: keyed-call surfaces never return
a `Job` for failure paths. Either you get the response, or you raise.

## ContextVars for `attach()`

**`current_job: ContextVar[JobContext | None]`, not
`threading.local`.** ContextVar propagates correctly through:

- `await` boundaries (no special handling needed)
- `asyncio.gather` task isolation (each task gets its own copy via
  `Context.copy()`)
- `asyncio.to_thread` (Python 3.7+)

Three propagation paths, one mechanism. `threading.local` would have
broken on the first two and required custom propagation through
`gather`.

> **Scoped silent-no-op.** Calling `attach()` or `attach_error()`
> outside an active recorded context is a silent no-op (was
> originally `AttachOutsideJobError`). Wrap-transparency requires
> that removing `@recorder` not force the caller to delete their
> `attach()` calls.

## The typed-slot contract for `attach()`

**`@recorder(data=Model)` makes the data slot a contract, not a
suggestion.** Under a typed data slot, `attach(key, value)` validates
`key` against the model's declared fields and raises `AttachKeyError`
at the call site for unknown keys. Bare `@recorder` (no `data=Model`)
keeps the free-form passthrough — the experimentation escape hatch.

Why this is wrap-transparency, not a separate principle:

- A row written via the public write path must be readable through the
  public read path. Pre-fix, `@recorder(data=Model)` plus
  `attach("undeclared", v)` wrote a `data_json` document the read path
  could not rehydrate (`_DataclassAdapter.deserialize` raised
  `TypeError`; pydantic `extra="forbid"` raised; pydantic default
  silently dropped the key, losing data the writer intended to keep).
  The asymmetry between successful write and failed read directly
  violates the invariant.
- Catching at `attach()` rather than at completion serialization is the
  difference between "stack trace points at the offending line" and
  "stack trace points at a serializer five frames deep." Highest-
  actionability error the library can emit. `flush=True` write-throughs
  must validate at the same point — otherwise the flush would land an
  unreachable key.

Why this dissolves rather than adds surface:

- The check is a property of the typed slot itself. `Adapter` already
  knows its model; surfacing `field_names` (with `None` as the
  passthrough sentinel) is one attribute, not a new subsystem. The
  inline `dataclasses.fields(...)` walk in `_DataclassAdapter.project`
  also dissolves into the same accessor — net code is smaller.
- The bare-recorder escape hatch preserves the
  experimentation-friendly path. Continuation arc: bare → repeated
  keys observed → declare a `data=Model` → tool starts enforcing →
  attach errors point at exactly which keys to declare. Schema grows
  out of call sites; you don't pay the cost until you want the
  guarantee.

The `error=Model` slot stays under different rules: `attach_error()`
is full-replace, not key-merged, and the recording layer falls back to
`{type, message}` when the typed adapter rejects the payload. Raising
on adapter rejection would itself violate wrap-transparency by
replacing the user's exception with one the bare function couldn't
produce. The data-slot rule and the error-slot rule are both
consequences of the same principle; their mechanics differ because
the slots' write semantics differ.

### Joiner symmetry

**All joiners — same-process and cross-process — rehydrate response
from storage. Type identity for typed-instance returns under `key=`
requires `response=Model`.**

Earlier the `Recorder` carried an in-process live-result stash: the
leader pushed its typed return value into `_live_results[job_id]`
just before the terminal write so a same-process joiner could pop it
and skip storage rehydration. The intent was to spare typed-return
users from declaring `response=Model` purely to satisfy in-process
joiners.

The stash didn't hold its invariant. `_take_live_result` popped on
consume, so on N same-key in-process joiners only the first got the
typed object; the rest fell through to storage and got the dict.
"Type identity preserved" became a coin flip among siblings, and
cross-process joiners — who never hit the stash — got the dict
deterministically. Same recorded row, three different return-type
shapes depending on lottery position. That violated wrap-transparency
across the joiner cohort and added a cleanup invariant the reaper
had to maintain (drop stale entries when sweeping orphaned rows).

Dissolved into a single rule, symmetric with the typed-data attach
contract above: the slot model is the contract. Declare
`response=Model` and joiners get the typed instance — same shape the
leader returned, by storage round-trip. Don't, and joiners get the
dict — same shape every joiner sees, every time. One mental model
for in-process and cross-process joins. Costs one SQLite WAL read
plus a JSON parse for the formerly-lucky first joiner; sub-millisecond
on a local file, and the price is paid for predictability rather
than for an invariant the cache couldn't keep.

## The wait primitive

**One mechanism, four surfaces.** `JobHandle.wait()`,
`JobHandle.wait_sync()`, idempotency-collision joins, and the reaper
all use the same subscribe/resolve protocol on `Recorder`.

`concurrent.futures.Future` (not `asyncio.Future`) so one Future
serves both `fut.result(timeout=)` for sync waiters and
`asyncio.wrap_future(fut)` for async waiters. Loop-agnostic — the
library doesn't pick an event loop on the user's behalf.

Cross-process joiners fall back to a polling loop with the same
waiter function; the per-iteration `NOTIFY_POLL_INTERVAL_S` (200 ms)
timeout doubles as the polling cadence. Mechanics in
[HOW.md → the wait primitive](HOW.md#the-wait-primitive--one-mechanism-four-surfaces).

## Worker — the leader

**`.submit()` is a cross-process tier. The leader is required
infrastructure, not opportunistic.** A separate process running
`python -m recorded run` claims pending rows and executes them; in-
process `.submit()` raises `ConfigurationError` if no leader heartbeat
is fresh.

> **Reshaped — dissolved the in-process worker.** The original
> implementation lazy-started an asyncio-loop-on-a-daemon-thread Worker
> on first `.submit()`. That worker introduced a class of shutdown
> hazards: a daemon thread killed at interpreter teardown converted
> in-flight successes into `CancelledError` rows; `Recorder.shutdown()`
> drained it bounded by a join timeout that was silent on miss. The
> hazards were patched defensively via `_LIVE_RECORDERS` + an atexit
> warning hook; structural elimination required dissolving the worker
> entirely. The dissolution test fired: the worker's job (claim →
> run → record) is identical to what a leader process does, so the
> in-process worker dissolves into the leader. Capability progression
> stays honest — `.submit()` *means* "you have a leader running."
> `_worker.py`, `_LIVE_RECORDERS`, `_atexit_warn_dirty_recorders`, the
> `_ensure_worker` lazy-start machinery: all gone. `Recorder.shutdown()`
> simplifies to "mark closed, close conn." See HANDOFF.md Decision
> Queue #2.

The leader process is a tight asyncio loop:

1. `_claim_leader_slot(host:pid)` inserts a `_recorded.leader` row.
2. Heartbeat task refreshes `started_at` every `leader_heartbeat_s`.
3. Main loop: `await rec._claim_one()`; on hit, spawn a task that runs
   `_run_and_record_async` for the row.
4. SIGTERM/SIGINT: drain in-flight tasks (bounded by
   `--shutdown-timeout`), DELETE the heartbeat row, exit 0.

The same SQL primitives that the in-process worker used (`CLAIM_ONE`,
`UPDATE_COMPLETED`, `UPDATE_FAILED`, the wait primitive) carry over —
the leader is just a different host for the same machinery. The wait
primitive's cross-process polling fallback (200 ms cadence) was
already shipped behavior; it's now the only path under `.submit()`.

### Why a heartbeat row, not a separate `leaders` table or PID file

A heartbeat row uses primitives that already exist: the `jobs` table,
the status machine, the partial unique index on `(kind, key)`, the
reaper. A reserved-kind prefix (`_recorded.*`) is convention, not DDL,
so no schema change. The reaper handles dead-leader cleanup
automatically — a stale `running` heartbeat with old `started_at`
gets reaped on the next bootstrap. PID files would have added a
filesystem dependency the library otherwise doesn't have; a separate
table would have added a migration boundary for one internal use
case. Both fail the dissolution test.

`is_leader_running()` is one SELECT against the heartbeat row:
`SELECT 1 FROM jobs WHERE kind='_recorded.leader' AND status='running'
AND started_at >= now() - leader_stale_s`. Cheap and exposed publicly
for deployment health checks.

### Cross-mode shims (`.sync`, `.async_run`)

**Explicit shims for "wrong-mode caller invokes function."**
`.sync()` runs an `asyncio.run()` per call (~10–50 ms loop-setup
overhead) — intended for one-off entry points (CLI main, REPL
exploration); not for tight loops. `.async_run()` is
`asyncio.to_thread(fn)` — contextvars propagate through `to_thread`,
so `attach()` works.

`.sync()` from inside a running event loop raises `SyncInLoopError`
rather than deadlocking on `asyncio.run`-under-loop.

### BYO leader

The leader implementation lives in `_cli.py::cmd_run` + the in-`Recorder`
heartbeat helpers. Custom workers (Celery / k8s-cron / bespoke scripts)
that want to drive jobs against `jobs.db` can do so by calling
`Recorder._claim_one()` plus the `_storage` SQL constants
(`INSERT_PENDING`, `UPDATE_COMPLETED`, `UPDATE_FAILED`) directly, and
maintaining their own heartbeat row via `_claim_leader_slot` /
`_touch_leader_heartbeat` so `is_leader_running()` reports correctly
to submitters. There's no `WorkerContract` interface — the second
implementation hasn't shown up yet.

## Recorder dispatch

**Decorated callables route through the module-level default
`Recorder`.** To point a decorated function at a non-default
Recorder, either:

- Use `recorded.configure(path=...)` (process-wide).
- Drive jobs through the explicit Recorder's own methods (`r.get`,
  `r.list`, the read API).

> **Reshaped from original.** The original spec hinted at a
> contextvar-based dispatch — `async with Recorder(...) as r: await
> place_order(req)` would route to `r`. Never built. Added cost
> (extra ContextVar plumbing, dispatch indirection) without earning
> its keep against `configure()`. Removed from the design.

> **Asymmetry to know.** `recorded.configure()` registers
> `atexit.register(r.shutdown)`; constructing `Recorder(path=...)`
> directly does **not**. Direct construction expects a `with` block
> (or explicit `r.shutdown()`).

## Reaper

**Defensive sweep on connection bootstrap.** Stuck `running` rows is
the single most common source of "this library feels broken" reports
for systems like this. ~10 lines of conditional UPDATE makes it
disappear.

Default threshold 5 minutes. The conditional UPDATE means
at-most-once-per-row regardless of who wins across processes —
multiple recorders booting against the same DB don't double-reap.

> **Trigger: first `_connection()` init**, not a `Recorder.start()`
> method. The original spec hinted at `start()`; never built. The
> reaper sweep belongs in the same place schema bootstrap runs.

## Read API

**Minimal primitives + an escape hatch.** `last`, `get`, `query`,
`connection`. The library knows the schemas-per-`kind` from the
decorator registry, so reads return rehydrated typed values where
models were registered, raw dicts where not.

`recorded.query(...)` filters: `kind` (glob), `status` (exact), `key`
(exact), `since` / `until`, `where_data` (equality on top-level
keys), `limit`, `order`. **Equality only.** Anything richer goes
through `recorded.connection()` and raw SQL.

This is the **no-DSL line.** The library is a recording mechanism,
not a query language. `where_data` is a 90%-case shorthand; the 10%
case is better expressed in raw SQL than in a half-built DSL.

## IDs and timestamps

**IDs: `uuid.uuid4().hex`.** Unique, no monotonic guarantees, no
cross-process coordination. Sort order comes from `submitted_at`.

> ULIDs were the alternative; turned out to require monotonic-
> within-millisecond state and locking. Not worth the complexity for
> the sortability win, especially when a separate timestamp column
> already sorts.

**Timestamps: ISO8601 UTC microsecond `Z`-suffix, fixed-width.**
Lexicographic sort = chronological sort, so `ORDER BY submitted_at`
and `WHERE submitted_at > '2026-...'` work without conversion.
Browsable in the `sqlite3` REPL without unixepoch wrapping.

## Module structure (post-RESHAPE)

**Bottom-up layering with `_lifecycle.py` as the recording-machinery
hub.** Pre-RESHAPE, bare-call (in `_decorator.py`) and worker (in
`_worker.py`) had parallel implementations of the same recording
lifecycle. They drifted; bugs clustered.

Post-RESHAPE, both delegate into `_lifecycle._run_and_record` /
`_run_and_record_async`. Sync/async asymmetry lives in the call
surface and the invocation closure, not in the recording layer. One
fix-site for any recording-layer bug. Layout in
[HOW.md → file map](HOW.md#file-map).

## Cuts and deferrals

The features that didn't earn their keep, and why each was cut.
**Refusing to add is the library's most-used decision.**

- **Honker (cross-process notify).** Native loadable extension with
  install-time fragility. The latency win (sub-ms vs 200 ms polling)
  only matters under workloads we're not targeting in v1. Add later
  if a production user actually needs it.
- **Schema versioning.** Considered a `schema_version` column +
  decorator parameter; pulled. Shipping the column without the
  behavior it serves is a Chekhov's gun. Add as one cohesive feature
  later, if drift becomes a real problem.
- **Actor / multi-tenant.** Out of v1 scope. Apps that need it can
  attach an `actor` key via `attach()` and query through `data_json`
  — the library doesn't provide row-level isolation.
- **Dry-run mode.** User-implementable in their own code; the
  library's only value-add would be conventionalization. Add when
  users actually ask.
- **Aggregation helpers** (`total_cost`, `count_by_kind`, etc.).
  One-liners via `recorded.connection()` + raw SQL. Adding them in
  the library drifts toward the query-DSL line.
- **Per-kind reaper threshold.** Single global threshold today;
  per-kind overrides designed-for but not implemented. Wait for a
  real workload.
- **Cross-process push notification.** Polling at 200 ms is the v1
  wake mechanism. Sub-millisecond cross-process push is a workload
  we're not targeting.
- **MCP server in core.** The read API + `Job.to_prompt()` makes this
  a thin wrapper anyone can build elsewhere. We make it cheap to
  ship, rather than bundling it.
- **`WorkerContract` for BYO workers.** No interface needed — the
  leader is just a tight asyncio loop on `_claim_one()` plus a
  heartbeat row. See [Worker — the leader → BYO leader](#byo-leader)
  for how to drive jobs from a custom executor without `cmd_run`.
- **In-process `Worker`.** Lazy-started asyncio-loop-on-a-daemon-thread
  that ran `.submit()` jobs in the submitting process. Dissolved in
  the worker-dissolution change — see [Worker — the leader](#worker--the-leader).
  The hazards it introduced (CancelledError-on-teardown, silent join
  timeout, the `_LIVE_RECORDERS` atexit warning) all dissolved with it.
- **`AttachOutsideJobError`.** Originally raised when `attach()` was
  called outside a recorded context; removed in favor of silent
  no-op. Wrap-transparency requires it.
- **Replay** (`recorded.replay(job_id)` to re-validate stored
  request and re-invoke). Mostly mechanical; ship after the core
  stabilizes if real demand surfaces.
- **`recorded.configure(recorder=...)`** for installing a pre-built
  Recorder as the module default. Today requires the private
  `_set_default(r)`. Surface if a user asks.
