"""
03 — Batch API processing: building a queryable Hacker News cache

Fetch the top N stories from Hacker News, capture each one as a recorded
call, then run aggregations over the recorded log. Run twice — the second
run does zero new HTTP because the idempotency key folds into the
existing rows.

Setup (60 seconds):
    pip install recorded httpx
    python docs/examples/03_api_consumer.py
    python docs/examples/03_api_consumer.py    # again — see idempotency

What you'll see:

    Run 1:    1 index call + 30 story fetches concurrently (~1-2s total).
              Each story becomes a row in jobs.db with title, score, domain.
    Run 2:    1 index call + 30 cache hits, sub-second total. No HTTP for
              the stories you've already got.
    Both:     A leaderboard of the top stories you've cached, plus a
              breakdown of which domains are dominating today.

What this example puts in front of you, in order:

  - `@recorder(kind="hn.item.get", data=HNStory)` — name the action,
    declare what you want indexable.

  - `asyncio.gather` of 30 concurrent recorded calls. The recorder
    handles concurrent lifecycles cleanly (pending → running → completed
    per row, atomic writes via SQLite WAL).

  - `key=f"hn-item-{id}"` makes the next run free. HN items don't change
    once posted, so this is `key=` used as a real cache. Same primitive
    that prevents double-charging in a side-effecting integration.

  - `attach()` lifts a few fields out of the response into the data slot
    under your preferred names — JSON-flat enough that aggregations
    are one Python comprehension or one `json_extract(...)` away in raw
    SQL.

  - The retrospective beat: once 30 stories are in jobs.db, the *log
    is a queryable cache*. Sort by score, group by domain, count by
    poster. No separate database needed — `jobs.db` is the database.

After running, poke at it from the shell:

    sqlite3 jobs.db "
        SELECT json_extract(data_json, '$.score')   AS score,
               json_extract(data_json, '$.title')   AS title,
               json_extract(data_json, '$.domain')  AS domain
        FROM jobs
        WHERE kind='hn.item.get' AND status='completed'
        ORDER BY score DESC
        LIMIT 10;
    "
"""

import asyncio
from collections import Counter
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from recorded import attach, last, recorder

HN_API = "https://hacker-news.firebaseio.com/v0"


@dataclass
class HNStory:
    story_id: int
    title: str
    score: int
    by: str
    domain: str | None


@recorder(kind="hn.item.get", data=HNStory)
async def fetch_item(item_id: int) -> dict:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"{HN_API}/item/{item_id}.json")
        r.raise_for_status()
        body = r.json()

    # Lift the queryable bits into the data slot.
    attach("story_id", body["id"])
    attach("title", body.get("title", ""))
    attach("score", body.get("score", 0))
    attach("by", body.get("by", ""))
    url = body.get("url")
    attach("domain", urlparse(url).netloc if url else None)

    return body


async def main():
    # The index call isn't side-effecting / not worth recording — it's
    # just where to look. Stick to recording the actual item fetches.
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"{HN_API}/topstories.json")
        r.raise_for_status()
        top_ids = r.json()[:30]

    print(f">>> fetching {len(top_ids)} top stories (concurrently)...\n")
    await asyncio.gather(
        *(fetch_item(item_id, key=f"hn-item-{item_id}") for item_id in top_ids)
    )

    rows = [j for j in last(100, kind="hn.item.get") if j.data]
    rows.sort(key=lambda j: j.data.score, reverse=True)

    print("\n=== top stories by score (from the recorded log) ===\n")
    for j in rows[:10]:
        title = j.data.title[:60] + ("…" if len(j.data.title) > 60 else "")
        domain = j.data.domain or "—"
        print(f"  {j.data.score:>4}  {title}  ({domain})")

    print("\n=== domains by frequency ===\n")
    domains = Counter(j.data.domain for j in rows if j.data.domain)
    for domain, count in domains.most_common(5):
        print(f"  {count:>3}  {domain}")


if __name__ == "__main__":
    asyncio.run(main())
