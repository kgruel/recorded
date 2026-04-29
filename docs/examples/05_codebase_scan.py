"""05 — pure analysis: a sweep where each function call IS the data.

Most `recorded` examples wrap I/O — HTTP fetches, broker calls, LLM
turns. This one inverts that: the wrapped function is pure local
computation, no side effect, no network. The row IS the result; the
recorded table IS the result space you query.

Walk every .py file in `src/recorded/`, parse its AST, project four
metrics into a typed `data=` slot. Then fold the recorded space —
"all files with async, ranked by LOC" is a query, not a recompute.
Stamp `key=` with `path:mtime` so re-runs are free for unchanged
files and re-measure only what changed.

    pip install recorded pydantic
    python docs/examples/05_codebase_scan.py
    python docs/examples/05_codebase_scan.py   # second run — zero parse work

What's worth noticing:

  - `measure()` returns a `FileMetrics`; `recorded` auto-projects it
    into the `data` slot, so `where_data={"has_async": True}` filters
    on a model field with no extra wiring.
  - The query at Beat 2 does no parsing — it reads typed rows out of
    SQLite, sorts them in Python. The "result space" is durable.
  - Beat 3 demonstrates idempotency-as-incremental-recompute: pin the
    cache key to file content (`path:mtime`) and the sweep becomes a
    differential update.
"""

import ast
import pathlib

from pydantic import BaseModel

from recorded import configure, query, recorder


class FileMetrics(BaseModel):
    path: str
    loc: int
    functions: int
    classes: int
    has_async: bool


@recorder(kind="scan.file", data=FileMetrics)
def measure(path: str) -> FileMetrics:
    src = pathlib.Path(path).read_text()
    tree = ast.parse(src)
    nodes = list(ast.walk(tree))
    return FileMetrics(
        path=path,
        loc=len(src.splitlines()),
        functions=sum(
            1 for n in nodes if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ),
        classes=sum(1 for n in nodes if isinstance(n, ast.ClassDef)),
        has_async=any(isinstance(n, ast.AsyncFunctionDef) for n in nodes),
    )


def sweep(root: pathlib.Path) -> None:
    for py in sorted(root.rglob("*.py")):
        measure(str(py), key=f"{py}:{py.stat().st_mtime_ns}")


def main() -> None:
    configure(path="./scan.db")
    root = pathlib.Path(__file__).resolve().parents[2] / "src" / "recorded"

    # Beat 1 — SCAN: every call is a row.
    print(">>> Sweeping src/recorded/ — each file becomes a row.\n")
    sweep(root)

    # Beat 2 — QUERY: fold the recorded space, no recompute.
    print(">>> Files with async defs, ranked by LOC (read from jobs.db):\n")
    rows = list(query(kind="scan.file", where_data={"has_async": True}, limit=50))
    rows.sort(key=lambda j: j.data.loc, reverse=True)
    for j in rows:
        rel = pathlib.Path(j.data.path).relative_to(root.parent.parent)
        print(f"  {j.data.loc:>4} loc  {j.data.functions:>3} fn   {rel}")

    # Beat 3 — RE-RUN: key=path:mtime → unchanged files are cache hits.
    print("\n>>> Re-sweeping. Unchanged files = zero parse work.\n")
    sweep(root)
    print(">>> All rows still in scan.db. Try: python -m recorded last 5 --path scan.db")


if __name__ == "__main__":
    main()
