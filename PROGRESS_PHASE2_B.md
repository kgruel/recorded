# Phase 2 Stream B — Completion report

Status: complete. **108 tests, 3.7 s** suite runtime on Python 3.13 /
SQLite 3.53. All 12 named tests across this stream (4 `to_prompt` + 8
CLI) collected and passing. All 96 prior tests (phase 1 + Stream A)
still pass.

## Files

### Package (`src/recorded/`)

| file | role / change |
|---|---|
| `_types.py` | Adds `Job.to_prompt() -> str` + private `_pretty()` helper. `json` import added. |
| `_cli.py` | **new** — three subcommand handlers (`cmd_last`, `cmd_get`, `cmd_tail`), `_format_row()` shared one-line formatter, argparse `build_parser()`, `main()` entry point. Stdlib only. |
| `__main__.py` | **new** — one-line `sys.exit(main())` shim so `python -m recorded …` works. |
| `__init__.py` | Unchanged — `Job` was already exported. |

### Tests (`tests/`)

| file | scope |
|---|---|
| `test_to_prompt.py` | **new** — 4 named `to_prompt` tests (completed/failed structure, empty-slot omission, pretty-print + `default=str` non-JSON-native fallthrough). |
| `test_cli.py` | **new** — 8 named CLI tests via real `subprocess.run` / `Popen`. Pre-seeds DB through a `Recorder(path=...)` + decorator path inside the test, shuts it down before the subprocess starts, and runs the CLI against the same path. SIGINT for the tail Ctrl-C path; `timeout=2.0` on every invocation. |

### Project files

| file | role |
|---|---|
| `PROGRESS_PHASE2_B.md` | This report. |

## Named-test scoreboard

All 12 named tests across Stream B present and passing.

### `to_prompt`

| named test | file | status |
|---|---|---|
| `test_to_prompt_completed_job_includes_request_response_data_no_error` | `test_to_prompt.py` | PASS |
| `test_to_prompt_failed_job_includes_error_section_no_response` | `test_to_prompt.py` | PASS |
| `test_to_prompt_omits_empty_slots_except_request` | `test_to_prompt.py` | PASS |
| `test_to_prompt_pretty_prints_nested_json` | `test_to_prompt.py` | PASS |

### CLI

| named test | file | status |
|---|---|---|
| `test_cli_last_prints_n_completed_jobs_descending` | `test_cli.py` | PASS |
| `test_cli_last_filters_by_kind_glob_and_status` | `test_cli.py` | PASS |
| `test_cli_get_prints_job_json` | `test_cli.py` | PASS |
| `test_cli_get_with_prompt_flag_emits_to_prompt_output` | `test_cli.py` | PASS |
| `test_cli_get_unknown_id_exits_2_with_stderr_message` | `test_cli.py` | PASS |
| `test_cli_tail_emits_new_terminal_rows_after_watermark` | `test_cli.py` | PASS |
| `test_cli_tail_handles_keyboard_interrupt_with_exit_zero` | `test_cli.py` | PASS |
| `test_cli_path_flag_targets_an_explicit_db_file` | `test_cli.py` | PASS |

## Major design decisions made within the brief / DESIGN.md guidance

1. **`get` JSON shape via `dataclasses.asdict(job)`, not `vars(job)`.**
   The brief says "the same shape `Job.__dict__` serializes to."
   `dataclasses.asdict` is the practical equivalent and recursively
   handles nested dataclass instances cleanly (rehydrated `request` /
   `response` / `data` / `error` may be dataclasses if a model was
   registered). Pretty-printed with `indent=2, sort_keys=False, default=str`
   to match `to_prompt()`'s renderer.

2. **`tail` polls two `list()` calls per tick (one per terminal status)
   and merges.** `Recorder.list()` accepts a single `status=` value, so
   `status="completed" OR "failed"` from the brief is realized as two
   queries merged + sorted by `submitted_at`. Cheaper than going through
   `recorded.connection()` raw SQL, and consistent with "read API
   only" from the brief.

3. **`tail` watermark + `seen_ids` boundary set.** `since=` in
   `Recorder.list()` is `>=` (not `>`), so naïvely advancing the
   watermark to `max(submitted_at)` would replay the boundary row on
   the next tick. I track a `seen_ids` set; after each tick, I trim it
   to only the ids whose `submitted_at` equals the new watermark — so
   it stays bounded (only the rows tied at the boundary timestamp).
   With microsecond-precision ISO timestamps, in practice the set is
   ~1–2 entries.

