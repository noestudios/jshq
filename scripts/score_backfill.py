"""One-off / ad-hoc fit scoring run against the live DB.

The refresh pipeline scores pending jobs automatically; this script exists for
the controlled first run (watch output and cost before trusting the wiring)
and for re-runs after editing fit_criteria.md with --rescore-all.

Usage: .venv/bin/python scripts/score_backfill.py [--rescore-all]
                                                  [--statuses a,b,c]
"""

import argparse
import asyncio
import sys
from pathlib import Path


from dotenv import load_dotenv  # noqa: E402

from jshq import db, scoring  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(REPO_ROOT / ".env")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rescore-all", action="store_true",
        help="rescore every job in --statuses, not just unscored ones",
    )
    parser.add_argument(
        "--statuses", default="active",
        help="comma-separated job statuses to score (default: active). Widening "
             "this rescores rows that are done with — applied/dismissed/closed "
             "keep old-rubric scores after a normal rescore, which is how the "
             "old model's habitual 82/72 survived as visible clusters. Only fit "
             "columns are written; status and application records are untouched.",
    )
    parser.add_argument(
        "--only-scored", action="store_true",
        help="restrict to rows that already carry an AI score — the 'get these "
             "off the old rubric' operation. Without it, widening --statuses "
             "also picks up rows dismissed or applied BEFORE they were ever "
             "scored, doubling the spend on jobs that are done with.",
    )
    args = parser.parse_args()
    statuses = tuple(s.strip() for s in args.statuses.split(",") if s.strip())

    db.init_db()
    conn = db.connect()
    try:
        report = asyncio.run(
            scoring.run_scoring(
                conn, only_pending=not args.rescore_all, statuses=statuses,
                only_scored=args.only_scored,
            )
        )
    finally:
        conn.close()
    print(report)
    if report.get("skipped"):
        sys.exit(1)


if __name__ == "__main__":
    main()
