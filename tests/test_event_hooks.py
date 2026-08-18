"""Phase 5 event plumbing: applied hook + activities endpoints."""

import json
from datetime import date


def test_applied_creates_application_and_activity(client, db, seed_job):
    job_id = seed_job(title="Design Director")
    r = client.patch(f"/api/jobs/{job_id}", json={"status": "applied"})
    assert r.status_code == 200

    apps = db.execute("SELECT * FROM applications WHERE job_id = ?", (job_id,)).fetchall()
    assert len(apps) == 1
    assert apps[0]["applied_date"] == date.today().isoformat()
    assert apps[0]["status"] == "applied"

    acts = db.execute(
        "SELECT * FROM activities WHERE entity_type = 'job' AND entity_id = ?", (job_id,)
    ).fetchall()
    assert len(acts) == 1
    assert acts[0]["type"] == "applied"
    # Scores are snapshotted at decision time; an unscored job records nulls.
    assert json.loads(acts[0]["content"]) == {
        "title": "Design Director", "fit_score": None, "model_score": None
    }


def test_apply_and_dismiss_snapshot_decision_time_scores(client, db, seed_job):
    """Rescores rewrite jobs.fit_score, so the activity row is the only durable
    record of what the board said when the decision was made — the future
    applied-vs-dismissed threshold validation depends on it."""
    job_id = seed_job(
        title="Design Director", fit_score=75, score_detail='{"model_score": 81}'
    )
    client.patch(f"/api/jobs/{job_id}", json={"status": "applied"})
    applied = db.execute(
        "SELECT content FROM activities WHERE type = 'applied' AND entity_id = ?",
        (job_id,),
    ).fetchone()
    body = json.loads(applied["content"])
    assert body["fit_score"] == 75 and body["model_score"] == 81

    # Revert, then a rescore moves the job, then a dismissal: the dismissal
    # must carry the score the user was looking at, not the apply-time one.
    client.patch(f"/api/jobs/{job_id}", json={"status": "active"})
    db.execute(
        """UPDATE jobs SET fit_score = 20, score_detail = '{"model_score": 24}'
           WHERE id = ?""",
        (job_id,),
    )
    db.commit()
    client.patch(
        f"/api/jobs/{job_id}", json={"status": "dismissed", "reason": "comp too low"}
    )
    dismissal = db.execute(
        "SELECT content FROM activities WHERE type = 'dismissal' AND entity_id = ?",
        (job_id,),
    ).fetchone()
    body = json.loads(dismissal["content"])
    assert body["fit_score"] == 20 and body["model_score"] == 24
    assert body["reason"] == "comp too low"


def _job_status(db, job_id):
    return db.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()["status"]


def _job_activity_types(db, job_id):
    return [
        r["type"]
        for r in db.execute(
            "SELECT type FROM activities WHERE entity_type = 'job' AND entity_id = ?"
            " ORDER BY id",
            (job_id,),
        ).fetchall()
    ]


def test_apply_revert_reapply_logs_each_step(client, db, seed_job):
    """Reverting via the jobs PATCH. This replaces test_reapply_is_noop, which
    asserted a single 'applied' row across this exact sequence — that was the
    bug, not the contract: the old guard skipped the insert whenever the job
    already had any 'applied' activity, and a revert deliberately keeps the old
    one as history, so no re-apply could ever log again."""
    job_id = seed_job()
    client.patch(f"/api/jobs/{job_id}", json={"status": "applied"})
    client.patch(f"/api/jobs/{job_id}", json={"status": "active"})
    client.patch(f"/api/jobs/{job_id}", json={"status": "applied"})

    assert _job_activity_types(db, job_id) == ["applied", "unapplied", "applied"]
    assert _job_status(db, job_id) == "applied"
    # The PATCH revert leaves the application row alone; re-applying promotes it
    # rather than creating a second.
    assert db.execute("SELECT COUNT(*) AS n FROM applications").fetchone()["n"] == 1


