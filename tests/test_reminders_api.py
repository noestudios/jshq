"""Reminders CRUD (Phase 5)."""


def test_create_minimal(client):
    r = client.post("/api/reminders", json={"title": "Renew passport", "due_date": "2026-07-01"})
    assert r.status_code == 201
    body = r.json()
    assert body["title"] == "Renew passport"
    assert body["type"] == "custom"
    assert body["done"] is False
    assert body["ics_uid"].endswith("@jobsearchhq")
    assert body["created_at"] and body["updated_at"]
    assert body["entity_label"] is None


def test_create_with_job_entity_resolves_label(client, seed_job):
    job_id = seed_job()
    r = client.post(
        "/api/reminders",
        json={
            "title": "Follow up",
            "type": "followup_application",
            "entity_type": "job",
            "entity_id": job_id,
            "due_date": "2026-06-20",
            "due_time": "09:30",
        },
    )
    assert r.status_code == 201
    assert r.json()["entity_label"] == "Product Designer @ TestCo"


def test_create_with_contact_entity(client, db, seed_company):
    cid = seed_company()
    db.execute("INSERT INTO contacts (name, company_id) VALUES ('Dana', ?)", (cid,))
    db.commit()
    contact_id = db.execute("SELECT id FROM contacts").fetchone()["id"]
    r = client.post(
        "/api/reminders",
        json={"title": "Ping Dana", "entity_type": "contact", "entity_id": contact_id,
              "due_date": "2026-06-20"},
    )
    assert r.json()["entity_label"] == "Dana"


def test_create_validation(client):
    # entity_type without entity_id
    r = client.post(
        "/api/reminders",
        json={"title": "x", "entity_type": "job", "due_date": "2026-06-20"},
    )
    assert r.status_code == 422
    # bad time format
    r = client.post(
        "/api/reminders", json={"title": "x", "due_date": "2026-06-20", "due_time": "9am"}
    )
    assert r.status_code == 422
    # nonexistent entity
    r = client.post(
        "/api/reminders",
        json={"title": "x", "entity_type": "job", "entity_id": 999, "due_date": "2026-06-20"},
    )
    assert r.status_code == 400


def test_list_sorts_done_last_then_due(client, seed_reminder):
    seed_reminder(title="b", due_date="2026-06-20")
    seed_reminder(title="done", due_date="2026-06-01", done=1)
    seed_reminder(title="a", due_date="2026-06-10")
    titles = [r["title"] for r in client.get("/api/reminders").json()]
    assert titles == ["a", "b", "done"]


def test_put_updates_and_preserves_uid(client, seed_reminder):
    rid = seed_reminder()
    before = client.get("/api/reminders").json()[0]
    r = client.put(
        f"/api/reminders/{rid}",
        json={"title": "Renamed", "type": "meeting", "due_date": "2026-06-16",
              "due_time": "14:00", "notes": "bring questions"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["title"] == "Renamed"
    assert body["ics_uid"] == before["ics_uid"]
    assert body["updated_at"] > before["updated_at"]
    assert body["done"] is False

    assert client.put("/api/reminders/999", json={"title": "x", "due_date": "2026-06-16"}).status_code == 404


def test_patch_done_and_snooze(client, seed_reminder):
    rid = seed_reminder(due_date="2026-06-10")
    r = client.patch(f"/api/reminders/{rid}", json={"done": True})
    assert r.json()["done"] is True
    r = client.patch(f"/api/reminders/{rid}", json={"done": False, "due_date": "2026-06-17"})
    body = r.json()
    assert body["done"] is False
    assert body["due_date"] == "2026-06-17"
    assert body["updated_at"] > "2026-01-01 00:00:00"

    assert client.patch(f"/api/reminders/{rid}", json={}).status_code == 422
    assert client.patch("/api/reminders/999", json={"done": True}).status_code == 404


def test_patch_rejects_explicit_nulls(client, seed_reminder):
    # None doubles as the not-sent sentinel; an explicit null used to reach
    # the handler — int(None) 500ed for done, and a nulled due_date wrote SQL
    # NULL, after which every ICS endpoint 500ed on that row (a subscribed
    # calendar feed poisoned until the reminder was fixed or deleted).
    rid = seed_reminder(due_date="2026-06-10", due_time="09:00")
    assert client.patch(f"/api/reminders/{rid}", json={"done": None}).status_code == 422
    assert client.patch(f"/api/reminders/{rid}", json={"due_date": None}).status_code == 422
    # an explicitly nulled due_time is legitimate: time-less reminders exist
    r = client.patch(f"/api/reminders/{rid}", json={"due_time": None})
    assert r.status_code == 200
    assert r.json()["due_time"] is None
    assert client.get("/api/calendar.ics").status_code == 200


def test_rejects_out_of_range_time(client, seed_reminder):
    # The old pattern r"^\d{2}:\d{2}$" matched digit shape only, so "24:00" /
    # "25:99" / "00:60" reached the DB, then build_event's datetime(...) raised
    # ValueError and 500ed every ICS endpoint (a subscribed feed poisoned by one
    # row). The range-checking pattern 422s them at the API instead.
    for bad in ("24:00", "25:99", "00:60", "23:99", "99:99"):
        r = client.post(
            "/api/reminders", json={"title": "x", "due_date": "2026-06-20", "due_time": bad}
        )
        assert r.status_code == 422, f"{bad} should be rejected"
    for ok in ("00:00", "23:59", "09:30"):
        r = client.post(
            "/api/reminders", json={"title": "x", "due_date": "2026-06-20", "due_time": ok}
        )
        assert r.status_code == 201, f"{ok} should be accepted"
    # PATCH validates on the same pattern.
    rid = seed_reminder(due_date="2026-06-10", due_time="09:00")
    assert client.patch(f"/api/reminders/{rid}", json={"due_time": "24:00"}).status_code == 422
    assert client.patch(f"/api/reminders/{rid}", json={"due_time": "23:59"}).status_code == 200


def test_delete(client, seed_reminder):
    rid = seed_reminder()
    assert client.delete(f"/api/reminders/{rid}").json() == {"deleted": rid}
    assert client.get("/api/reminders").json() == []
    assert client.delete(f"/api/reminders/{rid}").status_code == 404
