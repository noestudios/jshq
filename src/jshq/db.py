"""SQLite access. Raw sqlite3 by design — keep it simple."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

from jshq import paths

DB_PATH = paths.DATA_DIR / "hq.sqlite"
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    # check_same_thread=False: FastAPI creates the per-request connection in a
    # threadpool thread but the async endpoint uses it on the event loop. Each
    # connection is request-scoped and used sequentially, so this is safe.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    # Wait up to 5s for a competing writer instead of failing immediately with
    # SQLITE_BUSY. The launchd refresh runs in its own process alongside the
    # uvicorn API, so the per-process _refresh_lock can't coordinate across
    # them; WAL lets readers run during a write, and this covers writer↔writer.
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


# Columns added after a table first shipped. CREATE TABLE IF NOT EXISTS skips
# existing tables, so these are applied via ALTER TABLE when missing (SQLite
# has no ADD COLUMN IF NOT EXISTS). Schema v2 (Phase 3b), v3 (Phase 4),
# v4 (Phase 5), v5 (Phase 7c). ALTER TABLE ADD COLUMN rejects non-constant
# defaults, so the reminder/application timestamps are plain TEXT here; code
# writes both explicitly.
_COLUMN_MIGRATIONS = {
    "jobs": [
        ("miss_count", "INTEGER NOT NULL DEFAULT 0"),
        ("manually_elevated", "INTEGER NOT NULL DEFAULT 0"),
        ("source", "TEXT NOT NULL DEFAULT 'ats'"),  # 'ats' | 'manual' (hand-entered, decay-exempt)
        ("manually_edited", "INTEGER NOT NULL DEFAULT 0"),  # user corrected location/salary; refresh won't clobber those fields
        ("score_detail", "TEXT"),  # JSON scoring components (v8; per-criterion subscores added 2026-08) — shape in schema.sql; NULL = tier1 fail / pre-redesign
    ],
    "companies": [
        ("ats_last_checked", "TEXT"),
        ("ats_last_status", "TEXT"),
        ("sector_flags", "TEXT"),  # JSON array, e.g. ["healthcare"]; NULL = none
        ("logo_ext", "TEXT"),  # ext of the cached logo at data/logos/{id}.{ext}; NULL = monogram
    ],
    "reminders": [
        ("created_at", "TEXT"),
        ("updated_at", "TEXT"),
    ],
    "applications": [
        ("created_at", "TEXT"),
        ("updated_at", "TEXT"),
    ],
}


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    for table, cols in _COLUMN_MIGRATIONS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _backfill_next_steps(conn: sqlite3.Connection) -> None:
    """One-time v10 migration: promote applications.next_step/next_step_date
    into next_steps rows. Gated by a settings flag rather than NOT EXISTS —
    after go-live a user can legitimately delete every row for an app, and a
    bare existence check would resurrect it at next startup. The legacy fields
    are nulled after migrating (belt and suspenders). The backfilled uid keeps
    the legacy app-nextstep-{app_id} shape so already-subscribed calendars
    update in place; a closed app's leftover value migrates as dismissed
    history, not live work. COALESCE(status,'') deliberately treats NULL-status
    applications as open."""
    done = conn.execute(
        "SELECT value FROM settings WHERE key = 'next_steps_backfilled'"
    ).fetchone()
    if done and done["value"] == "1":
        return
    conn.execute(
        """INSERT INTO next_steps
               (application_id, title, due_date, status, ics_uid, created_at, updated_at)
           SELECT a.id,
                  COALESCE(NULLIF(TRIM(a.next_step), ''), 'Next step'),
                  NULLIF(a.next_step_date, ''),
                  CASE WHEN COALESCE(a.status, '') IN ('rejected', 'withdrawn')
                       THEN 'dismissed' ELSE 'pending' END,
                  'app-nextstep-' || a.id || '@jobsearchhq',
                  a.updated_at, a.updated_at
           FROM applications a
           WHERE TRIM(COALESCE(a.next_step, '')) != ''
              OR COALESCE(a.next_step_date, '') != ''"""
    )
    conn.execute(
        """UPDATE applications SET next_step = NULL, next_step_date = NULL
           WHERE TRIM(COALESCE(next_step, '')) != ''
              OR COALESCE(next_step_date, '') != ''"""
    )
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('next_steps_backfilled', '1')"
    )


def init_db(db_path: Path = DB_PATH) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        _add_missing_columns(conn)
        _backfill_next_steps(conn)
        # 'checking' is stamped and committed BEFORE the fire-and-forget detect/
        # pull task runs; the task writes the terminal status only on completion.
        # A process interrupted mid-check (Ctrl-C, crash, or a shutdown cancel,
        # which raises CancelledError past the worker's `except Exception`) leaves
        # the row stuck at 'checking' forever -- both recovery endpoints
        # short-circuit on it and the UI renders only a perpetual spinner. No task
        # can be running at process start, so any 'checking' here is stale: clear
        # it back to "not checked yet" so detect/refresh become reachable again.
        conn.execute(
            "UPDATE companies SET ats_last_status = NULL WHERE ats_last_status = 'checking'"
        )
        conn.commit()
    finally:
        conn.close()


def get_db() -> Iterator[sqlite3.Connection]:
    """FastAPI dependency: one connection per request."""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()
