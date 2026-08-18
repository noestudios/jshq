"""Phase 7c: applications CRUD, the applied promote/create symmetry, and the
followup_application suggestion end-to-end."""

import json
from datetime import date


def _job_status(db, job_id):
    return db.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()["status"]


def _applied_activities(db, job_id):
    return db.execute(
        "SELECT * FROM activities WHERE entity_type = 'job' AND entity_id = ? AND type = 'applied'",
        (job_id,),
    ).fetchall()


def _status_activities(db, app_id):
    return db.execute(
        "SELECT * FROM activities"
        " WHERE entity_type = 'application' AND entity_id = ? AND type = 'status'",
        (app_id,),
    ).fetchall()


# --- CRUD ---


def test_create_drafting(client, db, seed_job):
    job_id = seed_job(title="Design Director")
    r = client.post("/api/applications", json={"job_id": job_id})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "drafting"
    assert body["applied_date"] is None
    assert body["job_title"] == "Design Director"
    assert body["company_name"] == "TestCo"
    assert body["created_at"] and body["updated_at"]
    assert _job_status(db, job_id) == "active"  # drafting never flips the job
    assert _applied_activities(db, job_id) == []


def test_payload_carries_job_fit_fields(client, db, seed_job):
    """The Applications view renders the shared fitChip (fit_score,
    manually_elevated) and the Salary sort (salary_min/max) off the payload
    (2026-08-10)."""
    job_id = seed_job(title="Design Director", fit_score=86,
                      salary_min=200000, salary_max=240000)
    db.execute("UPDATE jobs SET manually_elevated = 1 WHERE id = ?", (job_id,))
    db.commit()
    app_id = client.post("/api/applications", json={"job_id": job_id}).json()["id"]
    body = client.get(f"/api/applications/{app_id}").json()
    assert body["fit_score"] == 86
    assert body["manually_elevated"] == 1
    assert body["salary_min"] == 200000 and body["salary_max"] == 240000
    rows = client.get("/api/applications").json()
    assert rows[0]["fit_score"] == 86 and rows[0]["salary_max"] == 240000


def test_payload_carries_listing_state(client, db, seed_job):
    """The Applications view renders "no longer listed" off job_status +
    miss_count (2026-08-13). Listing state lives on the JOB, so both must ride
    the join in the list AND the detail payload or the band silently vanishes."""
    job_id = seed_job(title="Design Director")
    app_id = client.post("/api/applications", json={"job_id": job_id}).json()["id"]

    body = client.get(f"/api/applications/{app_id}").json()
    assert body["job_status"] == "active" and body["miss_count"] == 0

    # The req is pulled: decay counts misses on the applied row without ever
    # destroying its status (backend/app/ats/refresh.py).
    db.execute("UPDATE jobs SET status = 'applied', miss_count = 2 WHERE id = ?", (job_id,))
    db.commit()
    assert client.get(f"/api/applications/{app_id}").json()["miss_count"] == 2
    rows = client.get("/api/applications").json()
    assert rows[0]["job_status"] == "applied" and rows[0]["miss_count"] == 2


def test_create_missing_job(client):
    r = client.post("/api/applications", json={"job_id": 999})
    assert r.status_code == 400


def test_create_duplicate_409(client, seed_application):
    app_id = seed_application()
    job_id = client.get(f"/api/applications/{app_id}").json()["job_id"]
    r = client.post("/api/applications", json={"job_id": job_id})
    assert r.status_code == 409
    assert str(app_id) in r.json()["detail"]


def test_list_and_detail(client, seed_application):
    a1 = seed_application()
    a2 = seed_application(status="screen")
    rows = client.get("/api/applications").json()
    assert [a["id"] for a in rows] == [a1, a2]
    assert all("job_title" in a and "company_name" in a for a in rows)
    assert client.get(f"/api/applications/{a1}").json()["status"] == "drafting"
    assert client.get("/api/applications/999").status_code == 404


