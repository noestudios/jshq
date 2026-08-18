"""jshq backup: the nightly run — snapshot, dated doc copies, mirror, prune."""

import re
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from jshq import backup
from jshq.db import connect, init_db

DAY = date(2026, 8, 15)


@pytest.fixture
def backup_env(tmp_path, monkeypatch):
    """Redirect every backup-module path constant into a scratch data dir."""
    monkeypatch.setattr(backup, "DB_PATH", tmp_path / "hq.sqlite")
    monkeypatch.setattr(backup, "BACKUPS_DIR", tmp_path / "backups")
    monkeypatch.setattr(backup, "STATUS_PATH", tmp_path / "backup_status.json")
    monkeypatch.setattr(backup, "LOG_PATH", tmp_path / "backup.log")
    monkeypatch.setattr(backup, "APPLICATIONS_DIR", tmp_path / "applications")
    monkeypatch.setattr(backup, "DOCUMENTS", (
        (tmp_path / "resume" / "content.json", "resume-content", ".json"),
        (tmp_path / "fit_criteria.md", "fit-criteria", ".md"),
        (tmp_path / "voice_guide.md", "voice-guide", ".md"),
        (tmp_path / "roadmap.json", "roadmap", ".json"),
    ))
    return tmp_path


def test_no_db_skips_without_status_or_backups_dir(backup_env):
    assert backup.run_backup(DAY) is None
    assert "no DB yet, skipping" in backup.LOG_PATH.read_text(encoding="utf-8")
    assert not backup.STATUS_PATH.exists()  # Today's day-one banner suppression
    assert not backup.BACKUPS_DIR.exists()


def test_happy_path_captures_wal_and_verifies(backup_env):
    init_db(backup.DB_PATH)
    conn = connect(backup.DB_PATH)
    try:
        conn.execute(
            "INSERT INTO companies (name, ats_type, ats_slug) VALUES ('A', 'greenhouse', 'a')"
        )
        conn.commit()
        # Connection stays open: the row lives in the WAL, not the main file —
        # a plain file copy would miss it, the online backup must not.
        result = backup.run_backup(DAY)
    finally:
        conn.close()

    assert result["result"] == "ok"
    assert result["backup_file"] == f"hq-{DAY}.sqlite"
    assert result["counts"]["companies"] == {"backup": 1, "live": 1}
    snapshot = sqlite3.connect(backup.BACKUPS_DIR / f"hq-{DAY}.sqlite")
    try:
        assert snapshot.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 1
    finally:
        snapshot.close()
    assert backup.STATUS_PATH.exists()
    # The snapshot must be ONE self-contained file — no -wal/-shm siblings
    # for the prune glob to orphan.
    assert not list(backup.BACKUPS_DIR.glob("*.sqlite-*"))


def test_same_day_rerun_overwrites(backup_env):
    init_db(backup.DB_PATH)
    assert backup.run_backup(DAY)["result"] == "ok"
    assert backup.run_backup(DAY)["result"] == "ok"
    assert [p.name for p in backup.BACKUPS_DIR.glob("hq-*.sqlite")] == [f"hq-{DAY}.sqlite"]


def test_prune_keeps_newest_thirty(backup_env):
    init_db(backup.DB_PATH)
    backup.BACKUPS_DIR.mkdir(parents=True)
    for i in range(1, 31):  # 30 older dated snapshots; today's makes 31
        (backup.BACKUPS_DIR / f"hq-2026-07-{i:02d}.sqlite").touch()
    backup.run_backup(DAY)

    names = sorted(p.name for p in backup.BACKUPS_DIR.glob("hq-*.sqlite"))
    assert len(names) == 30
    assert "hq-2026-07-01.sqlite" not in names  # oldest pruned, by name order
    assert f"hq-{DAY}.sqlite" in names
    assert "pruned hq-2026-07-01.sqlite" in backup.LOG_PATH.read_text(encoding="utf-8")


def test_documents_get_dated_copies_and_prune(backup_env, tmp_path):
    init_db(backup.DB_PATH)
    (tmp_path / "resume").mkdir()
    (tmp_path / "resume" / "content.json").write_text("{}", encoding="utf-8")
    (tmp_path / "fit_criteria.md").write_text("# criteria", encoding="utf-8")
    (tmp_path / "voice_guide.md").write_text("# voice", encoding="utf-8")
    (tmp_path / "roadmap.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=secret", encoding="utf-8")
    backup.BACKUPS_DIR.mkdir(parents=True)
    for i in range(1, 31):
        (backup.BACKUPS_DIR / f"fit-criteria-2026-07-{i:02d}.md").touch()

    backup.run_backup(DAY)

    for name in (
        f"resume-content-{DAY}.json",
        f"fit-criteria-{DAY}.md",
        f"voice-guide-{DAY}.md",
        f"roadmap-{DAY}.json",
    ):
        assert (backup.BACKUPS_DIR / name).is_file(), name
    assert (backup.BACKUPS_DIR / f"fit-criteria-{DAY}.md").read_text(encoding="utf-8") == "# criteria"
    assert len(list(backup.BACKUPS_DIR.glob("fit-criteria-*.md"))) == 30
    assert not (backup.BACKUPS_DIR / "fit-criteria-2026-07-01.md").exists()
    # The API key is a secret and is deliberately never backed up.
    assert not list(backup.BACKUPS_DIR.rglob("*.env")) and not list(backup.BACKUPS_DIR.rglob(".env"))


def test_missing_documents_are_skipped_quietly(backup_env):
    init_db(backup.DB_PATH)
    result = backup.run_backup(DAY)
    assert result["result"] == "ok"
    assert not list(backup.BACKUPS_DIR.glob("*.md"))


def test_applications_mirror_accumulates(backup_env, tmp_path):
    init_db(backup.DB_PATH)
    uploads = tmp_path / "applications" / "1"
    uploads.mkdir(parents=True)
    (uploads / "resume.pdf").write_bytes(b"pdf-bytes")
    backup.run_backup(DAY)
    assert (backup.BACKUPS_DIR / "applications" / "1" / "resume.pdf").read_bytes() == b"pdf-bytes"

    # An in-app delete must stay recoverable: the mirror never deletes.
    (uploads / "resume.pdf").unlink()
    (uploads / "cover.pdf").write_bytes(b"cover")
    backup.run_backup(DAY)
    assert (backup.BACKUPS_DIR / "applications" / "1" / "resume.pdf").read_bytes() == b"pdf-bytes"
    assert (backup.BACKUPS_DIR / "applications" / "1" / "cover.pdf").read_bytes() == b"cover"


def test_failed_copy_still_writes_failed_status(backup_env, monkeypatch):
    init_db(backup.DB_PATH)

    def boom(src, dest):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(backup, "_backup_db", boom)
    result = backup.run_backup(DAY)

    assert result["result"] == "failed"
    assert result["detail"] == "backup file missing or empty"
    assert backup.STATUS_PATH.exists()
    log = backup.LOG_PATH.read_text(encoding="utf-8")
    assert "backup copy failed: disk I/O error" in log
    assert "FAILED" in log


def test_log_lines_are_iso_stamped(backup_env):
    init_db(backup.DB_PATH)
    backup.run_backup(DAY)
    for line in backup.LOG_PATH.read_text(encoding="utf-8").splitlines():
        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2} ", line), line
