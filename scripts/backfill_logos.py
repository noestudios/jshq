"""One-off backfill of company logos against the live DB.

Derives each company's brand domain from its website (or careers URL) and caches
a logo at data/logos/{id}.{ext}; companies with no fetchable logo are left blank
and render a monogram in the UI. Keyless (apple-touch-icon + DuckDuckGo), so —
unlike the scoring scripts — this needs no .env / API key.

Usage: .venv/bin/python scripts/backfill_logos.py [--force] [--id N ...]
  --force  re-fetch even companies that already have a cached logo
  --id     only these company ids (repeatable)
"""

import argparse
import asyncio
import sys
from pathlib import Path


from jshq import db, logos  # noqa: E402


async def _run(force: bool, only_ids: list[int] | None) -> None:
    conn = db.connect()
    try:
        rows = conn.execute("SELECT id, name, logo_ext FROM companies ORDER BY id").fetchall()
        targets = [
            r
            for r in rows
            if (only_ids is None or r["id"] in only_ids) and (force or not r["logo_ext"])
        ]
        got = 0
        for r in targets:
            ok = await logos.refresh_company_logo(conn, r["id"])
            ext = conn.execute(
                "SELECT logo_ext FROM companies WHERE id = ?", (r["id"],)
            ).fetchone()["logo_ext"]
            print(f"{'✓' if ok else '·'} {r['id']:>3}  {r['name'][:30]:<30}  {ext or 'monogram'}")
            got += int(ok)
        print(f"\n{got}/{len(targets)} fetched a logo; {len(targets) - got} on monogram.")
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="re-fetch even cached logos")
    parser.add_argument("--id", type=int, action="append", dest="ids", help="only these ids")
    args = parser.parse_args()
    db.init_db()
    asyncio.run(_run(args.force, args.ids))


if __name__ == "__main__":
    main()
