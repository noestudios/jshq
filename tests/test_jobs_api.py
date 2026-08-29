"""Jobs + refresh endpoint tests."""

from jshq.ats import refresh as ats_refresh


def seed_job(db, company_id, **overrides):
    fields = {
        "company_id": company_id,
        "title": "Product Designer",
        "url": "https://example.com/j1",
        "location": "Remote - US",
        "remote_type": "remote",
        "level_band": "ic",
        "salary_min": None,
        "salary_max": None,
        "salary_stated": 0,
        "description_text": "A long JD body.",
        "first_seen": "2026-06-01T08:00:00+00:00",
        "last_seen": "2026-06-10T08:00:00+00:00",
        "status": "active",
        "miss_count": 0,
        "dedupe_key": f"{company_id}:J1",
    }
    fields.update(overrides)
    cols = ", ".join(fields)
    marks = ", ".join("?" * len(fields))
    cur = db.execute(f"INSERT INTO jobs ({cols}) VALUES ({marks})", tuple(fields.values()))
    db.commit()
    return cur.lastrowid


def test_list_jobs_omits_description_includes_company(client, db, seed_company):
    cid = seed_company(name="AlphaCo")
    seed_job(db, cid)

    jobs = client.get("/api/jobs").json()
    assert len(jobs) == 1
    j = jobs[0]
    assert "description_text" not in j
    assert j["company_name"] == "AlphaCo"
    assert j["title"] == "Product Designer"
    assert j["miss_count"] == 0


def test_list_jobs_ordered_by_last_seen_desc(client, db, seed_company):
    cid = seed_company()
    seed_job(db, cid, dedupe_key=f"{cid}:old", last_seen="2026-06-01T08:00:00+00:00", title="Old")
    seed_job(db, cid, dedupe_key=f"{cid}:new", last_seen="2026-06-10T08:00:00+00:00", title="New")
    titles = [j["title"] for j in client.get("/api/jobs").json()]
    assert titles == ["New", "Old"]


def test_list_jobs_filters_by_company_id(client, db, seed_company):
    alpha = seed_company(name="AlphaCo")
    beta = seed_company(name="BetaCo")
    alpha_job = seed_job(db, alpha, dedupe_key=f"{alpha}:J1", title="Design Lead")
    seed_job(db, beta, dedupe_key=f"{beta}:J1", title="PM")

    both = client.get("/api/jobs").json()
    assert len(both) == 2

    scoped = client.get(f"/api/jobs?company_id={alpha}").json()
    assert [j["id"] for j in scoped] == [alpha_job]
    assert scoped[0]["company_name"] == "AlphaCo"

    # Unknown company → empty list, not an error.
    assert client.get("/api/jobs?company_id=999999").json() == []


def test_create_manual_job(client, seed_company):
    cid = seed_company(name="Exampleco")
    r = client.post("/api/jobs", json={
        "company_id": cid,
        "title": "Senior Product Designer",
        "url": "https://careers.example.com/job/123",
        "location": "Evanston, IL",
        "remote_type": "hybrid",
        "salary_min": 150000,
        "salary_max": 190000,
        "description_text": "Design things at Exampleco.",
    })
    assert r.status_code == 201
    job = r.json()
    assert job["source"] == "manual"
    assert job["status"] == "active"
    assert job["company_name"] == "Exampleco"
    assert job["level_band"]  # derived from the title, like the ingestion path
    assert job["salary_stated"] == 1 and job["salary_max"] == 190000
    # Surfaces in the (company-scoped) list with its source.
    listed = client.get(f"/api/jobs?company_id={cid}").json()
    assert [j["id"] for j in listed] == [job["id"]]
    assert listed[0]["source"] == "manual"


def test_create_manual_job_dedupes_and_404s(client, seed_company):
    cid = seed_company(name="Exampleco")
    body = {"company_id": cid, "title": "PM", "url": "https://x.example/job/1"}
    created = client.post("/api/jobs", json=body)
    assert created.status_code == 201
    job_id = created.json()["id"]
    # Same url at the same company is a 409, not a duplicate row — and it carries
    # the existing job (id + status) so the Add-job UI can offer to reactivate it.
    dup = client.post("/api/jobs", json=body)
    assert dup.status_code == 409
    detail = dup.json()["detail"]
    assert detail["message"] == "this job is already tracked"
    assert detail["job_id"] == job_id
    assert detail["status"] == "active"
    # Unknown company → 404.
    assert client.post("/api/jobs", json={"company_id": 999999, "title": "PM"}).status_code == 404


