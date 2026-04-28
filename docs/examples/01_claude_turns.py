"""
01 — claude turns: a self-referential 10-minute tour

Use the AI CLI you already have installed (claude, gemini, codex, ...) to
learn `recorded` itself, then pull the prior turns back out of jobs.db
and feed them into a follow-up question. The library demos itself.

Setup (60 seconds):
    pip install recorded
    # have `claude` (or your preferred CLI) installed and logged in
    cd /path/to/sqlite-api-job        # so WHY.md is readable
    python docs/examples/01_claude_turns.py

What this script does, in three beats:

    Beat 1 — RECORD
        Three calls to your CLI asking about this library's design doc.
        Each call is a row in jobs.db: prompt, full response, status,
        duration. The decorator is the only ceremony.

    Beat 2 — READ
        `recorded.last(N, kind=...)` pulls the rows back out, rehydrated.
        request and response are typed (here: plain strings); status,
        duration_ms, timestamps come for free.

    Beat 3 — RE-FEED
        We grab the prior turns via the read API, format them into a
        conversation context, and ask a follow-up question that references
        what was said earlier. The follow-up call is also recorded —
        next time you pick this up, the whole thread is sitting in jobs.db.

After running, poke at the file:

    sqlite3 jobs.db "SELECT kind, status, length(response_json), \
                            json_extract(request_json, '$') AS prompt \
                     FROM jobs ORDER BY submitted_at;"

Things worth noticing in the code below:

  - `@recorder(kind="cli.ask")` is the only library-specific line. The
    function signature, return type, and everything else is plain Python.
  - The bare call returns the wrapped function's natural value (a string).
    No Job, no wrapper. You can rip out @recorder and the call sites are
    unaffected.
  - `last(N, kind=glob)` accepts a glob like `"cli.*"` if you decorate
    multiple kinds.
"""

import subprocess

from recorded import last, recorder


@recorder(kind="cli.ask")
def ask(prompt: str) -> str:
    result = subprocess.run(
        ["claude", "-p", prompt],   # swap to `gemini -p`, `codex exec`, etc.
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def main():
    # ============================================================
    # Beat 1 — RECORD: three turns about this library's design.
    # ============================================================
    print(">>> Asking the CLI to explain `recorded`...\n")

    print(ask("Read WHY.md — give me a one-paragraph overview of this library."))
    print()
    print(ask("Read WHY.md — what is a 'slot' in this library?"))
    print()
    print(ask("Read WHY.md — how does the key= idempotency argument work?"))
    print()

    # ============================================================
    # Beat 2 — READ: pull the conversation back out of jobs.db.
    # ============================================================
    print(">>> Pulling the recorded turns back out of the log...\n")

    for job in last(10, kind="cli.ask"):
        print(f"# {job.duration_ms}ms  ({job.status})")
        print(f"Q: {job.request}")
        print(f"A: {job.response[:300]}{'...' if len(job.response) > 300 else ''}\n")

    # ============================================================
    # Beat 3 — RE-FEED: thread the prior turns into a follow-up.
    # ============================================================
    print(">>> Threading the prior turns into a follow-up question...\n")

    prior = last(3, kind="cli.ask")
    context = "\n\n".join(
        f"Q: {j.request}\nA: {j.response}" for j in reversed(prior)
    )
    answer = ask(
        f"Earlier in this thread:\n\n{context}\n\n"
        f"Given that, write me a tiny @recorder example that uses a "
        f"Pydantic response model and an idempotency key. Keep it under 20 lines."
    )
    print(answer)
    print()
    print(">>> All four turns are now in jobs.db. Run the script again to add more.")


if __name__ == "__main__":
    main()
