"""jshq console entry point: serve the dashboard, or run one scheduled job.

`jshq` (or `jshq serve`) runs the single-process app on 127.0.0.1.
`jshq refresh` runs the twice-daily ATS ingestion once and exits 0 —
point launchd / Task Scheduler / cron at it; partial failures are
recorded in companies.ats_last_status, never raised to the scheduler.
`jshq backup` takes one verified nightly backup the same way: always
exit 0, failures land in backup.log and backup_status.json.
`jshq schedule --install` writes those scheduler entries for you
(launchd / crontab / schtasks); --status and --uninstall inspect and
remove them. Never silent: unsupported systems get the manual
instructions printed instead.

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
        m = result.get("manual", {})
        if m.get("error"):
            lines.append(f"  manual-liveness: crashed ({m['error']})")
        elif m.get("checked"):
            lines.append(
                f"  manual-liveness: {m['checked']} checked, {m['gone']} gone, {m['closed']} closed"
            )
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


def schedule_job(args) -> int:
    """Install, remove, or report the OS scheduler entries for `jshq refresh`
    and `jshq backup`. Times resolve flag → settings row → defaults; a
    flag-driven install persists its times to the settings row first, so the
    row stays the one source of truth the Settings UI edits too."""
    from jshq import db, schedule

    db.init_db()  # the settings row must exist before we read it
    conn = db.connect()
    try:
        try:
            times = schedule.read_times(conn)
            if args.refresh_time:
                times["refresh"] = schedule.parse_times(args.refresh_time)
            if args.backup_time:
                times["backup"] = schedule.parse_times(args.backup_time)
        except schedule.ScheduleError as exc:
            print(str(exc))
            return 1

        if args.install:
            if args.refresh_time or args.backup_time:
                schedule.write_times(conn, times)
            result = schedule.install(times)
            if not result["supported"]:
                print(result["manual"], end="")
                return 1
            if not result["ok"]:
                print(f"install FAILED — {result['error']}")
                return 1
            print(
                f"installed: refresh at {', '.join(times['refresh'])}; "
                f"backup at {', '.join(times['backup'])}"
            )
            return 0

        if args.uninstall:
            result = schedule.uninstall()
            if not result["supported"]:
                print(result["manual"], end="")
                return 1
            if not result["ok"]:
                print(f"uninstall FAILED — {result['error']}")
                return 1
            print("removed jshq scheduler entries")
            return 0

        # --status (the default)
        st = schedule.status(conn)
        if not st["supported"]:
            print(st["manual"], end="")
            return 0
        print(f"scheduler: {st['platform']}")
        for job in schedule.JOBS:
            state = "installed" if st["installed"][job] else "not installed"
            print(f"  {job}: {state} (times: {', '.join(st['times'][job])})")
        print(f"  command: {' '.join(st['command'])}")
        print(f"  data dir: {st['data_dir']}")
        return 0
    finally:
        conn.close()


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
    schedule = sub.add_parser(
        "schedule", help="install, remove, or inspect the OS scheduler entries"
    )
    mode = schedule.add_mutually_exclusive_group()
    mode.add_argument("--install", action="store_true", help="write and load the entries (idempotent)")
    mode.add_argument("--uninstall", action="store_true", help="remove the entries")
    mode.add_argument("--status", action="store_true", help="report installed state (the default)")
    schedule.add_argument(
        "--refresh-time", action="append", metavar="HH:MM",
        help="refresh time, 24-hour; repeat for several runs a day (default 10:00 and 16:00)",
    )
    schedule.add_argument(
        "--backup-time", action="append", metavar="HH:MM",
        help="backup time, 24-hour; repeatable (default 02:00)",
    )
    args = parser.parse_args(argv)

    _load_env()
    if args.command == "refresh":
        refresh_job()
        return 0
    if args.command == "backup":
        backup_job()
        return 0
    if args.command == "schedule":
        return schedule_job(args)

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
