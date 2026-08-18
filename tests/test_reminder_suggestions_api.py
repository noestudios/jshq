"""Reminder-suggestion lifecycle through the API (accept/ignore, never auto-applied)."""

from datetime import date, timedelta


def _reminder_suggestions(client):
    return client.get("/api/suggestions").json()["reminders"]


def test_applied_job_yields_followup_suggestion(client, seed_job):
    job_id = seed_job(title="Head of Design")
    client.patch(f"/api/jobs/{job_id}", json={"status": "applied"})

    [s] = _reminder_suggestions(client)
    assert s["type"] == "followup_application"
    assert s["due_date"] == (date.today() + timedelta(days=7)).isoformat()
    assert "Head of Design @ TestCo" in s["title"]


def test_accept_creates_reminder_and_retires_key(client, seed_job):
    job_id = seed_job()
    client.patch(f"/api/jobs/{job_id}", json={"status": "applied"})
    [s] = _reminder_suggestions(client)

    r = client.post("/api/suggestions/reminder", json={"key": s["key"], "action": "accept"})
    assert r.status_code == 200
    reminder = r.json()["reminder"]
    assert reminder["type"] == "followup_application"
    assert reminder["due_date"] == s["due_date"]
    assert reminder["entity_type"] == "job" and reminder["entity_id"] == job_id
    assert reminder["ics_uid"].endswith("@jobsearchhq")

    assert _reminder_suggestions(client) == []
    # stale re-accept: key is now ignored, so recompute can't find it
    r = client.post("/api/suggestions/reminder", json={"key": s["key"], "action": "accept"})
    assert r.status_code == 404
    assert len(client.get("/api/reminders").json()) == 1


def test_ignore_suppresses_without_creating(client, db, seed_company):
    cid = seed_company()
    db.execute("INSERT INTO contacts (name, company_id) VALUES ('Dana', ?)", (cid,))
    db.commit()
    contact_id = db.execute("SELECT id FROM contacts").fetchone()["id"]
    client.post(
        "/api/activities",
        json={"entity_type": "contact", "entity_id": contact_id, "type": "meeting"},
    )

    [s] = _reminder_suggestions(client)
    assert s["title"] == "Ping Dana"
    r = client.post("/api/suggestions/reminder", json={"key": s["key"], "action": "ignore"})
    assert r.status_code == 200 and r.json()["reminder"] is None
    assert _reminder_suggestions(client) == []
    assert client.get("/api/reminders").json() == []


def test_manual_reminder_suppresses_suggestion(client, seed_job):
    job_id = seed_job()
    client.patch(f"/api/jobs/{job_id}", json={"status": "applied"})
    client.post(
        "/api/reminders",
        json={"title": "my own follow-up", "type": "followup_application",
              "entity_type": "job", "entity_id": job_id, "due_date": "2026-12-01"},
    )
    assert _reminder_suggestions(client) == []