def test_update_bumps_updated_at(client, seed_application):
    app_id = seed_application()
    before = client.get(f"/api/applications/{app_id}").json()["updated_at"]
    r = client.put(
        f"/api/applications/{app_id}",
        json={"status": "drafting", "next_step": "tailor resume",
              "next_step_date": "2026-06-20"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["next_step"] == "tailor resume"
    assert body["next_step_date"] == "2026-06-20"
    assert body["updated_at"] > before


def test_update_to_applied_flips_job(client, db, seed_application):
    app_id = seed_application()
    job_id = client.get(f"/api/applications/{app_id}").json()["job_id"]
    r = client.put(f"/api/applications/{app_id}", json={"status": "applied"})
    body = r.json()
    assert body["status"] == "applied"
    assert body["applied_date"] == date.today().isoformat()
    assert _job_status(db, job_id) == "applied"
    assert len(_applied_activities(db, job_id)) == 1


def test_update_applied_idempotent_and_date_preserved(client, db, seed_application):
    app_id = seed_application()
    job_id = client.get(f"/api/applications/{app_id}").json()["job_id"]
    client.put(
        f"/api/applications/{app_id}",
        json={"status": "applied", "applied_date": "2026-06-01"},
    )
    r = client.put(
        f"/api/applications/{app_id}",
        json={"status": "applied", "applied_date": "2026-06-01"},
    )
    assert r.json()["applied_date"] == "2026-06-01"  # explicit date never overwritten
    assert len(_applied_activities(db, job_id)) == 1


# --- status-change timeline rows ---


def test_update_status_change_logs_activity(client, db, seed_application):
    app_id = seed_application(status="applied", applied_date="2026-06-01")
    r = client.put(f"/api/applications/{app_id}", json={"status": "rejected"})
    assert r.status_code == 200
    rows = _status_activities(db, app_id)
    assert len(rows) == 1
    assert rows[0]["date"] == date.today().isoformat()
    assert json.loads(rows[0]["content"]) == {"from": "applied", "to": "rejected"}


def test_update_unchanged_status_logs_nothing(client, db, seed_application):
    app_id = seed_application()
    r = client.put(
        f"/api/applications/{app_id}",
        json={"status": "drafting", "cover_note": "unrelated edit"},
    )
    assert r.status_code == 200
    assert _status_activities(db, app_id) == []


def test_update_first_apply_logs_no_status_row(client, db, seed_application):
    # drafting -> applied is represented by _ensure_applied's job-level row.
    app_id = seed_application()
    job_id = client.get(f"/api/applications/{app_id}").json()["job_id"]
    client.put(f"/api/applications/{app_id}", json={"status": "applied"})
    assert len(_applied_activities(db, job_id)) == 1
    assert _status_activities(db, app_id) == []


def test_update_demotion_to_applied_logs_status_row(client, db, seed_application):
    # screen -> applied is a real transition _ensure_applied won't record.
    app_id = seed_application(status="screen", applied_date="2026-06-01")
    client.put(f"/api/applications/{app_id}", json={"status": "applied"})
    rows = _status_activities(db, app_id)
    assert len(rows) == 1
    assert json.loads(rows[0]["content"]) == {"from": "screen", "to": "applied"}


def test_update_each_transition_logs_a_row(client, db, seed_application):
    app_id = seed_application(status="applied", applied_date="2026-06-01")
    client.put(f"/api/applications/{app_id}", json={"status": "rejected"})
    client.put(f"/api/applications/{app_id}", json={"status": "screen"})
    assert len(_status_activities(db, app_id)) == 2


# --- jobs-PATCH symmetry ---


def test_jobs_patch_promotes_drafting(client, db, seed_job):
    job_id = seed_job()
    app_id = client.post("/api/applications", json={"job_id": job_id}).json()["id"]
    r = client.patch(f"/api/jobs/{job_id}", json={"status": "applied"})
    assert r.status_code == 200
    apps = db.execute("SELECT * FROM applications WHERE job_id = ?", (job_id,)).fetchall()
    assert len(apps) == 1  # promoted in place, no second row
    assert apps[0]["id"] == app_id
    assert apps[0]["status"] == "applied"
    assert apps[0]["applied_date"] == date.today().isoformat()
    assert len(_applied_activities(db, job_id)) == 1


def test_jobs_patch_never_demotes(client, db, seed_application):
    app_id = seed_application(status="screen", applied_date="2026-06-01")
    job_id = client.get(f"/api/applications/{app_id}").json()["job_id"]
    client.patch(f"/api/jobs/{job_id}", json={"status": "applied"})
    client.patch(f"/api/jobs/{job_id}", json={"status": "applied"})
    body = client.get(f"/api/applications/{app_id}").json()
    assert body["status"] == "screen"
    assert body["applied_date"] == "2026-06-01"
    assert len(_applied_activities(db, job_id)) == 1


def test_jobs_patch_creates_with_timestamps(client, db, seed_job):
    job_id = seed_job()
    client.patch(f"/api/jobs/{job_id}", json={"status": "applied"})
    row = db.execute("SELECT * FROM applications WHERE job_id = ?", (job_id,)).fetchone()
    assert row["created_at"] and row["updated_at"]


# --- delete ---


def test_delete_cascades_and_reverts_job(client, db, seed_job):
    job_id = seed_job()
    app_id = client.post("/api/applications", json={"job_id": job_id}).json()["id"]
    client.put(f"/api/applications/{app_id}", json={"status": "applied"})
    client.post(
        "/api/activities",
        json={"entity_type": "application", "entity_id": app_id, "type": "note",
              "content": "sent via portal"},
    )
    client.post(
        "/api/reminders",
        json={"title": "Follow up", "type": "followup_application",
              "entity_type": "application", "entity_id": app_id, "due_date": "2026-06-20"},
    )
    r = client.delete(f"/api/applications/{app_id}")
    assert r.json() == {"deleted": app_id}
    assert client.get(f"/api/applications/{app_id}").status_code == 404
    assert db.execute(
        "SELECT COUNT(*) AS n FROM activities WHERE entity_type = 'application'"
    ).fetchone()["n"] == 0
    assert db.execute(
        "SELECT COUNT(*) AS n FROM reminders WHERE entity_type = 'application'"
    ).fetchone()["n"] == 0
    assert _job_status(db, job_id) == "active"  # applied job reverts
    assert len(_applied_activities(db, job_id)) == 1  # history stays


def test_delete_leaves_dismissed_job(client, db, seed_application, seed_job):
    job_id = seed_job(status="dismissed")
    app_id = seed_application(job_id=job_id)
    client.delete(f"/api/applications/{app_id}")
    assert _job_status(db, job_id) == "dismissed"


def test_delete_missing_404(client):
    assert client.delete("/api/applications/999").status_code == 404


# --- jobs expose the link ---


def test_jobs_expose_application_link(client, seed_job, seed_application):
    bare_id = seed_job()
    app_id = seed_application(status="screen")
    jobs = {j["id"]: j for j in client.get("/api/jobs").json()}
    bare = jobs[bare_id]
    assert bare["application_id"] is None and bare["application_status"] is None
    linked = next(j for j in jobs.values() if j["application_id"] == app_id)
    assert linked["application_status"] == "screen"
    detail = client.get(f"/api/jobs/{linked['id']}").json()
    assert detail["application_id"] == app_id
    assert detail["application_status"] == "screen"


# --- followup_application suggestion end-to-end ---


def test_suggestion_lifecycle(client, seed_job):
    job_id = seed_job(title="Head of Design")
    app_id = client.post("/api/applications", json={"job_id": job_id}).json()["id"]
    key = f"followup_application:application:{app_id}"

    # drafting (no applied_date) → nothing suggested
    keys = [s["key"] for s in client.get("/api/suggestions").json()["reminders"]]
    assert key not in keys

    client.put(f"/api/applications/{app_id}", json={"status": "applied"})
    match = next(
        s for s in client.get("/api/suggestions").json()["reminders"] if s["key"] == key
    )
    assert match["type"] == "followup_application"

    r = client.post("/api/suggestions/reminder", json={"key": key, "action": "accept"})
    reminder = r.json()["reminder"]
    assert reminder["type"] == "followup_application"
    assert reminder["entity_type"] == "job"  # suggester links the job, by design
    assert reminder["entity_id"] == job_id


def test_withdrawn_suppresses_suggestion(client, seed_application):
    today = date.today().isoformat()
    app_id = seed_application(status="applied", applied_date=today)
    key = f"followup_application:application:{app_id}"
    keys = [s["key"] for s in client.get("/api/suggestions").json()["reminders"]]
    assert key in keys

    client.put(
        f"/api/applications/{app_id}",
        json={"status": "withdrawn", "applied_date": today},
    )
    keys = [s["key"] for s in client.get("/api/suggestions").json()["reminders"]]
    assert key not in keys