def test_create_dup_reports_dismissed_status(client, seed_company):
    """The already-tracked 409 reports a dismissed duplicate's status, so the UI
    can offer reactivation (the dismissed job is hidden from the default list)."""
    cid = seed_company(name="Atlassian")
    body = {"company_id": cid, "title": "Senior Design Manager, AI", "url": "https://x.example/24468"}
    job_id = client.post("/api/jobs", json=body).json()["id"]
    assert client.patch(f"/api/jobs/{job_id}", json={"status": "dismissed"}).status_code == 200
    dup = client.post("/api/jobs", json=body)
    assert dup.status_code == 409
    detail = dup.json()["detail"]
    assert detail["job_id"] == job_id
    assert detail["status"] == "dismissed"


def test_get_job_detail_and_404(client, db, seed_company):
    cid = seed_company(name="AlphaCo")
    job_id = seed_job(db, cid)

    j = client.get(f"/api/jobs/{job_id}").json()
    assert j["description_text"] == "A long JD body."
    assert j["company_name"] == "AlphaCo"

    assert client.get("/api/jobs/9999").status_code == 404


def test_patch_status_validates_and_updates(client, db, seed_company):
    cid = seed_company()
    job_id = seed_job(db, cid)

    r = client.patch(f"/api/jobs/{job_id}", json={"status": "dismissed"})
    assert r.status_code == 200
    assert r.json()["status"] == "dismissed"
    assert db.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()[0] == "dismissed"

    assert client.patch(f"/api/jobs/{job_id}", json={"status": "nonsense"}).status_code == 422
    assert client.patch("/api/jobs/9999", json={"status": "active"}).status_code == 404


def test_edit_job_details_updates_and_marks_pending(client, db, seed_company):
    cid = seed_company()
    # A scored job, so we can prove the edit nulls the fit columns back to pending.
    jid = seed_job(db, cid, fit_score=42, fit_quadrant="balanced",
                   tier1_results='{"hard_fail": false}', near_miss_flags="[]",
                   scoring_notes="prior")

    r = client.patch(f"/api/jobs/{jid}/details", json={
        "location": "Evanston, IL", "remote_type": "hybrid",
        "salary_min": 150000, "salary_max": 190000,
    })
    assert r.status_code == 200
    job = r.json()
    assert job["location"] == "Evanston, IL" and job["remote_type"] == "hybrid"
    assert job["salary_min"] == 150000 and job["salary_max"] == 190000
    assert job["salary_stated"] == 1          # derived server-side
    assert job["manually_edited"] == 1         # protects these fields from the next refresh

    # No ANTHROPIC_API_KEY in tests -> run_scoring skips, leaving the job pending.
    row = db.execute(
        "SELECT tier1_results, fit_score, fit_quadrant, near_miss_flags, scoring_notes "
        "FROM jobs WHERE id = ?", (jid,)
    ).fetchone()
    assert all(row[c] is None for c in
               ("tier1_results", "fit_score", "fit_quadrant", "near_miss_flags", "scoring_notes"))


def test_edit_job_details_rescores_with_the_jobs_own_status(client, db, seed_company, monkeypatch):
    """The edit-rescore bug (2026-08-10): the edit NULLs the fit columns of a job in ANY
    status, but run_scoring's default population is active-only — so editing an
    applied job destroyed its score and the 'rescore' did nothing, silently and
    permanently. The endpoint must scope the rescore to this job with its own
    status."""
    from jshq import main as app_main

    calls = []

    async def spy(conn, **kwargs):
        calls.append(kwargs)
        return {"scored": 0}

    monkeypatch.setattr(app_main, "run_scoring", spy)
    cid = seed_company()
    jid = seed_job(db, cid, status="applied", fit_score=78,
                   tier1_results='{"hard_fail": false}')

    r = client.patch(f"/api/jobs/{jid}/details", json={
        "location": "Remote - US", "remote_type": "remote",
        "salary_min": 285000, "salary_max": 361000,
    })
    assert r.status_code == 200
    assert calls, "the edit must trigger a rescore"
    assert calls[0]["job_ids"] == (jid,)
    assert calls[0]["statuses"] == ("applied",)


