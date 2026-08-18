"""jshq.backup verification: backup integrity + row-count checks."""

import json
import sqlite3
from pathlib import Path

from jshq.backup import TABLES, verify, write_status
from jshq.db import init_db


def _make_backup(db_path, tmp_path) -> Path:
    """Same mechanism as `jshq backup` — a plain file copy would miss the WAL."""
    backup = tmp_path / "backup.sqlite"
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(backup)
    src.backup(dst)
    dst.close()
    src.close()
    return backup


def test_ok_path(db_path, tmp_path, seed_company, seed_job, seed_reminder):
    seed_job(company_id=seed_company())
    seed_reminder()
    backup = _make_backup(db_path, tmp_path)
    result = verify(db_path, backup)
    assert result["result"] == "ok"
    assert result["detail"] is None
    assert result["integrity"] == "ok"
    assert result["backup_file"] == "backup.sqlite"
    assert set(result["counts"]) == set(TABLES)
    assert result["counts"]["jobs"] == {"backup": 1, "live": 1}


def test_missing_backup_file(db_path, tmp_path):
    result = verify(db_path, tmp_path / "nope.sqlite")
    assert result["result"] == "failed"
    assert "missing" in result["detail"]


def test_empty_backup_file(db_path, tmp_path):
    backup = tmp_path / "empty.sqlite"
    backup.touch()
    result = verify(db_path, backup)
    assert result["result"] == "failed"
    assert "missing or empty" in result["detail"]


def test_garbage_backup_fails(db_path, tmp_path):
    backup = tmp_path / "garbage.sqlite"
    backup.write_bytes(b"this is not a sqlite database, not even close")
    result = verify(db_path, backup)
    assert result["result"] == "failed"
    assert "unreadable" in result["detail"]


def test_row_count_mismatch_names_table(db_path, tmp_path, db, seed_company):
    backup = _make_backup(db_path, tmp_path)  # backup taken before the insert
    seed_company()
    result = verify(db_path, backup)
    assert result["result"] == "failed"
    assert result["detail"] == "row count mismatch: companies"
    assert result["counts"]["companies"] == {"backup": 0, "live": 1}


def test_missing_live_db(tmp_path):
    backup = tmp_path / "backup.sqlite"
    init_db(backup)
    result = verify(tmp_path / "gone.sqlite", backup)
    assert result["result"] == "failed"
    assert "live DB unreadable" in result["detail"]


def test_write_status_round_trip_and_replace(db_path, tmp_path):
    out = tmp_path / "status" / "backup_status.json"
    backup = _make_backup(db_path, tmp_path)

    write_status(verify(db_path, backup), out)
    first = json.loads(out.read_text(encoding="utf-8"))
    assert first["result"] == "ok"

    write_status(verify(db_path, tmp_path / "nope.sqlite"), out)
    second = json.loads(out.read_text(encoding="utf-8"))
    assert second["result"] == "failed"
    assert not out.with_suffix(out.suffix + ".tmp").exists()
