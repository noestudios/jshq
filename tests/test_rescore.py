"""Background rescore endpoint + run_rescore (Phase 7h).

Mirrors /api/refresh: non-blocking spawn, a {"running": True} short-circuit, and
last_rescore surfaced on the status endpoint. run_scoring is stubbed so no Haiku
call or real DB connection is ever made.
"""

import asyncio

from jshq.ats import refresh as refresh_mod


def test_rescore_spawns_started(client, monkeypatch):
    async def fake_run_rescore(conn=None):
        return {"last_rescore": "now", "scoring": {}}

    monkeypatch.setattr(refresh_mod, "run_rescore", fake_run_rescore)
    resp = client.post("/api/scoring/rescore")
    assert resp.status_code == 202
    assert resp.json() == {"started": True}


def test_rescore_busy_short_circuits(client, monkeypatch):
    monkeypatch.setattr(refresh_mod, "is_running", lambda: True)
    resp = client.post("/api/scoring/rescore")
    assert resp.status_code == 200
    assert resp.json() == {"running": True}


def test_status_reports_last_rescore(client, db):
    db.execute(
        "INSERT INTO settings (key, value) VALUES ('last_rescore', ?)",
        ("2026-06-13T10:00:00+00:00",),
    )
    db.commit()
    body = client.get("/api/refresh/status").json()
    assert body["last_rescore"] == "2026-06-13T10:00:00+00:00"


def test_status_last_rescore_null_when_absent(client):
    assert client.get("/api/refresh/status").json()["last_rescore"] is None


def test_run_rescore_full_scores_and_stamps(db, monkeypatch):
    seen = {}

    async def fake_run_scoring(conn, only_pending=True, client=None, on_progress=None):
        seen["only_pending"] = only_pending
        return {"scored": 3, "tier1_failed": 1, "errors": 0}

    monkeypatch.setattr(refresh_mod.scoring, "run_scoring", fake_run_scoring)
    # fresh lock bound to this asyncio.run loop (the module lock may be bound to
    # another test's loop); pass conn so no real DB connection is opened.
    monkeypatch.setattr(refresh_mod, "_refresh_lock", asyncio.Lock())

    out = asyncio.run(refresh_mod.run_rescore(conn=db))
    assert seen["only_pending"] is False  # full rescore, not pending-only
    assert out["scoring"]["scored"] == 3
    assert out["last_rescore"]
    row = db.execute("SELECT value FROM settings WHERE key = 'last_rescore'").fetchone()
    assert row["value"] == out["last_rescore"]
    # the run also persists a report and clears the in-flight progress marker
    assert refresh_mod.SCORING_PROGRESS is None
    rep = db.execute("SELECT value FROM settings WHERE key = 'last_scoring_report'").fetchone()
    assert rep is not None


def test_rescore_estimate_shape(client):
    body = client.get("/api/scoring/rescore-estimate").json()
    assert set(body) == {"active", "to_score", "tier1_failed", "est_cost_usd"}
    # no jobs seeded -> nothing to score, zero estimated cost
    assert body["active"] == 0 and body["est_cost_usd"] == 0


# --- completion pop-ups (app.notify — osascript patched by the autouse fixture) ---


def _fake_scoring(report):
    async def fake_run_scoring(conn, only_pending=True, client=None, on_progress=None):
        return report

    return fake_run_scoring


def test_rescore_completion_sends_popup(db, monkeypatch, notify_calls):
    monkeypatch.setattr(refresh_mod.scoring, "run_scoring", _fake_scoring({"scored": 3, "errors": 0}))
    monkeypatch.setattr(refresh_mod, "_refresh_lock", asyncio.Lock())
    asyncio.run(refresh_mod.run_rescore(conn=db))
    assert len(notify_calls) == 1
    assert "Rescore complete — 3 scored" in notify_calls[0]["message"]
    assert notify_calls[0]["sound"] == "Glass"


def test_rescore_errors_use_error_sound(db, monkeypatch, notify_calls):
    monkeypatch.setattr(refresh_mod.scoring, "run_scoring", _fake_scoring({"scored": 2, "errors": 1}))
    monkeypatch.setattr(refresh_mod, "_refresh_lock", asyncio.Lock())
    asyncio.run(refresh_mod.run_rescore(conn=db))
    assert len(notify_calls) == 1
    assert "1 errors" in notify_calls[0]["message"]
    assert notify_calls[0]["sound"] == "Basso"


def test_rescore_skipped_sends_no_popup(db, monkeypatch, notify_calls):
    monkeypatch.setattr(refresh_mod.scoring, "run_scoring", _fake_scoring({"skipped": "no key"}))
    monkeypatch.setattr(refresh_mod, "_refresh_lock", asyncio.Lock())
    asyncio.run(refresh_mod.run_rescore(conn=db))
    assert notify_calls == []