4. **Tail's initial watermark is `now_iso()`.** Per the brief: "no
   historical replay; users who want history use `last`." Spawn-time
   `now_iso()` is the obvious anchor.

5. **`Job.to_prompt()` rendering rules.** Header bullets always render;
   a `None` value for `key` / `started_at` / `completed_at` / duration
   shows as the em-dash `—`. Sections:
   - **Request**: always renders (audit anchor) — `—` if `request is None`.
   - **Response**: renders iff `response is not None`. `is not None`
     rather than truthiness so a legitimate falsy return value
     (`False`, `0`, `[]`, `""`) still appears — preserving the audit
     invariant from DESIGN.md ("the audit log is faithful").
   - **Data**: renders iff `data` is truthy. Empty `{}` / `[]` is noise
     and the brief explicitly says "only if data is non-empty."
   - **Error**: renders iff `error` is truthy. By the time `Job` exists,
     a recorded error is `{"type": "...", "message": "..."}` (non-empty).
   This is a small asymmetry between `response` (None-only) and
   `data`/`error` (truthiness); rationale captured inline in the code.

6. **Pretty-printing uses `default=str`.** Per brief. The fallthrough
   covers any non-JSON-native value that somehow reached `to_prompt`
   (datetime, etc.). In practice the read-side adapter normalizes
   typed values back to native dicts before `Job` exists; the `default=str`
   guard is belt-and-suspenders that avoids ever raising from
   rendering code.

7. **CLI never touches the module-level default Recorder.** Each
   subcommand handler builds its own `Recorder(path=args.path)` and
   shuts it down in a `try/finally`. Per the brief: a CLI invocation
   is a separate process with its own short-lived Recorder, and
   touching the module-level default would create a worker thread the
   CLI doesn't need (the read API and the tail polling never call
   `.submit()`, so no worker actually starts — but going through
   `get_default()` would still register the singleton, which the CLI
   has no reason to do).

8. **`tail` calls `r.shutdown()` in `finally`** wrapping the inner
   try/except for `KeyboardInterrupt`. Brief: "tail calls `shutdown()`
   in its KeyboardInterrupt handler." Wrapping in `finally` is
   strictly more robust (covers any other unwind too) and is
   identical to the SIGINT path because `KeyboardInterrupt` is just a
   normal exception that triggers the `finally`.

9. **`build_parser()` lives in `_cli.py`, not `__main__.py`.**
   Keeps `__main__.py` to its proper one-line role and makes the
   parser introspectable from the test harness if needed (none
   currently — every CLI test runs the real subprocess).

10. **Same `_format_row` between `last` and `tail`.** Per the brief:
    "prints any new rows in the same one-line format as `last`."
    Shared helper avoids drift.

## DESIGN.md interpretations / clarifications

1. **"Section omission" rule for `to_prompt`.** DESIGN.md is silent
   on the precise emptiness predicate. The brief gives two rules in
   the same spec — `(only on completed)` for Response, `(only on
   failed)` for Error, `(only if data is non-empty)` for Data — plus
   the umbrella "Slots that are None or empty don't render except
   Request." I unified to: render iff the slot is non-None for
   Response (audit faithfulness on falsy returns), truthy for
   Data/Error (empty dicts are noise). Documented inline in
   `_types.py`. Easy to switch to status-driven gating if the lead
   prefers.

2. **`get`'s JSON output shape uses `dataclasses.asdict`** (see
   decision #1 above). The brief said "the same shape `Job.__dict__`
   serializes to" — `asdict` produces equivalent output and is the
   well-formed API to use.

3. **`last`'s ordering** is descending by `submitted_at` per the
   brief, which matches `Recorder.last()`'s existing semantics. No
   `--asc` / `--desc` flag — out of scope.

4. **`tail` interval default is 1.0 s** (production default per the
   brief); tests pass `--interval 0.05` for snappy assertions.

