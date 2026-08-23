"""/api/schedule: stored times + live status, install/uninstall verbs.

Same isolation contract as tests/test_schedule.py: schedule._run is
monkeypatched and LAUNCH_AGENTS_DIR points at tmp_path, so no test ever
touches the real host scheduler. The backend is pinned to launchd so the
suite behaves identically on every CI platform.
"""

import subprocess

import pytest

from jshq import schedule


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(schedule, "backend", lambda: "launchd")
    monkeypatch.setattr(schedule, "LAUNCH_AGENTS_DIR", tmp_path / "LaunchAgents")
    calls = []

    def fake_run(cmd, input_text=None):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(schedule, "_run", fake_run)
    return calls


def test_get_schedule_defaults(client):
    r = client.get("/api/schedule")
    assert r.status_code == 200
    body = r.json()
    assert body["supported"] is True
    assert body["platform"] == "launchd"
    assert body["times"] == schedule.DEFAULT_TIMES
    assert body["installed"] == {"refresh": False, "backup": False}


def test_put_schedule_round_trips_and_normalizes(client):
    r = client.put("/api/schedule", json={"refresh": ["16:00", "8:30"], "backup": ["1:00"]})
    assert r.status_code == 200
    assert r.json()["times"] == {"refresh": ["08:30", "16:00"], "backup": ["01:00"]}
    assert client.get("/api/schedule").json()["times"] == {
        "refresh": ["08:30", "16:00"],
        "backup": ["01:00"],
    }


@pytest.mark.parametrize(
    "body",
    [
        {"refresh": [], "backup": ["02:00"]},
        {"refresh": ["25:00"], "backup": ["02:00"]},
        {"refresh": ["10:00"], "backup": ["noonish"]},
    ],
)
def test_put_schedule_422_with_code_on_bad_times(client, body):
    r = client.put("/api/schedule", json=body)
    assert r.status_code == 422
    assert "[JSHQ-209]" in r.json()["detail"]


def test_install_uses_stored_times_and_reports_installed(client, tmp_path):
    client.put("/api/schedule", json={"refresh": ["07:15"], "backup": ["23:00"]})
    r = client.post("/api/schedule/install")
    assert r.status_code == 200
    body = r.json()
    assert body["installed"] == {"refresh": True, "backup": True}
    plist = (tmp_path / "LaunchAgents" / "com.jshq.refresh.plist").read_text(encoding="utf-8")
    assert "<integer>7</integer>" in plist and "<integer>15</integer>" in plist


def test_uninstall_removes_entries(client, tmp_path):
    client.post("/api/schedule/install")
    r = client.post("/api/schedule/uninstall")
    assert r.status_code == 200
    assert r.json()["installed"] == {"refresh": False, "backup": False}
    assert list((tmp_path / "LaunchAgents").glob("*.plist")) == []


def test_unsupported_platform_is_a_coded_422(client, monkeypatch):
    monkeypatch.setattr(schedule, "backend", lambda: None)
    for path in ("/api/schedule/install", "/api/schedule/uninstall"):
        r = client.post(path)
        assert r.status_code == 422
        assert "[JSHQ-210]" in r.json()["detail"]
    status = client.get("/api/schedule").json()
    assert status["supported"] is False and "manual" in status


def test_apply_failure_is_a_coded_500_with_the_scheduler_detail(client, monkeypatch):
    def failing_run(cmd, input_text=None):
        rc = 1 if cmd[:2] == ["launchctl", "load"] else 0
        return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="Load failed: 5")

    monkeypatch.setattr(schedule, "_run", failing_run)
    r = client.post("/api/schedule/install")
    assert r.status_code == 500
    detail = r.json()["detail"]
    assert "[JSHQ-211]" in detail and "Load failed: 5" in detail


def test_put_stores_exactly_what_the_cli_resolver_reads(client, db):
    """One source of truth: the row the API writes is the row
    schedule.read_times (the CLI path) reads."""
    client.put("/api/schedule", json={"refresh": ["06:00"], "backup": ["04:00"]})
    assert schedule.read_times(db) == {"refresh": ["06:00"], "backup": ["04:00"]}