def test_edit_job_details_clears_salary_and_validates(client, db, seed_company):
    cid = seed_company()
    jid = seed_job(db, cid, salary_min=100000, salary_max=120000, salary_stated=1)
    # Clearing both figures -> salary_stated derives back to 0.
    r = client.patch(f"/api/jobs/{jid}/details", json={"location": "Remote - US", "remote_type": "remote"})
    assert r.status_code == 200
    assert r.json()["salary_stated"] == 0
    assert r.json()["salary_min"] is None and r.json()["salary_max"] is None

    assert client.patch("/api/jobs/9999/details", json={"remote_type": "remote"}).status_code == 404
    assert client.patch(f"/api/jobs/{jid}/details", json={"remote_type": "spaceship"}).status_code == 422


def test_dismiss_with_reason_writes_activity(client, db, seed_company):
    import json

    cid = seed_company()
    job_id = seed_job(db, cid, title="ML Engineer")

    r = client.patch(
        f"/api/jobs/{job_id}",
        json={"status": "dismissed", "reason": "not my focus area", "note": "too ML-y"},
    )
    assert r.status_code == 200
    row = db.execute(
        "SELECT * FROM activities WHERE entity_type = 'job' AND entity_id = ?", (job_id,)
    ).fetchone()
    assert row["type"] == "dismissal"
    content = json.loads(row["content"])
    assert content == {
        "reason": "not my focus area", "note": "too ML-y", "title": "ML Engineer",
        "fit_score": None, "model_score": None,  # decision-time snapshot; unscored job
    }


def test_dismiss_without_reason_writes_no_activity(client, db, seed_company):
    job_id = seed_job(db, seed_company())
    client.patch(f"/api/jobs/{job_id}", json={"status": "dismissed"})
    assert db.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 0


def test_reason_without_dismissed_status_rejected(client, db, seed_company):
    job_id = seed_job(db, seed_company())
    r = client.patch(f"/api/jobs/{job_id}", json={"status": "active", "reason": "nope"})
    assert r.status_code == 422


def test_refresh_status_empty_then_set(client, db):
    s = client.get("/api/refresh/status").json()
    assert s == {
        "last_refresh": None,
        "connectable": 0,
        "last_rescore": None,
        "running": False,
        "refresh_error": None,
        "adapter_errors": [],
        "no_ats": [],
        "stale": [],
        "checking": [],
        "scoring_progress": None,
        "scoring_report": None,
        "refresh_progress": None,
        "refresh_report": None,
        "usage": None,
    }

    db.execute("INSERT INTO settings (key, value) VALUES ('last_refresh', '2026-06-10T08:00:00+00:00')")
    db.commit()
    s = client.get("/api/refresh/status").json()
    assert s["last_refresh"] == "2026-06-10T08:00:00+00:00"


def test_refresh_status_counts_connectable_companies(client, seed_company):
    # #34: `connectable` counts companies with a real adapter + slug — the client
    # skips the day-one auto-refresh when it's 0 so a manual-only board stays calm.
    assert client.get("/api/refresh/status").json()["connectable"] == 0
    seed_company(name="Manual Co", ats_type="manual", ats_slug=None)
    assert client.get("/api/refresh/status").json()["connectable"] == 0
    seed_company(name="Greenhouse Co", ats_type="greenhouse", ats_slug="ghco")
    assert client.get("/api/refresh/status").json()["connectable"] == 1


def test_refresh_status_surfaces_outage_marker(client, db):
    db.execute(
        "INSERT INTO settings (key, value) VALUES ('last_refresh_error', ?)",
        ('{"at": "2026-06-14T20:06:02+00:00", "reason": "offline", "attempted": 18}',),
    )
    db.commit()
    s = client.get("/api/refresh/status").json()
    assert s["refresh_error"] == {
        "at": "2026-06-14T20:06:02+00:00",
        "reason": "offline",
        "attempted": 18,
    }