def test_apply_revert_reapply_via_application_delete(client, db, seed_job):
    """The path one real revert actually took: the revert DELETED the application
    (row id is reused, which is how we know), so the re-apply created a fresh
    one. The reversal row is job-scoped on purpose — delete_application wipes
    every application-scoped activity, so an application-scoped record of the
    delete would remove itself."""
    job_id = seed_job()
    client.patch(f"/api/jobs/{job_id}", json={"status": "applied"})
    app_id = db.execute(
        "SELECT id FROM applications WHERE job_id = ?", (job_id,)
    ).fetchone()["id"]

    assert client.delete(f"/api/applications/{app_id}").status_code == 200
    assert _job_status(db, job_id) == "active"

    client.patch(f"/api/jobs/{job_id}", json={"status": "applied"})

    assert _job_activity_types(db, job_id) == ["applied", "unapplied", "applied"]
    assert _job_status(db, job_id) == "applied"


def test_reapply_without_revert_logs_once(client, db, seed_job):
    """The guard the old one-per-job check was really there for: marking an
    already-applied job applied again is a no-op and must stay silent."""
    job_id = seed_job()
    client.patch(f"/api/jobs/{job_id}", json={"status": "applied"})
    client.patch(f"/api/jobs/{job_id}", json={"status": "applied"})
    assert _job_activity_types(db, job_id) == ["applied"]


def test_dismiss_from_applied_logs_both(client, db, seed_job):
    """Leaving applied and being dismissed are two facts, and the timeline
    carries both — the dismissal row is the only one holding the reason."""
    job_id = seed_job()
    client.patch(f"/api/jobs/{job_id}", json={"status": "applied"})
    client.patch(
        f"/api/jobs/{job_id}", json={"status": "dismissed", "reason": "comp too low"}
    )
    assert _job_activity_types(db, job_id) == ["applied", "unapplied", "dismissal"]


def test_create_activity(client, db, seed_company):
    cid = seed_company()
    db.execute("INSERT INTO contacts (name, company_id) VALUES ('Dana', ?)", (cid,))
    db.commit()
    contact_id = db.execute("SELECT id FROM contacts").fetchone()["id"]

    r = client.post(
        "/api/activities",
        json={"entity_type": "contact", "entity_id": contact_id, "type": "meeting",
              "content": "coffee chat"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["date"] == date.today().isoformat()  # server default
    assert body["type"] == "meeting"

    # explicit date respected
    r = client.post(
        "/api/activities",
        json={"entity_type": "contact", "entity_id": contact_id, "type": "call",
              "date": "2026-06-01"},
    )
    assert r.json()["date"] == "2026-06-01"


def test_create_activity_validation(client):
    # entity_id required for non-general
    r = client.post("/api/activities", json={"entity_type": "job", "type": "note"})
    assert r.status_code == 422
    # general must not carry an entity_id
    r = client.post(
        "/api/activities", json={"entity_type": "general", "entity_id": 1, "type": "note"}
    )
    assert r.status_code == 422
    # nonexistent entity
    r = client.post(
        "/api/activities", json={"entity_type": "contact", "entity_id": 999, "type": "note"}
    )
    assert r.status_code == 400
    # general is fine without entity
    r = client.post("/api/activities", json={"entity_type": "general", "type": "note",
                                             "content": "posted on linkedin"})
    assert r.status_code == 201


def test_list_activities_filters(client, seed_activity, seed_job):
    job_id = seed_job()
    seed_activity(entity_type="job", entity_id=job_id, type="interview", date="2026-06-10")
    seed_activity(entity_type="job", entity_id=job_id, type="note", date="2026-06-09")
    seed_activity(entity_type="general", type="meeting", date="2026-06-08")

    assert len(client.get("/api/activities").json()) == 3
    rows = client.get(f"/api/activities?entity_type=job&entity_id={job_id}").json()
    assert [a["type"] for a in rows] == ["interview", "note"]  # date DESC
    rows = client.get("/api/activities?types=meeting,interview").json()
    assert {a["type"] for a in rows} == {"meeting", "interview"}
