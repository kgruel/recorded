# Usage guides

Task-oriented walkthroughs, one per feature. Each is independent — read
the ones you need, in any order. The list below is a suggested first-pass
tour; the **By need** table below it is a faster path to a specific goal.

---

## Reading order

1. **[decorator.md](decorator.md)** — `@recorder` itself: bare vs factory
   form, sync vs async, cross-mode shims (`.sync` / `.async_run`), what
   the wrapper preserves.
2. **[configuration.md](configuration.md)** — picking where the SQLite
   file lives, `configure()` and the configure-once rule, async context
   manager, multi-process safety.
3. **[reading.md](reading.md)** — `last`, `get`, `list`, the CLI, raw
   SQLite. The `Job` dataclass and slot rehydration.
4. **[idempotency.md](idempotency.md)** — `key=`, `retry_failed`,
   `JoinedSiblingFailedError`, the partial-unique-index that makes it
   work across processes.
5. **[workers.md](workers.md)** — `.submit()`, `JobHandle.wait()`, the
   lazy worker, the reaper, multi-process atomic claims.
6. **[typed-slots.md](typed-slots.md)** — `request` / `response` /
   `data` / `error` models, auto-projection, `attach()` /
   `attach_error()`, the audit invariant.
7. **[fastapi.md](fastapi.md)** — lifespan integration,
   `capture_request`, the two-shape pattern (recorder slot vs API
   contract).
8. **[queries.md](queries.md)** — `recorded.query(...)` filters, raw SQL
   via `connection()`, `Job.to_prompt()` for LLM consumption, schema
   reference.
9. **[errors.md](errors.md)** — exception hierarchy, when each fires,
   what to catch where.

---

## By need

| if you want to... | start here |
|---|---|
| record API calls right now | [decorator.md](decorator.md) → [reading.md](reading.md) |
| dedupe expensive calls | [idempotency.md](idempotency.md) |
| run work in the background | [workers.md](workers.md) |
| validate inputs/outputs against a model | [typed-slots.md](typed-slots.md) |
| query the recorded log later | [queries.md](queries.md) |
| wire this into a FastAPI app | [fastapi.md](fastapi.md) |
| know what exceptions to catch | [errors.md](errors.md) |
| pick where the SQLite file lives | [configuration.md](configuration.md) |

---

## Adjacent docs

- **Architecture tour:** [`../HOW.md`](../HOW.md) — schema,
  lifecycle, the wait primitive, idempotency mechanics, the reaper, the
  read side. Start here for contributors.
- **Authoritative spec:** [`../WHY.md`](../WHY.md) — the "why"
  behind the slot abstraction, three-write lifecycle, dissolution test,
  every load-bearing decision.
- **Runnable examples:** [`../examples/`](../examples/) — LLM-CLI
  replay, FastAPI service, batch HTTP consumer.