def test_refresh_status_lists_adapter_errors(client, seed_company):
    seed_company(
        name="Broken Co",
        ats_type="workday",
        ats_slug="brokenco",
        ats_last_status="error: GET https://x.example: HTTP 403",
        ats_last_checked="2026-06-11T08:00:00+00:00",
    )
    seed_company(name="Fine Co", ats_type="greenhouse", ats_slug="fineco", ats_last_status="ok: 12 matched")
    seed_company(name="Manual Co", ats_type="manual", ats_slug=None)

    status = client.get("/api/refresh/status").json()
    errors = status["adapter_errors"]
    assert [e["name"] for e in errors] == ["Broken Co"]
    assert errors[0]["ats_last_status"].startswith("error:")
    # Manual / undetected companies surface in no_ats (Broken/Fine have real adapters).
    assert [c["name"] for c in status["no_ats"]] == ["Manual Co"]
    assert errors[0]["ats_last_checked"] == "2026-06-11T08:00:00+00:00"
    assert "company_id" in errors[0]


def test_refresh_status_lists_stale(client, seed_company):
    from datetime import datetime, timezone

    recent = datetime.now(timezone.utc).isoformat(timespec="seconds")
    old = "2020-01-01T00:00:00+00:00"
    # Connected but pulling 0 jobs -> "empty" (silent breakage signature).
    seed_company(name="Empty Co", ats_type="greenhouse", ats_slug="emptyco",
                 ats_last_status="ok: 0 matched", ats_last_checked=recent)
    # Connected, not re-checked within the window -> "not_refreshed".
    seed_company(name="Stale Co", ats_type="greenhouse", ats_slug="staleco",
                 ats_last_status="ok: 5 matched", ats_last_checked=old)
    # Healthy, recently checked -> excluded.
    seed_company(name="Fresh Co", ats_type="greenhouse", ats_slug="freshco",
                 ats_last_status="ok: 5 matched", ats_last_checked=recent)
    # Failing -> excluded (lives in adapter_errors instead).
    seed_company(name="Broken Co", ats_type="workday", ats_slug="brokenco",
                 ats_last_status="error: boom", ats_last_checked=old)
    # Never checked -> pending, not stale.
    seed_company(name="Pending Co", ats_type="greenhouse", ats_slug="pendingco")
    # Manual -> excluded (no connectable adapter).
    seed_company(name="Manual Co", ats_type="manual", ats_slug=None)

    stale = client.get("/api/refresh/status").json()["stale"]
    assert {s["name"]: s["reason"] for s in stale} == {
        "Empty Co": "empty",
        "Stale Co": "not_refreshed",
    }
    assert all("company_id" in s for s in stale)


def test_refresh_status_lists_checking_boards(client, seed_company):
    """The per-board ↻ / onboarding stamp 'checking'; the status endpoint names
    those in-flight boards so Today's progress bar can say which is updating."""
    cid = seed_company(name="Exampleco", ats_type="oracle", ats_slug="exampleco",
                       ats_last_status="checking")
    seed_company(name="Fine Co", ats_type="greenhouse", ats_slug="fineco",
                 ats_last_status="ok: 12 matched")

    checking = client.get("/api/refresh/status").json()["checking"]
    assert checking == [{"company_id": cid, "name": "Exampleco"}]


def test_elevate_round_trips_and_keeps_score(client, db, seed_company):
    cid = seed_company()
    jid = seed_job(db, cid, fit_score=0)  # Tier-1 hard-fail sentinel
    r = client.post(f"/api/jobs/{jid}/elevate", json={"elevated": True})
    assert r.status_code == 200
    assert r.json()["manually_elevated"] == 1
    assert r.json()["fit_score"] == 0  # score stays model-judged
    assert client.get(f"/api/jobs/{jid}").json()["manually_elevated"] == 1
    # the list payload carries the flag too (drives the chip / filter / sort)
    assert any(j["manually_elevated"] == 1 for j in client.get("/api/jobs").json())
    r = client.post(f"/api/jobs/{jid}/elevate", json={"elevated": False})
    assert r.json()["manually_elevated"] == 0


def test_elevate_unknown_job_404(client):
    assert client.post("/api/jobs/99999/elevate", json={"elevated": True}).status_code == 404


def test_trigger_refresh_starts_and_reports_running(client, monkeypatch):
    started = []

    async def fake_run_refresh(conn=None):
        started.append(True)
        return {}

    monkeypatch.setattr(ats_refresh, "run_refresh", fake_run_refresh)
    r = client.post("/api/refresh")
    assert r.status_code == 202
    assert r.json() == {"started": True}
    assert started == [True]  # TestClient drains the task on request completion

    monkeypatch.setattr(ats_refresh, "is_running", lambda: True)
    r = client.post("/api/refresh")
    assert r.status_code == 200
    assert r.json() == {"running": True}


