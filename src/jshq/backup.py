"""Nightly backup: DB snapshot + user documents into DATA_DIR/backups, verified.

Python port of the retired scripts/backup.sh (zsh, macOS-only). Run
`jshq backup` once nightly from your scheduler of choice (launchd /
Task Scheduler / cron); it never raises to the scheduler — failures land
in backup.log, and the copy's verification result in backup_status.json,
which /api/backup/status serves for the Today-view banners.

What gets copied: the SQLite DB via the online-backup API (a plain file
copy would miss the WAL), dated keep-30 copies of the user-authored
documents, and an accumulating mirror of applications/ (uploaded resumes
and letters aren't regenerable; the mirror never deletes, so an
accidental in-app delete stays recoverable). The .env file — the API
key — is deliberately NOT backed up: it's a secret; copy it yourself.

Verification checks the backup file exists, passes PRAGMA
integrity_check, and matches the live DB's row counts table by table.
Counts are compared exactly: the backup is taken seconds earlier while
nothing writes, so any drift means a bad copy, not activity.
"""

import json
import os
import shutil
import sqlite3
from datetime import date, datetime
from pathlib import Path

from jshq import db, paths

TABLES = ["companies", "contacts", "jobs", "applications", "activities", "reminders"]
KEEP = 30

# Module-level constants (the paths.py convention): tests monkeypatch these
# attribute-by-attribute. DB_PATH mirrors db.DB_PATH; STATUS_PATH mirrors
# main.BACKUP_STATUS_PATH.
DB_PATH: Path = db.DB_PATH
BACKUPS_DIR: Path = paths.DATA_DIR / "backups"
STATUS_PATH: Path = paths.DATA_DIR / "backup_status.json"
LOG_PATH: Path = paths.DATA_DIR / "backup.log"
APPLICATIONS_DIR: Path = paths.DATA_DIR / "applications"

# User-authored documents beyond the DB, backed up as dated keep-30 copies:
# (source path, backup filename prefix, suffix). resume/content.json is the
# upstream set; the criteria doc, voice guide, and roadmap joined in Phase 5 —
# they're user-edited and were otherwise unprotected.
DOCUMENTS: tuple[tuple[Path, str, str], ...] = (
    (paths.DATA_DIR / "resume" / "content.json", "resume-content", ".json"),
    (paths.DATA_DIR / "fit_criteria.md", "fit-criteria", ".md"),
    (paths.DATA_DIR / "voice_guide.md", "voice-guide", ".md"),
    (paths.DATA_DIR / "roadmap.json", "roadmap", ".json"),
)


def _connect_ro(path: Path) -> sqlite3.Connection:
    # mode=ro: verification must never mutate either DB (no WAL recovery either).
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}


def verify(live_path: Path, backup_path: Path) -> dict:
    result: dict = {
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "backup_file": backup_path.name,
        "result": "failed",
        "detail": None,
        "integrity": None,
        "counts": None,
    }

    if not backup_path.is_file() or backup_path.stat().st_size == 0:
        result["detail"] = "backup file missing or empty"
        return result

    try:
        backup = _connect_ro(backup_path)
        try:
            integrity = backup.execute("PRAGMA integrity_check").fetchone()[0]
            result["integrity"] = integrity
            if integrity != "ok":
                result["detail"] = "integrity_check failed"
                return result
            backup_counts = _counts(backup)
        finally:
            backup.close()
    except sqlite3.Error as exc:
        result["detail"] = f"backup unreadable: {exc}"
        return result

    try:
        live = _connect_ro(live_path)
        try:
            live_counts = _counts(live)
        finally:
            live.close()
    except sqlite3.Error as exc:
        result["detail"] = f"live DB unreadable: {exc}"
        return result

    result["counts"] = {t: {"backup": backup_counts[t], "live": live_counts[t]} for t in TABLES}
    mismatched = [t for t in TABLES if backup_counts[t] != live_counts[t]]
    if mismatched:
        result["detail"] = f"row count mismatch: {', '.join(mismatched)}"
        return result

    result["result"] = "ok"
    return result


def write_status(result: dict, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, out_path)


def _log(line: str) -> None:
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(f"{stamp} {line}\n")


def _backup_db(src_path: Path, dest_path: Path) -> None:
    """SQLite online backup — captures the WAL a plain copy would miss.
    Replaces dest's content wholesale, so a same-day re-run overwrites."""
    src = sqlite3.connect(src_path)
    try:
        dst = sqlite3.connect(dest_path)
        try:
            src.backup(dst)
            # A snapshot is never written concurrently: flip it out of the
            # source's WAL mode so close() leaves ONE self-contained file.
            # (Apple's SQLite keeps -wal/-shm siblings after close otherwise,
            # and the hq-*.sqlite prune glob would never match them.)
            dst.execute("PRAGMA journal_mode=DELETE")
        finally:
            dst.close()
    finally:
        src.close()


def _prune(pattern: str) -> None:
    # Keep the newest KEEP by NAME, not mtime (upstream used `ls -1t`): the
    # ISO dates in these names sort chronologically, and name order stays
    # deterministic under the coarse file times a CI runner can produce.
    for old in sorted(BACKUPS_DIR.glob(pattern))[:-KEEP]:
        old.unlink()
        _log(f"pruned {old.name}")


def run_backup(today: date | None = None) -> dict | None:
    """One nightly backup pass. Returns the verification result, or None when
    there is no DB yet. Never raises — the scheduler must always see exit 0."""
    day = (today or date.today()).isoformat()

    if not DB_PATH.exists():
        # No status write and no backups dir: the Today view suppresses its
        # backup-missing banner on day one by the status file's ABSENCE.
        _log("no DB yet, skipping")
        return None

    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    dest = BACKUPS_DIR / f"hq-{day}.sqlite"
    try:
        _backup_db(DB_PATH, dest)
        _log(f"backed up {dest.name}")
    except (sqlite3.Error, OSError) as exc:
        # The shell script's `|| true`: a failed copy must still reach
        # verify(), which records the failure in the status file.
        _log(f"backup copy failed: {exc}")

    try:
        _prune("hq-*.sqlite")
    except OSError as exc:
        _log(f"prune failed: {exc}")

    for src, prefix, suffix in DOCUMENTS:
        if not src.is_file():
            continue
        try:
            shutil.copyfile(src, BACKUPS_DIR / f"{prefix}-{day}{suffix}")
            _log(f"backed up {prefix}-{day}{suffix}")
            _prune(f"{prefix}-*{suffix}")
        except OSError as exc:
            _log(f"{prefix} backup failed: {exc}")

    if APPLICATIONS_DIR.is_dir():
        try:
            # Accumulating mirror — copytree never deletes, so this is
            # `rsync -a` without --delete: accidental deletes stay recoverable.
            shutil.copytree(APPLICATIONS_DIR, BACKUPS_DIR / "applications", dirs_exist_ok=True)
            n = sum(1 for p in APPLICATIONS_DIR.rglob("*") if p.is_file())
            _log(f"mirrored applications/ ({n} files)")
        except OSError as exc:
            _log(f"applications mirror failed: {exc}")

    result = verify(DB_PATH, dest)
    write_status(result, STATUS_PATH)
    if result["result"] == "ok":
        _log(f"verified {result['backup_file']}: ok")
    else:
        _log(f"verified {result['backup_file']}: FAILED — {result['detail']}")
    return result
