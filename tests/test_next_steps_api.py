"""CRUD + resolution semantics for /api/next-steps. Completing or dismissing a
step is a status flip that keeps the row and writes an application-scoped
'next_step' activity (the durable record); flipping back to pending is the
silent-undo convention borrowed from reminders."""

import json


def _activities(db, app_id):
    return db.execute(
        """SELECT * FROM activities WHERE entity_type = 'application'
           AND entity_id = ? AND type = 'next_step' ORDER BY id""",
        (app_id,),
    ).fetchall()


def test_create(client, db, seed_application):
    app_id = seed_application(status="applied")
    r = client.post("/api/next-steps",
                    json={"application_id": app_id, "title": "Send work samples",
                          "due_date": "2026-06-20"})
    assert r.status_code == 201
    body = r.json()
    assert body["application_id"] == app_id
    assert body["title"] == "Send work samples"
    assert body["due_date"] == "2026-06-20"
    assert body["status"] == "pending"
    assert body["ics_uid"].endswith("@jobsearchhq")
    assert body["entity_label"] == "Product Designer @ TestCo"


def test_create_dateless(client, seed_application):
    app_id = seed_application()
    r = client.post("/api/next-steps", json={"application_id": app_id, "title": "Follow up"})
    assert r.status_code == 201
    assert r.json()["due_date"] is None


def test_create_missing_application_400(client):
    r = client.post("/api/next-steps", json={"application_id": 999, "title": "X"})
    assert r.status_code == 400


def test_patch_done_sets_resolved_and_logs(client, db, seed_application, seed_next_step):
    app_id = seed_application(status="screen")
    ns_id = seed_next_step(application_id=app_id, title="Recruiter screen",
                           due_date="2026-08-28")
    r = client.patch(f"/api/next-steps/{ns_id}", json={"status": "done"})
    assert r.status_code == 200
    assert r.json()["status"] == "done"
    assert r.json()["resolved_at"] is not None
    [act] = _activities(db, app_id)
    assert json.loads(act["content"]) == {
        "action": "done", "title": "Recruiter screen", "due_date": "2026-08-28",
    }


def test_patch_dismiss_logs(client, db, seed_application, seed_next_step):
    app_id = seed_application(status="screen")
    ns_id = seed_next_step(application_id=app_id, title="Old plan", due_date=None)
    client.patch(f"/api/next-steps/{ns_id}", json={"status": "dismissed"})
    [act] = _activities(db, app_id)
    assert json.loads(act["content"]) == {
        "action": "dismissed", "title": "Old plan", "due_date": None,
    }


def test_patch_back_to_pending_clears_resolved_no_activity(client, db, seed_application,
                                                           seed_next_step):
    """Undo is silent (reminders convention); the earlier resolution's activity
    row stays in the timeline."""
    app_id = seed_application(status="screen")
    ns_id = seed_next_step(application_id=app_id, status="done",
                           resolved_at="2026-06-01T00:00:00+00:00")
    r = client.patch(f"/api/next-steps/{ns_id}", json={"status": "pending"})
    assert r.json()["status"] == "pending"
    assert r.json()["resolved_at"] is None
    assert _activities(db, app_id) == []


def test_patch_done_to_dismissed_flips_without_second_log(client, db, seed_application,
                                                          seed_next_step):
    app_id = seed_application(status="screen")
    ns_id = seed_next_step(application_id=app_id, status="done",
                           resolved_at="2026-06-01T00:00:00+00:00")
    r = client.patch(f"/api/next-steps/{ns_id}", json={"status": "dismissed"})
    assert r.json()["status"] == "dismissed"
    assert _activities(db, app_id) == []  # only pending → resolved logs


def test_patch_edits_title_and_date(client, seed_next_step):
    ns_id = seed_next_step(title="Old", due_date="2026-06-20")
    r = client.patch(f"/api/next-steps/{ns_id}",
                     json={"title": "New", "due_date": "2026-07-01"})
    assert r.json()["title"] == "New"
    assert r.json()["due_date"] == "2026-07-01"
    assert r.json()["status"] == "pending"


def test_patch_explicit_null_date_clears_it(client, seed_next_step):
    """Undated steps are legitimate — an explicit null due_date takes the row
    off the calendar and the feed rather than 422ing."""
    ns_id = seed_next_step(due_date="2026-06-20")
    r = client.patch(f"/api/next-steps/{ns_id}", json={"due_date": None})
    assert r.status_code == 200
    assert r.json()["due_date"] is None


def test_patch_explicit_null_title_or_status_422(client, seed_next_step):
    ns_id = seed_next_step()
    assert client.patch(f"/api/next-steps/{ns_id}", json={"title": None}).status_code == 422
    assert client.patch(f"/api/next-steps/{ns_id}", json={"status": None}).status_code == 422


def test_patch_bumps_updated_at(client, seed_next_step):
    ns_id = seed_next_step()  # seeded at the fixed 2026-01-01 instant
    r = client.patch(f"/api/next-steps/{ns_id}", json={"title": "New"})
    assert r.json()["updated_at"] != "2026-01-01 00:00:00"


def test_patch_empty_body_422(client, seed_next_step):
    assert client.patch(f"/api/next-steps/{seed_next_step()}", json={}).status_code == 422


def test_patch_missing_404(client):
    assert client.patch("/api/next-steps/999", json={"status": "done"}).status_code == 404


def test_delete(client, db, seed_next_step):
    ns_id = seed_next_step()
    r = client.delete(f"/api/next-steps/{ns_id}")
    assert r.json() == {"deleted": ns_id}
    assert db.execute("SELECT 1 FROM next_steps WHERE id = ?", (ns_id,)).fetchone() is None


def test_delete_missing_404(client):
    assert client.delete("/api/next-steps/999").status_code == 404