def test_trigger_refresh_scope_failed_stamps_and_scopes(client, db, seed_company, monkeypatch):
    failing = seed_company(name="FailCo", ats_slug="failco", ats_last_status="error: boom")
    manual = seed_company(name="ManualCo", ats_type="manual", ats_slug=None,
                          ats_last_status="error: boom")
    healthy = seed_company(name="OkCo", ats_slug="okco", ats_last_status="ok: 1 matched")
    calls = []

    async def fake_run_refresh(conn=None, company_ids=None):
        calls.append(company_ids)
        return {}

    monkeypatch.setattr(ats_refresh, "run_refresh", fake_run_refresh)
    r = client.post("/api/refresh", json={"scope": "failed"})
    assert r.status_code == 202
    assert r.json() == {"started": True, "ids": [failing]}
    assert calls == [[failing]]
    status = {
        row["id"]: row["ats_last_status"]
        for row in db.execute("SELECT id, ats_last_status FROM companies")
    }
    # Only the CONNECTABLE failing company is pre-stamped; a manual company's
    # error status has nothing that could re-fetch it.
    assert status[failing] == "checking"
    assert status[manual] == "error: boom"
    assert status[healthy] == "ok: 1 matched"


def test_trigger_refresh_scope_failed_none(client, seed_company, monkeypatch):
    seed_company(name="OkCo", ats_slug="okco", ats_last_status="ok: 1 matched")
    calls = []

    async def fake_run_refresh(conn=None, company_ids=None):
        calls.append(company_ids)
        return {}

    monkeypatch.setattr(ats_refresh, "run_refresh", fake_run_refresh)
    r = client.post("/api/refresh", json={"scope": "failed"})
    assert r.status_code == 200
    assert r.json() == {"none": True}
    assert calls == []


def test_trigger_refresh_scope_all_matches_bare_post(client, monkeypatch):
    async def fake_run_refresh(conn=None, company_ids=None):
        return {}

    monkeypatch.setattr(ats_refresh, "run_refresh", fake_run_refresh)
    r = client.post("/api/refresh", json={"scope": "all"})
    assert r.status_code == 202
    assert r.json() == {"started": True}


def test_trigger_refresh_bogus_scope_rejected(client):
    assert client.post("/api/refresh", json={"scope": "sideways"}).status_code == 422


def test_create_manual_collides_with_ats_row_by_url(client, db, seed_company):
    """A pasted URL the ATS already ingested must 409 against that row, not twin
    it (pasting the LinkedIn-tracked link of an already-pulled posting). Matching
    is scheme/www./tracking-param/trailing-slash insensitive."""
    cid = seed_company(name="Acmeco")
    ats_id = seed_job(db, cid, title="Staff Product Designer, Mobile",
                      url="https://boards.example/acmeco/jobs/4100200300")
    dup = client.post("/api/jobs", json={
        "company_id": cid,
        "title": "Staff Product Designer, Mobile",
        "url": "http://www.boards.example/acmeco/jobs/4100200300/?utm_source=linkedin",
    })
    assert dup.status_code == 409
    detail = dup.json()["detail"]
    assert detail["job_id"] == ats_id
    assert detail["status"] == "active"
    assert detail["title"] == "Staff Product Designer, Mobile"
    assert len(client.get(f"/api/jobs?company_id={cid}").json()) == 1


def test_create_manual_identity_query_params_do_not_collide(client, db, seed_company):
    """Some boards carry job identity in the query (embedded greenhouse tenants
    are all /?gh_jid=<id>) — a different query must NOT read as the same posting."""
    cid = seed_company(name="Exampleco")
    seed_job(db, cid, url="https://careers.example.com/?gh_jid=100")
    r = client.post("/api/jobs", json={
        "company_id": cid,
        "title": "UX Designer",
        "url": "https://careers.example.com/?gh_jid=200",
    })
    assert r.status_code == 201


def test_create_manual_same_url_other_company_creates(client, db, seed_company):
    a = seed_company(name="CoA")
    b = seed_company(name="CoB")
    seed_job(db, a, url="https://boards.example/shared/jobs/1")
    r = client.post("/api/jobs", json={
        "company_id": b,
        "title": "Product Designer",
        "url": "https://boards.example/shared/jobs/1",
    })
    assert r.status_code == 201
