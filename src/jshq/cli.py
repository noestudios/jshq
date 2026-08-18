"""jshq console entry point: serve the dashboard, or run one scheduled job.

`jshq` (or `jshq serve`) runs the single-process app on 127.0.0.1.
`jshq refresh` runs the twice-daily ATS ingestion once and exits 0 —
point launchd / Task Scheduler / cron at it; partial failures are
recorded in companies.ats_last_status, never raised to the scheduler.
`jshq backup` takes one verified nightly backup the same way: always
exit 0, failures land in backup.log and backup_status.json.

Import discipline: nothing from jshq may be imported at module level.
jshq.paths freezes DATA_DIR at first import, and the cwd .env loaded in
_load_env() stage 1 may set JSHQ_DATA_DIR (the dev-checkout flow).
"""

import argparse
import asyncio

# "JSHQ" on a phone keypad. Deliberately not 8000/3000/5173 (dev-server
# collisions) or 5000 (macOS AirPlay Receiver).
DEFAULT_PORT = 5747


def _load_env() -> None:
    from dotenv import load_dotenv

    # Stage 1: a .env found from the cwd (dev checkout) — may set
    # JSHQ_DATA_DIR, so it must run before jshq.paths is imported.
    load_dotenv()
    from jshq import paths

    # Stage 2: the user's own .env in the data dir (ANTHROPIC_API_KEY lives
    # here for installed copies; scheduled runs have no shell env).
    load_dotenv(paths.DATA_DIR / ".env")
    paths.seed_data_dir()


def refresh_job() -> None:
    """One full ATS refresh pass, logged to <data dir>/refresh.log."""
    from jshq import db, paths
    from jshq.ats.refresh import run_refresh

    log_path = paths.DATA_DIR / "refresh.log"
    db.init_db()  # apply any pending schema migrations before writing
    result = asyncio.run(run_refresh())
    if result.get("outage"):
        # Total connectivity outage (offline/asleep): last_refresh was left
        # untouched so the next run retries. Log a single line and exit.
        text = f"{result['at']} refresh skipped — no network ({result['attempted']} boards); will retry"
    else:
        lines = [f"{result['last_refresh']} refresh run ({len(result['companies'])} companies)"]
        for entry in result["companies"]:
            extra = ""
            if "new" in entry:
                extra = f", {entry['new']} new, {entry['closed']} closed"
            lines.append(f"  {entry['company']}: {entry['status']}{extra}")
        s = result.get("scoring", {})
        if "skipped" in s:
            lines.append(f"  scoring: skipped ({s['skipped']})")
        else:
            lines.append(
                f"  scoring: {s.get('scored', 0)} scored, "
                f"{s.get('tier1_failed', 0)} tier1-failed, {s.get('errors', 0)} errors"
            )
        text = "\n".join(lines)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(text + "\n")
    print(text)


def backup_job() -> None:
    """One verified backup pass into <data dir>/backups, logged to backup.log."""
    from jshq.backup import run_backup

    result = run_backup()
    if result is None:
        print("no DB yet, skipping")
    elif result["result"] == "ok":
        print(f"backed up and verified {result['backup_file']}")
    else:
        print(f"backup FAILED — {result['detail']} (see backup_status.json)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jshq",
        description="Local-first, zero-tracking job-search dashboard.",
    )
    sub = parser.add_subparsers(dest="command")
    serve = sub.add_parser("serve", help="run the dashboard (the default)")
    # No --host flag, ever: binding beyond 127.0.0.1 is refused by design.
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve.add_argument("--reload", action="store_true", help="dev auto-reload")
    sub.add_parser("refresh", help="run the twice-daily ATS refresh once and exit")
    sub.add_parser("backup", help="run the nightly verified backup once and exit")
    args = parser.parse_args(argv)

    _load_env()
    if args.command == "refresh":
        refresh_job()
        return 0
    if args.command == "backup":
        backup_job()
        return 0

    import uvicorn

    uvicorn.run(
        "jshq.main:app",
        host="127.0.0.1",
        port=getattr(args, "port", DEFAULT_PORT),
        reload=getattr(args, "reload", False),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
