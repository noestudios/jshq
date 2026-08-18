COMPANY = {
    "name": "Acme Studio",
    "location": "Evanston, IL",
    "priority": 2,
    "status": "prospect",
    "values_fit": "high",
    "website": "https://acme.example",
    "notes": "test company",
    "linkedin_company_ids": ["12345"],
    "linkedin_title_searches": ["Director of Design", "Head of Design"],
}


def create(client, **overrides):
    payload = {**COMPANY, **overrides}
    if "name" in overrides and "website" not in overrides:
        # POST rejects duplicates by name / website host / careers URL (Phase 4),
        # so a renamed company must not inherit the template's website.
        slug = "".join(ch for ch in overrides["name"].lower() if ch.isalnum())
        payload["website"] = f"https://{slug or 'x'}.example"
    response = client.post("/api/companies", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_create_and_list_roundtrip(client):
    created = create(client)
    assert created["name"] == "Acme Studio"
    assert created["linkedin_company_ids"] == ["12345"]
    assert created["linkedin_title_searches"] == ["Director of Design", "Head of Design"]

    companies = client.get("/api/companies").json()
    assert [c["id"] for c in companies] == [created["id"]]
    assert companies[0]["linkedin_title_searches"] == created["linkedin_title_searches"]


def test_list_sort_closed_last_priority_nulls_last(client):
    create(client, name="Aardvark Closed", priority=1, status="closed")
    create(client, name="Zeta", priority=1, status="targeting")
    create(client, name="NoPriority", priority=None, status="prospect")
    create(client, name="Mid", priority=3, status="prospect")
    create(client, name="NullStatus", priority=None, status=None)

    companies = client.get("/api/companies").json()
    assert [c["name"] for c in companies] == [
        "Zeta",
        "Mid",
        "NoPriority",
        "NullStatus",
        "Aardvark Closed",
    ]


def test_create_defaults(client):
    response = client.post("/api/companies", json={"name": "Bare Minimum"})
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["linkedin_company_ids"] == []


def test_get_company_by_id(client):
    created = create(client)
    fetched = client.get(f"/api/companies/{created['id']}")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["id"] == created["id"]
    assert fetched.json()["name"] == "Acme Studio"


def test_get_company_404(client):
    assert client.get("/api/companies/99999").status_code == 404


# --- LinkedIn roles seeded on create from the linkedin_title_defaults setting.
# The setting ships EMPTY (Phase 5b): role titles are field-specific, so a
# fresh install must not stamp another field's titles on every company. ---


def test_create_seeds_no_titles_on_a_fresh_install(client):
    created = client.post("/api/companies", json={"name": "Seedling"}).json()
    assert created["linkedin_title_searches"] == []


def test_create_seeds_titles_from_the_setting_when_populated(client):
    client.put(
        "/api/settings/linkedin_title_defaults",
        json={"value": ["Director of Nursing", "Charge Nurse"]},
    )
    created = client.post("/api/companies", json={"name": "Seeded"}).json()
    assert created["linkedin_title_searches"] == ["Director of Nursing", "Charge Nurse"]


def test_create_keeps_explicit_titles(client):
    created = client.post(
        "/api/companies", json={"name": "Custom", "linkedin_title_searches": ["VP Design"]}
    ).json()
    assert created["linkedin_title_searches"] == ["VP Design"]


# --- Add-time ATS check (QA pass 2, Part C). The background work is stubbed by
# the autouse _no_background_onboarding fixture; here we assert the synchronous
# 'checking' stamp and that the onboarding spawn fires with the new id. ---


def test_create_with_url_stamps_checking(client):
    created = client.post(
        "/api/companies", json={"name": "Probed", "website": "https://probed.example"}
    ).json()
    assert created["ats_last_status"] == "checking"


def test_create_without_url_no_check(client):
    created = client.post("/api/companies", json={"name": "NoUrl"}).json()
    assert created["ats_last_status"] is None


def test_create_with_url_spawns_onboarding(client, monkeypatch):
    calls = []
    monkeypatch.setattr("jshq.main._spawn_onboarding", lambda company_id: calls.append(company_id))
    created = client.post(
        "/api/companies", json={"name": "Spawned", "careers_url": "https://spawned.example/jobs"}
    ).json()
    assert calls == [created["id"]]


# --- URL edits re-probe (the add-time check, again) + POST /detect. Before
# this, only POST /api/companies ever detected anything: adding a careers URL
# to an existing company was a silent no-op, though the wizard's done step
# already promised it "gets another look". ---


def _settle(db, company_id, status="none: no ATS detected"):
    """Land the add-time 'checking' stamp on a settled outcome (the stubbed
    background task never will)."""
    db.execute("UPDATE companies SET ats_last_status = ? WHERE id = ?", (status, company_id))
    db.commit()


def _capture_spawns(monkeypatch):
    calls = []
    monkeypatch.setattr("jshq.main._spawn_onboarding", lambda company_id: calls.append(company_id))
    return calls


def test_put_careers_url_change_reprobes(client, db, monkeypatch):
    created = create(client)
    _settle(db, created["id"])
    calls = _capture_spawns(monkeypatch)
    updated = client.put(
        f"/api/companies/{created['id']}",
        json={**COMPANY, "careers_url": "https://acme.example/careers"},
    ).json()
    assert updated["ats_last_status"] == "checking"
    assert calls == [created["id"]]


def test_put_website_change_reprobes(client, db, monkeypatch):
    created = create(client)
    _settle(db, created["id"])
    calls = _capture_spawns(monkeypatch)
    updated = client.put(
        f"/api/companies/{created['id']}",
        json={**COMPANY, "website": "https://acme-studio.example"},
    ).json()
    assert updated["ats_last_status"] == "checking"
    assert calls == [created["id"]]


def test_put_unrelated_edit_never_probes(client, db, monkeypatch):
    created = create(client)
    _settle(db, created["id"])
    calls = _capture_spawns(monkeypatch)
    updated = client.put(
        f"/api/companies/{created['id']}", json={**COMPANY, "priority": 5}
    ).json()
    assert updated["ats_last_status"] == "none: no ATS detected"
    assert calls == []


def test_put_cosmetic_url_touchup_stays_quiet(client, db, monkeypatch):
    # Same site in new dressing — scheme/www/trailing slash are not a change.
    created = create(client)
    _settle(db, created["id"])
    calls = _capture_spawns(monkeypatch)
    updated = client.put(
        f"/api/companies/{created['id']}",
        json={**COMPANY, "website": "http://www.acme.example/"},
    ).json()
    assert updated["ats_last_status"] == "none: no ATS detected"
    assert calls == []


def test_put_cleared_urls_never_probe(client, db, monkeypatch):
    created = create(client)
    _settle(db, created["id"])
    calls = _capture_spawns(monkeypatch)
    updated = client.put(
        f"/api/companies/{created['id']}",
        json={**COMPANY, "website": None, "careers_url": None},
    ).json()
    assert updated["ats_last_status"] == "none: no ATS detected"
    assert calls == []


def test_put_probes_a_url_corrected_mid_check(client, monkeypatch):
    # The commonest correction is fixing a typo'd URL seconds after the add —
    # exactly while the add-time probe runs. The old in-flight guard skipped
    # the re-probe and nothing ever re-queued it: the check settled against
    # the typo while the pane promised a corrected URL "gets re-checked when
    # it saves". Overlapping probes are safe (each task re-reads its row;
    # the pull half serializes under _refresh_lock).
    created = create(client)  # the add itself stamped 'checking' (spawn stubbed)
    calls = _capture_spawns(monkeypatch)
    updated = client.put(
        f"/api/companies/{created['id']}",
        json={**COMPANY, "careers_url": "https://acme.example/jobs"},
    ).json()
    assert updated["ats_last_status"] == "checking"
    assert calls == [created["id"]]


def test_detect_missing_404(client):
    assert client.post("/api/companies/999/detect").status_code == 404


def test_detect_without_urls_400(client):
    created = client.post("/api/companies", json={"name": "Bare"}).json()
    response = client.post(f"/api/companies/{created['id']}/detect")
    assert response.status_code == 400
    assert "careers URL" in response.json()["detail"]


def test_detect_stamps_checking_and_spawns(client, db, monkeypatch):
    created = create(client)
    _settle(db, created["id"])
    calls = _capture_spawns(monkeypatch)
    response = client.post(f"/api/companies/{created['id']}/detect")
    assert response.status_code == 202
    assert response.json() == {"started": True}
    assert calls == [created["id"]]
    fetched = client.get(f"/api/companies/{created['id']}").json()
    assert fetched["ats_last_status"] == "checking"


def test_detect_while_checking_short_circuits(client, monkeypatch):
    created = create(client)  # 'checking' from the add
    calls = _capture_spawns(monkeypatch)
    response = client.post(f"/api/companies/{created['id']}/detect")
    assert response.status_code == 200
    assert response.json() == {"checking": True}
    assert calls == []


# --- POST /api/companies/careers-preview: no-write ATS probe run BEFORE a
# company exists (the wizard's careers auto-population) or before an edit is
# saved. detect_company is faked (no network); the point is the endpoint's
# shape and that it never touches the database. ---


def _fake_detect(result):
    async def fake(client, company):  # signature matches detect.detect_company
        # Touch the SAME keys real detect_company reads off the row — a preview
        # that forgets one (id/name/website/careers_url) would KeyError live but
        # pass a mock that ignored `company`. Reproduces that as a test failure.
        for key in ("id", "name", "website", "careers_url"):
            company[key]
        return result

    return fake


def test_careers_preview_returns_board_url_without_writing(client, db, monkeypatch):
    monkeypatch.setattr(
        "jshq.ats.detect.detect_company",
        _fake_detect({"ats_type": "greenhouse", "ats_slug": "discord", "errors": []}),
    )
    response = client.post(
        "/api/companies/careers-preview",
        json={"name": "Discord", "website": "https://discord.com"},
    )
    assert response.status_code == 200, response.text
    # The derived board URL is exactly what add-time detection would backfill.
    assert response.json() == {
        "found": True,
        "ats_type": "greenhouse",
        "careers_url": "https://boards.greenhouse.io/discord",
    }
    # Pure preview: no company row was created.
    assert db.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 0


def test_careers_preview_reports_no_board(client, monkeypatch):
    monkeypatch.setattr(
        "jshq.ats.detect.detect_company",
        _fake_detect({"ats_type": None, "ats_slug": None, "errors": ["no ATS"]}),
    )
    response = client.post(
        "/api/companies/careers-preview",
        json={"name": "Custom Co", "website": "https://custom.example"},
    )
    assert response.status_code == 200
    assert response.json() == {"found": False, "ats_type": None, "careers_url": None}


def test_careers_preview_requires_a_url(client):
    response = client.post("/api/companies/careers-preview", json={"name": "No URL Co"})
    assert response.status_code == 400


def test_update(client, db):
    created = create(client)
    db.execute(
        "UPDATE companies SET updated_at = '2000-01-01 00:00:00' WHERE id = ?",
        (created["id"],),
    )
    db.commit()

    response = client.put(
        f"/api/companies/{created['id']}",
        json={**COMPANY, "status": "targeting", "priority": 1},
    )
    assert response.status_code == 200
    updated = response.json()
    assert updated["status"] == "targeting"
    assert updated["priority"] == 1
    assert updated["updated_at"] > "2000-01-01 00:00:00"


def test_update_missing_404(client):
    assert client.put("/api/companies/999", json=COMPANY).status_code == 404


def test_delete_missing_404(client):
    assert client.delete("/api/companies/999").status_code == 404


def test_validation_422(client):
    for payload in (
        {**COMPANY, "name": "  "},
        {**COMPANY, "priority": 0},
        {**COMPANY, "priority": 6},
        {**COMPANY, "values_fit": "amazing"},
    ):
        assert client.post("/api/companies", json=payload).status_code == 422


def test_delete_detaches_contacts_and_removes_activities(client, db):
    company = create(client)
    contact = client.post(
        "/api/contacts", json={"name": "Jane Doe", "company_id": company["id"]}
    ).json()
    db.execute(
        "INSERT INTO activities (entity_type, entity_id, date, type, content)"
        " VALUES ('company', ?, '2026-06-01', 'note', 'company note')",
        (company["id"],),
    )
    db.commit()

    response = client.delete(f"/api/companies/{company['id']}")
    assert response.status_code == 200
    assert response.json() == {
        "deleted": company["id"],
        "contacts_detached": 1,
        "jobs_deleted": 0,
        "applications_deleted": 0,
    }

    contacts = client.get("/api/contacts").json()
    assert contacts[0]["id"] == contact["id"]
    assert contacts[0]["company_id"] is None
    assert contacts[0]["company_name"] is None

    remaining = db.execute(
        "SELECT COUNT(*) AS n FROM activities WHERE entity_type = 'company'"
    ).fetchone()["n"]
    assert remaining == 0


def test_delete_cascades_jobs_applications_and_reminders(client, db):
    company = create(client)
    other = create(client, name="Bystander Co")
    job_id = db.execute(
        "INSERT INTO jobs (company_id, title, dedupe_key) VALUES (?, 'Design Director', 'k1')",
        (company["id"],),
    ).lastrowid
    other_job = db.execute(
        "INSERT INTO jobs (company_id, title, dedupe_key) VALUES (?, 'Head of Design', 'k2')",
        (other["id"],),
    ).lastrowid
    app_id = db.execute(
        "INSERT INTO applications (job_id, status) VALUES (?, 'applied')", (job_id,)
    ).lastrowid
    for entity_type, entity_id in (("job", job_id), ("application", app_id), ("company", company["id"])):
        db.execute(
            "INSERT INTO activities (entity_type, entity_id, date, type, content)"
            " VALUES (?, ?, '2026-06-01', 'note', 'n')",
            (entity_type, entity_id),
        )
        db.execute(
            "INSERT INTO reminders (title, entity_type, entity_id, due_date)"
            " VALUES ('r', ?, ?, '2026-06-20')",
            (entity_type, entity_id),
        )
    db.commit()

    response = client.delete(f"/api/companies/{company['id']}")
    assert response.status_code == 200
    assert response.json() == {
        "deleted": company["id"],
        "contacts_detached": 0,
        "jobs_deleted": 1,
        "applications_deleted": 1,
    }

    for table in ("jobs", "applications", "activities", "reminders"):
        rows = db.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        assert rows == (1 if table == "jobs" else 0), table  # other company's job survives
    assert db.execute("SELECT id FROM jobs").fetchone()["id"] == other_job
    assert [c["id"] for c in client.get("/api/companies").json()] == [other["id"]]


def test_ats_health_fields_exposed(client, seed_company):
    """Pinning test: ats_last_* must reach the UI (Companies health + Today banner).

    list_companies/_serialize_company currently SELECT * — this guards a future
    switch to an explicit column list dropping the fields silently.
    """
    seed_company(
        name="Health Co",
        ats_last_status="ok: 7 matched",
        ats_last_checked="2026-06-11T08:00:00+00:00",
    )
    company = client.get("/api/companies").json()[0]
    assert company["ats_last_status"] == "ok: 7 matched"
    assert company["ats_last_checked"] == "2026-06-11T08:00:00+00:00"


def test_list_includes_active_job_count(client, seed_job):
    created = create(client)
    create(client, name="Zero Jobs Co")
    seed_job(company_id=created["id"], status="active")
    seed_job(company_id=created["id"], status="applied")
    seed_job(company_id=created["id"], status="dismissed")
    seed_job(company_id=created["id"], status="closed")

    counts = {c["name"]: c["active_job_count"] for c in client.get("/api/companies").json()}
    assert counts == {"Acme Studio": 1, "Zero Jobs Co": 0}


def test_active_job_count_excludes_tier1_hard_fails(client, seed_job):
    """A Tier-1 hard fail (fit_score=0, no LLM cost) is hidden by default in the
    Jobs/Today lists, so it must not inflate active_job_count either — unless the
    user manually elevated it. An unscored active job (fit_score NULL) still
    counts; the SQL `IS NOT 0` is NULL-safe."""
    created = create(client)
    seed_job(company_id=created["id"], status="active", fit_score=0)  # hard fail -> excluded
    seed_job(company_id=created["id"], status="active", fit_score=0, manually_elevated=1)  # elevated -> counts
    seed_job(company_id=created["id"], status="active", fit_score=None)  # unscored -> counts
    seed_job(company_id=created["id"], status="active", fit_score=80)  # positive -> counts

    counts = {c["name"]: c["active_job_count"] for c in client.get("/api/companies").json()}
    assert counts["Acme Studio"] == 3


def test_create_and_update_return_active_job_count(client, seed_job):
    """PUT replaces the row client-side without a list refetch (quiet save) —
    every company payload must carry the count, not just the list."""
    created = create(client)
    assert created["active_job_count"] == 0
    seed_job(company_id=created["id"])
    response = client.put(f"/api/companies/{created['id']}", json=COMPANY)
    assert response.status_code == 200
    assert response.json()["active_job_count"] == 1


def test_create_rejects_duplicate_name_case_insensitive(client):
    create(client)
    r = client.post("/api/companies", json={"name": "  acme   STUDIO "})
    assert r.status_code == 409
    body = r.json()["detail"]
    assert body["message"].startswith("Acme Studio is already on your board")
    assert "same name" in body["message"]
    assert body["company_id"]
    # nothing was inserted
    assert len(client.get("/api/companies").json()) == 1


def test_create_rejects_duplicate_website_host(client):
    created = create(client)  # website https://acme.example
    r = client.post(
        "/api/companies", json={"name": "Totally Different", "website": "http://WWW.acme.example/careers/"}
    )
    assert r.status_code == 409
    assert r.json()["detail"]["company_id"] == created["id"]
    assert "same website" in r.json()["detail"]["message"]


def test_create_rejects_duplicate_careers_url_but_not_shared_ats_host(client):
    create(client, careers_url="https://boards.greenhouse.example/acme")
    # Same hosted-ATS HOST but a different board path = a different company.
    ok = client.post(
        "/api/companies",
        json={
            "name": "Bravo Robotics",
            "website": "https://bravo.example",
            "careers_url": "https://boards.greenhouse.example/bravo",
        },
    )
    assert ok.status_code == 201, ok.text
    # The same WHOLE careers URL (www/scheme/trailing-slash noise aside) is a dupe.
    r = client.post(
        "/api/companies",
        json={
            "name": "Bravo Second Try",
            "website": "https://bravo2.example",
            "careers_url": "http://www.boards.greenhouse.example/bravo/",
        },
    )
    assert r.status_code == 409
    assert "same careers URL" in r.json()["detail"]["message"]


def test_create_without_urls_only_guards_by_name(client):
    create(client, name="No Urls Inc", website=None)
    ok = client.post("/api/companies", json={"name": "Different Name Inc"})
    assert ok.status_code == 201, ok.text


def test_careers_preview_malformed_url_degrades_not_500(client):
    # A malformed host raises idna.IDNAError / httpx.InvalidURL (both ValueError,
    # not httpx.HTTPError) during host encoding, BEFORE any network I/O, so this
    # stays offline. The guard-less careers-preview route used to propagate it as
    # a 500 on user-typed input; it must degrade to the no-board-found result.
    for bad in ("xn--", "https://xn---", "http://xn--a"):
        r = client.post("/api/companies/careers-preview", json={"website": bad})
        assert r.status_code == 200, (bad, r.text)
        assert r.json()["found"] is False


def test_init_db_clears_stale_checking_status(db_path, seed_company):
    # A company interrupted mid-detect/pull (Ctrl-C / crash / shutdown cancel)
    # is left committed at 'checking' -- a permanent spinner with both recovery
    # endpoints short-circuited. init_db runs at boot when no task can be live,
    # so it clears a stale 'checking' back to NULL and leaves terminal states be.
    from jshq.db import connect, init_db

    stuck = seed_company(name="Stuck Co", ats_last_status="checking")
    done = seed_company(name="Done Co", ats_last_status="ok: 3 matched")
    errored = seed_company(name="Errored Co", ats_last_status="error: timeout")

    init_db(db_path)  # simulate a fresh boot

    conn = connect(db_path)
    try:
        rows = {
            r["id"]: r["ats_last_status"]
            for r in conn.execute("SELECT id, ats_last_status FROM companies")
        }
    finally:
        conn.close()
    assert rows[stuck] is None
    assert rows[done] == "ok: 3 matched"
    assert rows[errored] == "error: timeout"
