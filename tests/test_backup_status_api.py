"""GET /api/backup/status: serves the backup_status.json written by `jshq backup`."""

import json

import pytest

import jshq.main as main


@pytest.fixture
def status_path(tmp_path, monkeypatch):
    path = tmp_path / "backup_status.json"
    monkeypatch.setattr(main, "BACKUP_STATUS_PATH", path)
    return path


def test_missing_file(client, status_path):
    assert client.get("/api/backup/status").json() == {"present": False}


def test_ok_status_round_trips(client, status_path):
    contents = {
        "checked_at": "2026-06-11T02:00:05-04:00",
        "backup_file": "hq-2026-06-11.sqlite",
        "result": "ok",
        "detail": None,
        "integrity": "ok",
        "counts": {"jobs": {"backup": 3, "live": 3}},
    }
    status_path.write_text(json.dumps(contents))
    assert client.get("/api/backup/status").json() == {"present": True, **contents}


def test_malformed_file_reports_failed(client, status_path):
    status_path.write_text("{not json")
    body = client.get("/api/backup/status").json()
    assert body == {"present": True, "result": "failed", "detail": "status file unreadable"}