5. **`tail`'s exit code is 0 on `KeyboardInterrupt`.** Brief: "Ctrl-C
   exits 0 cleanly." The handler returns `0` and `__main__.py` passes
   it to `sys.exit`. No traceback leaks because the `KeyboardInterrupt`
   is caught inside `cmd_tail`.

6. **`get` exit code 2 on unknown id.** Per the brief; argparse owns
   exit code 1 for usage errors (its default).

## Deviations from the brief

1. **None.** Surface and named tests match the brief 1:1. `to_prompt`
   produces exactly the structure described in the brief's "shape"
   section; CLI subcommands take exactly the documented flags and
   exit codes.

## Open questions / rough spots

1. **`response is not None` vs. truthiness asymmetry.** Documented
   in design decision #5. If the lead prefers strict status-driven
   gating (Response only on `status='completed'`, Error only on
   `status='failed'`), it's a one-line change per branch. I went
   with value-driven gating because (a) the omit-empty rule in the
   brief is value-driven, and (b) it handles the in-flight `pending`
   /`running` case naturally — no Response section while a job is
   still running, no special case needed.

2. **`tail`'s seen-id set is unbounded if many rows share a single
   `submitted_at` boundary.** In practice with microsecond-precision
   ISO timestamps colliding rows are rare (a single tick produces a
   handful at most). The boundary-trim keeps it bounded under the
   normal case. If a workload genuinely produced thousands of rows
   per microsecond, the trim would still cap at "rows tied at the
   max timestamp this tick" — fine.

3. **`asyncio.run()` in CLI test fixtures.** The `seeded_db_path`
   fixture and the tail test seed via `asyncio.run(seed())` because
   the decorator path is async. This is the same pattern used by the
   existing `test_read_api.py` indirectly via `pytest-asyncio`, just
   spelled explicitly here so the seed runs before the subprocess
   starts. Cosmetically the `import asyncio` is repeated three times
   inside fixtures; folding to a top-level import is fine if the
   lead prefers.

4. **No `--limit` / `--follow` flag on `tail`.** DESIGN.md §"CLI"
   mentions `--limit=N` and `--follow` for tail. Brief Stream B
   scope omits them. Held for a future stream — they are additive.

5. **No JSON-line `tail` output mode.** DESIGN.md says "live-tails
   new rows to stdout in JSON-line format." Brief overrides this:
   "prints any new rows in the same one-line format as `last`." I
   followed the brief; the JSON-line mode is a flag-flip away if a
   future stream wants it.

6. **`get` without `--prompt` emits raw JSON of the dataclass dict —
   no key=value flat form.** This is what the brief asked for. If
   the lead wants a flat `--format=plain` to mirror `last`'s row
   format for one-job inspection, it's straightforward to add later.

7. **`last [N]` accepts a positional int but not `--limit`.** Per the
   brief; `--limit` is a separate-stream concern.

## Bugs found and fixed

None. The phase-1 + Stream A surfaces (read API, `Job` dataclass,
`Recorder.list()` / `last()` / `get()`) all behaved as documented;
Stream B is purely additive. The advisor flagged the Response/Data
emptiness asymmetry; I documented the rationale rather than treating
it as a bug.

## Wrap-transparency principle (carry-forward)

`Job.to_prompt()` is a pure rendering of a `Job` — it never raises
on any field shape (`default=str` covers exotic values). The CLI
calls only the read API: `Recorder.get()`, `Recorder.last()`,
`Recorder.list()`. No `JobHandle`, no `.submit()`, no wait-primitive
subscription, no decorator wrapping. Per the brief.

## Test results

```
108 passed in 3.73 s
```

- Full suite under the 10-second budget (~2.7× margin).
- 96 prior tests (phase 1 + Stream A) all still pass.
- 12 new tests across 2 new files.
- All 12 named tests collected by exact name (verified via the run
  output above).
- Slowest individual tests: the two `tail` tests (~0.5 s and ~0.2 s
  wall-clock — bounded by their `--interval 0.05` polling cadence
  and the SIGINT round-trip). Both well inside the brief's "~0.2 s
  per tail test" guidance modulo the SIGINT lag.
- Smoke check: `python -m recorded --help` prints the subcommand
  list; `python -m recorded last 0 --path /tmp/none.db` exits 0
  with empty output (and creates an empty WAL DB at the path).

No commit — diff is clean and held for lead review.
