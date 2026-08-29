"""Refresh pipeline tests: dedupe, decay, failure isolation. Adapters are faked."""

import asyncio
import json

import httpx
import pytest

from jshq.ats import refresh as refresh_mod
from jshq.ats.normalize import AdapterError, NormalizedJob
from jshq.db import connect as connect_db


def njob(**overrides) -> NormalizedJob:
    base = dict(
        external_id="J1", title="Product Designer", url="https://example.com/j1",
        location="Remote - US", remote_type="remote", salary_min=None,
        salary_max=None, salary_stated=False, description_text="Design things.",
    )
    base.update(overrides)
    return NormalizedJob(**base)


def run(conn, adapters: dict, company_ids=None):
    """Run the pipeline with ADAPTERS swapped for fakes keyed by ats_type.
    company_ids scopes the run (the bulk retry-failed path)."""

    def make_fake(value):
        # **_kw: real adapters take a per-run `config` for CONFIG_AWARE types
        async def fake(client, slug, title_filter, **_kw):
            if isinstance(value, Exception):
                raise value
            return value

        return fake

    fakes = {ats_type: make_fake(v) for ats_type, v in adapters.items()}
    original = refresh_mod.ADAPTERS
    refresh_mod.ADAPTERS = fakes
    try:
        return asyncio.run(refresh_mod.run_refresh(conn, company_ids=company_ids))
    finally:
        refresh_mod.ADAPTERS = original


def job_row(db, dedupe_key):
    return db.execute("SELECT * FROM jobs WHERE dedupe_key = ?", (dedupe_key,)).fetchone()


def test_insert_sets_all_fields(db, seed_company):
    cid = seed_company()
    result = run(db, {"greenhouse": [njob(salary_min=150000, salary_max=190000, salary_stated=True)]})

    row = job_row(db, f"{cid}:J1")
    assert row["title"] == "Product Designer"
    assert row["status"] == "active"
    assert row["miss_count"] == 0
    assert row["level_band"] == "ic"
    assert row["salary_min"] == 150000 and row["salary_stated"] == 1
    assert row["first_seen"] == row["last_seen"] == result["last_refresh"]
    assert db.execute("SELECT value FROM settings WHERE key='last_refresh'").fetchone()[0] == result["last_refresh"]
    company = db.execute("SELECT * FROM companies WHERE id = ?", (cid,)).fetchone()
    assert company["ats_last_status"] == "ok: 1 matched"
    assert company["ats_last_checked"] == result["last_refresh"]


def test_rerun_updates_without_duplicating(db, seed_company):
    cid = seed_company()
    run(db, {"greenhouse": [njob()]})
    run(db, {"greenhouse": [njob(title="Senior Product Designer")]})

    rows = db.execute("SELECT * FROM jobs WHERE company_id = ?", (cid,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["title"] == "Senior Product Designer"
    assert rows[0]["miss_count"] == 0


def test_decay_two_misses_closes_then_reappear_reactivates(db, seed_company):
    cid = seed_company()
    first = run(db, {"greenhouse": [njob()]})
    key = f"{cid}:J1"

    run(db, {"greenhouse": []})  # miss 1
    assert (job_row(db, key)["miss_count"], job_row(db, key)["status"]) == (1, "active")

    run(db, {"greenhouse": []})  # miss 2 -> closed
    assert job_row(db, key)["status"] == "closed"

    run(db, {"greenhouse": [njob()]})  # listing returns
    row = job_row(db, key)
    assert row["status"] == "active"
    assert row["miss_count"] == 0
    assert row["first_seen"] == first["last_refresh"]  # preserved across reactivation


def test_user_states_never_overwritten(db, seed_company):
    cid = seed_company()
    run(db, {"greenhouse": [njob()]})
    db.execute("UPDATE jobs SET status = 'dismissed' WHERE dedupe_key = ?", (f"{cid}:J1",))
    db.commit()

    run(db, {"greenhouse": [njob()]})  # still listed
    assert job_row(db, f"{cid}:J1")["status"] == "dismissed"

    # Dismissed rows are exempt from the miss count as well as the status flip —
    # you've already decided against the job, so whether the req closed is moot.
    # Deliberate asymmetry with applied rows (see the next test).
    run(db, {"greenhouse": []})  # gone from board: dismissed jobs don't decay
    run(db, {"greenhouse": []})
    row = job_row(db, f"{cid}:J1")
    assert row["status"] == "dismissed"
    assert row["miss_count"] == 0


def test_applied_job_accrues_misses_but_keeps_status(db, seed_company):
    """An applied job can't be flipped to 'closed' without destroying the
    user-owned state, so decay counts its misses instead: miss_count >=
    MISS_LIMIT on an applied row is how the UI knows the req was pulled."""
    cid = seed_company()
    run(db, {"greenhouse": [njob()]})
    key = f"{cid}:J1"
    db.execute("UPDATE jobs SET status = 'applied' WHERE dedupe_key = ?", (key,))
    db.commit()

    run(db, {"greenhouse": [njob()]})  # still listed: no misses
    assert (job_row(db, key)["status"], job_row(db, key)["miss_count"]) == ("applied", 0)

    run(db, {"greenhouse": []})  # miss 1
    assert (job_row(db, key)["status"], job_row(db, key)["miss_count"]) == ("applied", 1)

    run(db, {"greenhouse": []})  # miss 2 — counted, but NEVER flipped to closed
    row = job_row(db, key)
    assert row["status"] == "applied"
    assert row["miss_count"] == refresh_mod.MISS_LIMIT

    run(db, {"greenhouse": [njob()]})  # re-listed: the signal clears itself
    row = job_row(db, key)
    assert row["status"] == "applied"
    assert row["miss_count"] == 0


def test_manual_applied_job_never_accrues_misses(db, seed_company):
    cid = seed_company(name="OkCo", ats_type="greenhouse", ats_slug="okco")
    run(db, {"greenhouse": [njob()]})
    # A hand-entered job isn't on the board we poll, so "absent from the fetch"
    # says nothing about it — applied or not, it must never accrue a miss.
    db.execute(
        """INSERT INTO jobs (company_id, title, status, miss_count, source, dedupe_key,
               first_seen, last_seen)
           VALUES (?, 'Manual Role', 'applied', 0, 'manual', ?, '2026-06-01', '2026-06-01')""",
        (cid, f"manual:{cid}:abc"),
    )
    db.commit()

    run(db, {"greenhouse": []})
    run(db, {"greenhouse": []})

    manual = job_row(db, f"manual:{cid}:abc")
    assert manual["status"] == "applied"
    assert manual["miss_count"] == 0


def test_failing_adapter_isolates_company(db, seed_company):
    cid_ok = seed_company(name="OkCo", ats_type="greenhouse", ats_slug="okco")
    cid_bad = seed_company(name="BadCo", ats_type="ashby", ats_slug="badco")
    run(db, {"greenhouse": [njob()], "ashby": [njob(external_id="B1", title="Design Lead")]})
    assert job_row(db, f"{cid_bad}:B1")["status"] == "active"

    # BadCo's adapter now fails twice; its jobs must not decay.
    result = run(db, {"greenhouse": [njob()], "ashby": AdapterError("HTTP 503")})
    result = run(db, {"greenhouse": [njob()], "ashby": AdapterError("HTTP 503")})

    bad_row = job_row(db, f"{cid_bad}:B1")
    assert bad_row["status"] == "active"
    assert bad_row["miss_count"] == 0
    bad_company = db.execute("SELECT * FROM companies WHERE id = ?", (cid_bad,)).fetchone()
    assert bad_company["ats_last_status"] == "error: HTTP 503"
    ok_company = db.execute("SELECT * FROM companies WHERE id = ?", (cid_ok,)).fetchone()
    assert ok_company["ats_last_status"] == "ok: 1 matched"
    # the run still completes and stamps last_refresh
    assert db.execute("SELECT value FROM settings WHERE key='last_refresh'").fetchone()[0] == result["last_refresh"]

    # ...and writes a completion report for the Today bar: 1 of 2 refreshed,
    # with BadCo's failure reason carried through.
    rep = json.loads(_setting(db, "last_refresh_report"))
    assert rep["total"] == 2 and rep["refreshed"] == 1
    assert [f["name"] for f in rep["failures"]] == ["BadCo"]
    assert "503" in rep["failures"][0]["reason"]
    # the live progress global is cleared once the run finishes
    assert refresh_mod.REFRESH_PROGRESS is None


def test_network_outage_skips_last_refresh_and_preserves_health(db, seed_company):
    cid = seed_company(name="OkCo", ats_type="greenhouse", ats_slug="okco")
    good = run(db, {"greenhouse": [njob()]})
    assert job_row(db, f"{cid}:J1")["status"] == "active"
    assert _company(db, cid)["ats_last_status"] == "ok: 1 matched"

    # The whole network goes down: every board fails with a connectivity error.
    dns = AdapterError(
        "GET https://boards-api.greenhouse.io/v1/boards/okco/jobs: "
        "ConnectError: [Errno 8] nodename nor servname provided, or not known"
    )
    outage = run(db, {"greenhouse": dns})

    assert outage["outage"] is True
    # last_refresh is NOT advanced — the on-load backstop will retry.
    assert _setting(db, "last_refresh") == good["last_refresh"]
    # Company health + jobs are left exactly as they were (no error: stamp, no decay).
    co = _company(db, cid)
    assert co["ats_last_status"] == "ok: 1 matched"
    assert job_row(db, f"{cid}:J1")["status"] == "active"
    assert job_row(db, f"{cid}:J1")["miss_count"] == 0
    # An outage marker is recorded for the UI.
    assert json.loads(_setting(db, "last_refresh_error"))["reason"] == "offline"


def test_partial_or_http_failure_is_not_an_outage_and_clears_marker(db, seed_company):
    cid_ok = seed_company(name="OkCo", ats_type="greenhouse", ats_slug="okco")
    cid_bad = seed_company(name="BadCo", ats_type="ashby", ats_slug="badco")
    # A stale outage marker from a prior offline run.
    db.execute("INSERT INTO settings (key, value) VALUES ('last_refresh_error', ?)",
               ('{"at": "x", "reason": "offline", "attempted": 2}',))
    db.commit()

    # A real HTTP 404 proves the internet was reachable — NOT an outage, even
    # though only one board succeeds.
    result = run(db, {"greenhouse": [njob()], "ashby": AdapterError("HTTP 404")})

    assert not result.get("outage")
    assert _setting(db, "last_refresh") == result["last_refresh"]  # stamped normally
    assert _company(db, cid_bad)["ats_last_status"].startswith("error:")  # genuine failure surfaces
    assert _company(db, cid_ok)["ats_last_status"] == "ok: 1 matched"
    assert _setting(db, "last_refresh_error") is None  # stale marker cleared


def test_majority_connectivity_failure_is_an_outage(db, seed_company):
    """Most boards dropping with connectivity errors (asleep/just-woke) is ONE
    outage, even though a few boards still answered — not N per-board failures."""
    seed_company(name="OkCo", ats_type="greenhouse", ats_slug="okco")
    seed_company(name="BadCo1", ats_type="ashby", ats_slug="badco1")
    seed_company(name="BadCo2", ats_type="workday", ats_slug="badco2")
    # Baseline: a fully successful run stamps last_refresh.
    good = run(db, {"greenhouse": [njob()], "ashby": [njob()], "workday": [njob()]})

    dns = AdapterError("ConnectError: [Errno 8] nodename nor servname provided, or not known")
    timeout = AdapterError("ReadTimeout: ")
    # 2 of 3 boards fail connectivity -> majority -> outage (the 1 success is abandoned).
    outage = run(db, {"greenhouse": [njob()], "ashby": dns, "workday": timeout})

    assert outage["outage"] is True
    marker = json.loads(_setting(db, "last_refresh_error"))
    assert marker["reason"] == "offline"
    assert marker["attempted"] == 2  # the two unreachable boards, not all 3
    assert _setting(db, "last_refresh") == good["last_refresh"]  # NOT advanced -> backstop retries


def test_minority_connectivity_failure_is_not_an_outage(db, seed_company):
    """One board dropping while the rest succeed is a normal partial failure, not an
    outage — guards against over-triggering the offline banner."""
    cid_ok = seed_company(name="OkCo1", ats_type="greenhouse", ats_slug="okco1")
    seed_company(name="OkCo2", ats_type="ashby", ats_slug="okco2")
    cid_bad = seed_company(name="BadCo", ats_type="workday", ats_slug="badco")
    db.execute("INSERT INTO settings (key, value) VALUES ('last_refresh_error', ?)",
               ('{"at": "x", "reason": "offline", "attempted": 3}',))
    db.commit()

    dns = AdapterError("ConnectError: [Errno 8] nodename nor servname provided, or not known")
    result = run(db, {"greenhouse": [njob()], "ashby": [njob()], "workday": dns})

    assert not result.get("outage")  # 1 of 3 connectivity -> minority -> partial
    assert _setting(db, "last_refresh") == result["last_refresh"]  # stamped normally
    assert _company(db, cid_bad)["ats_last_status"].startswith("error:")  # genuine failure surfaces
    assert _company(db, cid_ok)["ats_last_status"] == "ok: 1 matched"
    assert _setting(db, "last_refresh_error") is None  # stale marker cleared


def test_manual_edit_survives_refresh(db, seed_company):
    """A user-corrected location/salary (manually_edited) is preserved when the board
    re-publishes the job with its own (wrong/missing) values."""
    cid = seed_company(name="OkCo", ats_type="greenhouse", ats_slug="okco")
    run(db, {"greenhouse": [njob(location="Remote - US", salary_min=None, salary_max=None, salary_stated=False)]})
    key = f"{cid}:J1"
    db.execute(
        "UPDATE jobs SET manually_edited = 1, location = ?, remote_type = ?, "
        "salary_min = ?, salary_max = ?, salary_stated = 1 WHERE dedupe_key = ?",
        ("Evanston, IL", "hybrid", 170000, 200000, key),
    )
    db.commit()

    # Same posting (external_id J1), different facts from the board.
    run(db, {"greenhouse": [njob(location="Somewhere, ZZ", salary_min=90000, salary_max=100000, salary_stated=True)]})

    row = job_row(db, key)
    assert row["location"] == "Evanston, IL"
    assert row["remote_type"] == "hybrid"
    assert (row["salary_min"], row["salary_max"], row["salary_stated"]) == (170000, 200000, 1)


def test_unedited_job_still_takes_ats_values(db, seed_company):
    """Control: without manually_edited, the refresh overwrites facts from the board."""
    cid = seed_company(name="OkCo", ats_type="greenhouse", ats_slug="okco")
    run(db, {"greenhouse": [njob(location="Remote - US", salary_min=None, salary_max=None, salary_stated=False)]})
    run(db, {"greenhouse": [njob(location="Austin, TX", salary_min=140000, salary_max=160000, salary_stated=True)]})

    row = job_row(db, f"{cid}:J1")
    assert row["location"] == "Austin, TX"
    assert (row["salary_min"], row["salary_max"], row["salary_stated"]) == (140000, 160000, 1)


def test_manual_job_is_exempt_from_decay(db, seed_company):
    cid = seed_company(name="OkCo", ats_type="greenhouse", ats_slug="okco")
    run(db, {"greenhouse": [njob()]})  # one ATS-pulled job
    # A hand-entered manual job at the same company (decay must never touch it,
    # even once the company gains a real adapter).
    db.execute(
        """INSERT INTO jobs (company_id, title, status, miss_count, source, dedupe_key,
               first_seen, last_seen)
           VALUES (?, 'Manual Role', 'active', 0, 'manual', ?, '2026-06-01', '2026-06-01')""",
        (cid, f"manual:{cid}:abc"),
    )
    db.commit()

    run(db, {"greenhouse": []})  # board drops the ATS job
    run(db, {"greenhouse": []})  # second miss -> ATS job closes

    assert job_row(db, f"{cid}:J1")["status"] == "closed"
    manual = job_row(db, f"manual:{cid}:abc")
    assert manual["status"] == "active"
    assert manual["miss_count"] == 0


def _company(db, cid):
    return db.execute("SELECT * FROM companies WHERE id = ?", (cid,)).fetchone()


def _setting(db, key):
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def test_unexpected_exception_is_caught(db, seed_company):
    seed_company()
    result = run(db, {"greenhouse": ValueError("boom")})
    assert result["companies"][0]["status"].startswith("error: unexpected: ValueError")


def test_manual_and_null_companies_skipped(db, seed_company):
    seed_company(name="ManualCo", ats_type="manual", ats_slug=None)
    seed_company(name="UnknownCo", ats_type=None, ats_slug=None)
    result = run(db, {"greenhouse": [njob()]})
    assert result["companies"] == []
    assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_intra_batch_duplicates_collapse(db, seed_company):
    cid = seed_company()
    run(db, {"greenhouse": [njob(), njob()]})
    assert db.execute("SELECT COUNT(*) FROM jobs WHERE company_id = ?", (cid,)).fetchone()[0] == 1


def test_dedupe_key_without_external_id(db, seed_company):
    cid = seed_company()
    run(db, {"greenhouse": [njob(external_id=None, title="Design Lead", location="NYC")]})
    assert job_row(db, f"{cid}:design lead|nyc") is not None


def test_is_running_flag():
    assert refresh_mod.is_running() is False


def test_changed_description_clears_scoring_columns(db, seed_company):
    cid = seed_company()
    run(db, {"greenhouse": [njob()]})
    db.execute(
        """UPDATE jobs SET fit_score = 80, fit_quadrant = 'energizing_strength',
               tier1_results = '{}', near_miss_flags = '[]', scoring_notes = 'x'
           WHERE dedupe_key = ?""",
        (f"{cid}:J1",),
    )
    db.commit()

    run(db, {"greenhouse": [njob()]})  # unchanged JD -> scores preserved
    assert job_row(db, f"{cid}:J1")["fit_score"] == 80

    run(db, {"greenhouse": [njob(description_text="Totally new responsibilities.")]})
    row = job_row(db, f"{cid}:J1")
    assert row["fit_score"] is None
    assert row["tier1_results"] is None
    assert row["scoring_notes"] is None


def test_scoring_skipped_without_key_but_reported(db, seed_company):
    from jshq import apikey

    seed_company()
    result = run(db, {"greenhouse": [njob()]})
    assert result["scoring"] == {"skipped": apikey.MISSING_MESSAGE}


def test_rescore_records_the_skip(db):
    """A manual Rescore with no key stores a skipped report (not nothing), so the
    System tab can say why nothing scored instead of showing a stale success."""
    from jshq import apikey

    result = asyncio.run(refresh_mod.run_rescore(db))
    assert result["scoring"]["skipped"] == apikey.MISSING_MESSAGE
    stored = json.loads(
        db.execute(
            "SELECT value FROM settings WHERE key = 'last_scoring_report'"
        ).fetchone()["value"]
    )
    assert stored["skipped"] == apikey.MISSING_MESSAGE
    assert "at" in stored


def test_scoring_crash_does_not_break_refresh(db, seed_company, monkeypatch):
    async def boom(conn, **kwargs):
        raise RuntimeError("scoring exploded")

    monkeypatch.setattr(refresh_mod.scoring, "run_scoring", boom)
    seed_company()
    result = run(db, {"greenhouse": [njob()]})
    assert result["scoring"]["skipped"].startswith("scoring crashed:")
    # refresh itself completed and stamped last_refresh
    assert db.execute("SELECT value FROM settings WHERE key='last_refresh'").fetchone()[0] == result["last_refresh"]


# --- URL scheme normalization: a scheme-less website ("discord.com") is what
# users type, but httpx rejects it — so detection silently failed everywhere
# until detect._with_scheme fronted the fetch. ---


def test_with_scheme_adds_https_when_missing():
    from jshq.ats import detect

    assert detect._with_scheme("discord.com") == "https://discord.com"
    assert detect._with_scheme("discord.com/careers") == "https://discord.com/careers"
    # An explicit scheme is preserved (http stays http; https stays https).
    assert detect._with_scheme("http://x.example") == "http://x.example"
    assert detect._with_scheme("https://x.example/jobs") == "https://x.example/jobs"
    # Empty / None pass through untouched (the "no URL to probe" path).
    assert detect._with_scheme("") == ""
    assert detect._with_scheme(None) is None


# --- detect_and_fetch_company: add-time ATS onboarding (QA pass 2) ---
# detect_company + the adapters are faked (no network); the function opens its
# own connection, so db.connect is pointed at the test DB.


def _fake_adapter(value):
    async def fake(client, slug, title_filter):
        if isinstance(value, Exception):
            raise value
        return value

    return fake


def onboard(db_path, company_id, monkeypatch, *, detect_result, adapters=None):
    async def fake_detect(client, row):
        if isinstance(detect_result, Exception):
            raise detect_result
        return detect_result

    async def _no_logo(conn, company_id, **kwargs):
        return False  # onboarding caches a logo over the network — never in tests

    monkeypatch.setattr(refresh_mod.db, "connect", lambda *a, **k: connect_db(db_path))
    monkeypatch.setattr(refresh_mod, "detect_company", fake_detect)
    monkeypatch.setattr("jshq.logos.refresh_company_logo", _no_logo)
    if adapters is not None:
        monkeypatch.setattr(
            refresh_mod, "ADAPTERS", {k: _fake_adapter(v) for k, v in adapters.items()}
        )
    return asyncio.run(refresh_mod.detect_and_fetch_company(company_id))


def test_onboard_detected_pulls_and_scores(db, db_path, seed_company, monkeypatch):
    cid = seed_company(
        name="NewCo", ats_type=None, ats_slug=None, website="https://newco.example"
    )
    result = onboard(
        db_path,
        cid,
        monkeypatch,
        detect_result={
            "id": cid, "name": "NewCo", "ats_type": "greenhouse", "ats_slug": "newco",
            "method": "html-scan", "evidence": {}, "errors": [],
        },
        adapters={"greenhouse": [njob()]},
    )
    assert result["status"] == "ok: 1 matched"
    company = db.execute("SELECT * FROM companies WHERE id = ?", (cid,)).fetchone()
    assert company["ats_type"] == "greenhouse"
    assert company["ats_slug"] == "newco"  # detected ats now enrolls it in run()
    assert company["ats_last_status"] == "ok: 1 matched"
    assert db.execute("SELECT COUNT(*) FROM jobs WHERE company_id = ?", (cid,)).fetchone()[0] == 1


def test_onboard_detected_but_unsupported_ats_sets_none(db, db_path, seed_company, monkeypatch):
    # A detected ATS with no adapter must degrade to a clear "none:" status
    # instead of crashing in the fetch (the pre-2026-08 Lever gap: a tracked
    # company got "error: unexpected: KeyError: 'lever'"). Detection stays persisted
    # so the gap is visible on the company card.
    cid = seed_company(
        name="FutureCo", ats_type=None, ats_slug=None, website="https://futureco.example"
    )
    result = onboard(
        db_path,
        cid,
        monkeypatch,
        detect_result={
            "id": cid, "name": "FutureCo", "ats_type": "newats", "ats_slug": "futureco",
            "method": "html-scan", "evidence": {}, "errors": [],
        },
        adapters={"greenhouse": [njob()]},  # registry without 'newats'
    )
    assert result["status"] == "none: newats detected but no adapter supports it"
    company = db.execute("SELECT * FROM companies WHERE id = ?", (cid,)).fetchone()
    assert company["ats_type"] == "newats"  # detection persisted
    assert company["ats_last_status"].startswith("none:")
    assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_onboard_no_ats_sets_none(db, db_path, seed_company, monkeypatch):
    cid = seed_company(
        name="ManualCo", ats_type=None, ats_slug=None, website="https://manualco.example"
    )
    result = onboard(
        db_path,
        cid,
        monkeypatch,
        detect_result={"id": cid, "name": "ManualCo", "ats_type": None, "ats_slug": None, "errors": ["x"]},
    )
    assert result["status"] == "none: no ATS detected"
    company = db.execute("SELECT * FROM companies WHERE id = ?", (cid,)).fetchone()
    assert company["ats_type"] is None
    assert company["ats_last_status"] == "none: no ATS detected"
    assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_onboard_no_ats_disconnects_a_previously_detected_board(
    db, db_path, seed_company, monkeypatch
):
    # Re-detection runs on CONNECTED companies now (a URL edit, "Check
    # again"). Finding nothing must also clear ats_type/ats_slug: a stale
    # slug would keep the company enrolled in the scheduled-refresh query, so
    # the board the pane says doesn't exist reappears as "ok: N matched" on
    # the next run.
    cid = seed_company(
        name="MovedCo", ats_type="greenhouse", ats_slug="movedco",
        website="https://movedco.example",
    )
    result = onboard(
        db_path,
        cid,
        monkeypatch,
        detect_result={"id": cid, "name": "MovedCo", "ats_type": None, "ats_slug": None, "errors": []},
    )
    assert result["status"] == "none: no ATS detected"
    company = db.execute("SELECT * FROM companies WHERE id = ?", (cid,)).fetchone()
    assert company["ats_type"] is None
    assert company["ats_slug"] is None


def test_onboard_exception_sets_error(db, db_path, seed_company, monkeypatch):
    cid = seed_company(
        name="BoomCo", ats_type=None, ats_slug=None, website="https://boomco.example"
    )
    result = onboard(db_path, cid, monkeypatch, detect_result=RuntimeError("kaboom"))
    assert result["status"].startswith("error:")
    company = db.execute("SELECT * FROM companies WHERE id = ?", (cid,)).fetchone()
    assert company["ats_last_status"].startswith("error:")


# --- on-demand single-board refresh (refresh_company_board + the endpoint) ---


def refresh_one(db_path, company_id, monkeypatch, *, adapters):
    """Drive refresh_company_board against the test DB with faked adapters (no
    detection) — mirrors `onboard` for the lower, fetch-only half."""
    monkeypatch.setattr(refresh_mod.db, "connect", lambda *a, **k: connect_db(db_path))

    def make_fake(value):
        async def fake(client, slug, title_filter):
            if isinstance(value, Exception):
                raise value
            return value

        return fake

    monkeypatch.setattr(
        refresh_mod, "ADAPTERS", {k: make_fake(v) for k, v in adapters.items()}
    )
    return asyncio.run(refresh_mod.refresh_company_board(company_id))


def test_refresh_company_board_pulls_and_scores(db, db_path, seed_company, monkeypatch):
    cid = seed_company(name="PullCo", ats_type="greenhouse", ats_slug="pullco")
    result = refresh_one(db_path, cid, monkeypatch, adapters={"greenhouse": [njob()]})
    assert result["status"] == "ok: 1 matched"
    company = db.execute("SELECT * FROM companies WHERE id = ?", (cid,)).fetchone()
    assert company["ats_last_status"] == "ok: 1 matched"
    assert db.execute("SELECT COUNT(*) FROM jobs WHERE company_id = ?", (cid,)).fetchone()[0] == 1


def test_refresh_company_board_records_adapter_error(db, db_path, seed_company, monkeypatch):
    cid = seed_company(name="OracleCo", ats_type="oracle_hcm", ats_slug="exco/CX")
    result = refresh_one(
        db_path, cid, monkeypatch, adapters={"oracle_hcm": AdapterError("ReadTimeout")}
    )
    assert result["status"].startswith("error:") and "ReadTimeout" in result["status"]
    company = db.execute("SELECT * FROM companies WHERE id = ?", (cid,)).fetchone()
    assert company["ats_last_status"].startswith("error:")


def test_company_refresh_endpoint_starts_and_stamps_checking(client, seed_company):
    cid = seed_company(ats_type="greenhouse", ats_slug="x", ats_last_status="error: boom")
    r = client.post(f"/api/companies/{cid}/refresh")
    assert r.status_code == 202 and r.json() == {"started": True}
    # the background worker is stubbed in tests; the endpoint still stamps 'checking'
    assert client.get(f"/api/companies/{cid}").json()["ats_last_status"] == "checking"


def test_company_refresh_endpoint_404_for_missing(client):
    assert client.post("/api/companies/99999/refresh").status_code == 404


def test_company_refresh_endpoint_400_for_manual(client, seed_company):
    cid = seed_company(ats_type="manual", ats_slug=None)
    assert client.post(f"/api/companies/{cid}/refresh").status_code == 400


def test_company_refresh_endpoint_400_for_no_slug(client, seed_company):
    cid = seed_company(ats_type="greenhouse", ats_slug=None)
    assert client.post(f"/api/companies/{cid}/refresh").status_code == 400


def test_company_refresh_endpoint_no_op_while_checking(client, seed_company):
    cid = seed_company(ats_type="greenhouse", ats_slug="x", ats_last_status="checking")
    r = client.post(f"/api/companies/{cid}/refresh")
    assert r.status_code == 200 and r.json() == {"checking": True}


def test_company_refresh_defers_to_full_refresh(client, seed_company, monkeypatch):
    """While a FULL refresh runs it will re-pull every board, so a per-board
    request short-circuits (and leaves the status untouched, not 'checking')."""
    cid = seed_company(ats_type="greenhouse", ats_slug="x", ats_last_status="error: boom")
    monkeypatch.setattr(refresh_mod, "is_full_refresh_running", lambda: True)
    r = client.post(f"/api/companies/{cid}/refresh")
    assert r.status_code == 200 and r.json() == {"running": True}
    assert client.get(f"/api/companies/{cid}").json()["ats_last_status"] == "error: boom"


def test_company_refresh_proceeds_when_only_a_board_refresh_runs(client, seed_company, monkeypatch):
    """Regression: another SINGLE-board refresh holds _refresh_lock (is_running
    True) but doesn't cover this board, so the request must still start + stamp
    'checking' — the spawned task queues on the lock — not bail like a full run."""
    cid = seed_company(ats_type="greenhouse", ats_slug="x", ats_last_status="error: boom")
    monkeypatch.setattr(refresh_mod, "is_running", lambda: True)
    monkeypatch.setattr(refresh_mod, "is_full_refresh_running", lambda: False)
    r = client.post(f"/api/companies/{cid}/refresh")
    assert r.status_code == 202 and r.json() == {"started": True}
    assert client.get(f"/api/companies/{cid}").json()["ats_last_status"] == "checking"


# --- completion pop-ups (app.notify — osascript patched by the autouse fixture) ---


def test_refresh_completion_sends_popup(db, seed_company, notify_calls):
    seed_company()
    seed_company(name="Broken", ats_type="lever", ats_slug="broken")
    run(db, {"greenhouse": [njob()], "lever": AdapterError("boom")})
    assert len(notify_calls) == 1
    assert "1 of 2 boards refreshed — 1 failed" in notify_calls[0]["message"]
    assert notify_calls[0]["sound"] == "Glass"


def test_outage_sends_error_popup(db, seed_company, notify_calls):
    seed_company()
    seed_company(name="Two", ats_type="lever", ats_slug="two")
    dns = AdapterError("ConnectError: [Errno 8] nodename nor servname provided, or not known")
    run(db, {"greenhouse": dns, "lever": dns})
    assert len(notify_calls) == 1
    assert "unreachable" in notify_calls[0]["message"]
    assert notify_calls[0]["sound"] == "Basso"


def test_popup_respects_setting(db, seed_company, notify_calls):
    db.execute("INSERT INTO settings (key, value) VALUES ('notify_popups', 'false')")
    db.commit()
    seed_company()
    run(db, {"greenhouse": [njob()]})
    assert notify_calls == []


# --- scoped retry-failed runs (the bulk "Refresh failing" path) ---


def test_scoped_run_touches_only_given_ids(db, seed_company):
    a = seed_company(name="FailCo", ats_slug="failco", ats_last_status="error: boom")
    b = seed_company(name="OtherCo", ats_slug="otherco", ats_last_status="error: boom")
    run(db, {"greenhouse": [njob()]}, company_ids=[a])
    assert _company(db, a)["ats_last_status"] == "ok: 1 matched"
    assert _company(db, b)["ats_last_status"] == "error: boom"  # untouched
    assert job_row(db, f"{a}:J1") is not None
    assert job_row(db, f"{b}:J1") is None


def test_scoped_run_skips_last_refresh_and_tags_report(db, seed_company):
    cid = seed_company(ats_last_status="error: boom")
    sentinel = "2000-01-01T00:00:00+00:00"
    db.execute(
        "INSERT INTO settings (key, value) VALUES ('last_refresh', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (sentinel,),
    )
    db.commit()
    run(db, {"greenhouse": [njob()]}, company_ids=[cid])
    # A partial run must not mark the whole estate fresh.
    assert _setting(db, "last_refresh") == sentinel
    rep = json.loads(_setting(db, "last_refresh_report"))
    assert rep["scope"] == "failed"
    assert rep["total"] == 1 and rep["refreshed"] == 1


def test_scoped_connectivity_failures_are_not_an_outage(db, seed_company):
    # A retry-failed sample is selection-biased toward timeouts — even 100%
    # connectivity failures must re-stamp per-company statuses (no stranded
    # 'checking' rows) and must not write the offline marker.
    cid = seed_company(ats_last_status="error: old")
    dns = AdapterError(
        "GET https://boards-api.greenhouse.io/v1/boards/testco/jobs: "
        "ConnectError: [Errno 8] nodename nor servname provided, or not known"
    )
    result = run(db, {"greenhouse": dns}, company_ids=[cid])
    assert "outage" not in result
    assert _company(db, cid)["ats_last_status"].startswith("error:")
    assert _setting(db, "last_refresh_error") is None
    rep = json.loads(_setting(db, "last_refresh_report"))
    assert rep["scope"] == "failed" and rep["refreshed"] == 0


def test_scoped_run_clears_outage_marker_only_on_contact(db, seed_company):
    cid = seed_company(ats_last_status="error: old")
    db.execute(
        "INSERT INTO settings (key, value) VALUES ('last_refresh_error', '{\"reason\": \"offline\"}')"
    )
    db.commit()
    dns = AdapterError("GET https://x/jobs: ConnectError: connection refused")
    run(db, {"greenhouse": dns}, company_ids=[cid])
    # Every scoped fetch timed out — that proves nothing about the estate.
    assert _setting(db, "last_refresh_error") is not None
    run(db, {"greenhouse": [njob()]}, company_ids=[cid])
    # A board answered: the machine is demonstrably online.
    assert _setting(db, "last_refresh_error") is None


def test_scoped_run_is_not_a_full_refresh(db, seed_company):
    # The full-refresh flag makes per-board requests defer ("will be covered");
    # a scoped run covers only its ids, so it must not set it.
    cid = seed_company(ats_last_status="error: old")
    seen = []

    async def fake(client, slug, title_filter):
        seen.append(refresh_mod.is_full_refresh_running())
        return [njob()]

    original = refresh_mod.ADAPTERS
    refresh_mod.ADAPTERS = {"greenhouse": fake}
    try:
        asyncio.run(refresh_mod.run_refresh(db, company_ids=[cid]))
    finally:
        refresh_mod.ADAPTERS = original
    assert seen == [False]


# --- per-company title keywords (settings.company_title_keywords) ------------


def _set(db, key, value):
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )
    db.commit()


def test_per_company_keywords_scope_to_that_company(db):
    _set(db, "title_keywords", ["design"])
    _set(db, "company_title_keywords", {"49": ["product lead", "product manager"]})
    f = refresh_mod._title_filter(db)
    assert f(49).search("Product Lead")
    assert f(49).search("Design Lead")
    assert f(7).search("Product Lead") is None  # other companies: global only
    assert f(7).search("Design Lead")


def test_per_company_include_never_beats_global_exclude(db):
    _set(db, "title_keywords", ["design"])
    _set(db, "title_exclude_keywords", ["freelance"])
    _set(db, "company_title_keywords", {"49": ["product lead"]})
    f = refresh_mod._title_filter(db)
    assert f(49).search("Product Lead (Freelance)") is None
    assert f(49).search("Product Lead")


def test_extras_do_not_become_a_gate_when_the_global_list_is_empty(db):
    # Extras WIDEN the global gate. With no global include list there is no
    # gate (everything ingests) — universe ∪ extras is still the universe.
    # Compiling the extras alone would invert the setting into a per-company
    # NARROWING gate: the extras company would silently drop every title but
    # its extras while every other board ingests everything.
    _set(db, "company_title_keywords", {"49": ["product lead"]})
    f = refresh_mod._title_filter(db)
    assert f(49).search("Staff Nurse")
    assert f(49).search("Product Lead")
    assert f(7).search("Staff Nurse")


def test_no_company_map_resolves_to_global_filter_everywhere(db):
    _set(db, "title_keywords", ["design"])
    f = refresh_mod._title_filter(db)
    assert f(1).search("Design Lead")
    assert f(999).search("Product Lead") is None


def test_fetch_config_derives_workday_terms_from_title_keywords(db):
    # Workday's searchText scope follows the ingestion gate: whatever writes
    # title_keywords (rules, manual chips, the wizard's field step) scopes
    # Workday boards for free. The workday_search_terms setting is an override,
    # not the source of truth.
    _set(db, "title_keywords", ["nursing", "oncology"])
    assert refresh_mod._fetch_config(db)["workday_search_terms"] == ["nursing", "oncology"]
    _set(db, "workday_search_terms", ["nurse manager"])
    assert refresh_mod._fetch_config(db)["workday_search_terms"] == ["nurse manager"]


def test_fetch_config_unions_per_company_extras(db):
    # A Workday board whose gate is widened by company_title_keywords must be
    # able to FETCH those titles: searchText is the hard ceiling on what comes
    # back at all, so the derived terms are a superset of every company's
    # gate. The per-company filter of record still gates ingestion.
    _set(db, "title_keywords", ["nursing"])
    _set(db, "company_title_keywords", {"49": ["nurse manager", "nursing"]})
    assert refresh_mod._fetch_config(db)["workday_search_terms"] == [
        "nursing", "nurse manager",
    ]


def test_fetch_config_empty_everywhere_yields_no_terms(db):
    # Fresh install, no field answer yet: the adapter errors (skip path, no
    # decay) rather than inventing a scope (test_adapters covers the error).
    # Extras don't feed the union here: with no global list there is no gate,
    # so they are not include terms in this regime either.
    _set(db, "company_title_keywords", {"49": ["nurse manager"]})
    assert refresh_mod._fetch_config(db)["workday_search_terms"] == []
# --- manual-row liveness (decay for source='manual' via their posting URL) ----


@pytest.fixture(autouse=True)
def _stub_manual_liveness(monkeypatch):
    """Keep the suite offline: the liveness pass GETs each manual row's URL.
    Default verdict is None (indeterminate = inert); tests override per-case."""

    async def indeterminate(client, url):
        return None

    monkeypatch.setattr(refresh_mod, "_check_manual_url", indeterminate)


def _verdict(monkeypatch, value):
    async def check(client, url):
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(refresh_mod, "_check_manual_url", check)


def seed_manual(db, cid, *, status="active", url="https://example.com/manual/1",
                miss_count=0, key="m1"):
    db.execute(
        """INSERT INTO jobs (company_id, title, status, miss_count, source, url,
               dedupe_key, first_seen, last_seen)
           VALUES (?, 'Manual Role', ?, ?, 'manual', ?, ?, '2026-06-01', '2026-06-01')""",
        (cid, status, miss_count, url, f"manual:{cid}:{key}"),
    )
    db.commit()
    return f"manual:{cid}:{key}"


def test_liveness_alive_resets_miss_and_bumps_last_seen(db, seed_company, monkeypatch):
    cid = seed_company()
    key = seed_manual(db, cid, miss_count=1)
    _verdict(monkeypatch, True)
    result = run(db, {"greenhouse": []})
    row = job_row(db, key)
    assert row["miss_count"] == 0
    assert row["last_seen"] == result["last_refresh"]
    assert row["status"] == "active"
    assert result["manual"] == {"checked": 1, "gone": 0, "closed": 0}


def test_liveness_gone_twice_closes_active_row(db, seed_company, monkeypatch):
    cid = seed_company()
    key = seed_manual(db, cid)
    _verdict(monkeypatch, False)
    first = run(db, {"greenhouse": []})
    assert first["manual"] == {"checked": 1, "gone": 1, "closed": 0}
    row = job_row(db, key)
    assert (row["status"], row["miss_count"]) == ("active", 1)
    second = run(db, {"greenhouse": []})
    assert second["manual"] == {"checked": 1, "gone": 1, "closed": 1}
    row = job_row(db, key)
    assert (row["status"], row["miss_count"]) == ("closed", 2)


def test_liveness_applied_accrues_misses_but_keeps_status(db, seed_company, monkeypatch):
    cid = seed_company()
    key = seed_manual(db, cid, status="applied")
    _verdict(monkeypatch, False)
    run(db, {"greenhouse": []})
    result = run(db, {"greenhouse": []})
    row = job_row(db, key)
    # miss_count >= MISS_LIMIT on an applied row IS the "no longer listed"
    # signal (frontend isDelisted); the status is user-owned and never flipped.
    assert (row["status"], row["miss_count"]) == ("applied", 2)
    assert result["manual"]["closed"] == 0


def test_liveness_indeterminate_is_inert(db, seed_company):
    cid = seed_company()
    key = seed_manual(db, cid, miss_count=1)
    result = run(db, {"greenhouse": []})  # autouse stub: verdict None
    row = job_row(db, key)
    assert (row["status"], row["miss_count"]) == ("active", 1)
    assert result["manual"] == {"checked": 1, "gone": 0, "closed": 0}


def test_liveness_reactivated_row_survives_indeterminate_check(db, seed_company):
    # closed at the limit -> user reactivates (miss_count stays stale at 2) ->
    # next check is indeterminate. The flip keys on a gone verdict THIS run,
    # never on the stale count, so the row must stay active.
    cid = seed_company()
    key = seed_manual(db, cid, miss_count=2)  # status active = post-reactivate
    run(db, {"greenhouse": []})  # autouse stub: verdict None
    assert job_row(db, key)["status"] == "active"


def test_liveness_crash_still_stamps_last_refresh(db, seed_company, monkeypatch):
    cid = seed_company()
    seed_manual(db, cid)
    _verdict(monkeypatch, RuntimeError("boom"))
    result = run(db, {"greenhouse": []})
    assert _setting(db, "last_refresh") == result["last_refresh"]
    assert "boom" in result["manual"]["error"]


def test_liveness_skipped_on_scoped_runs(db, seed_company, monkeypatch):
    cid = seed_company()
    key = seed_manual(db, cid)
    _verdict(monkeypatch, False)
    result = run(db, {"greenhouse": []}, company_ids=[cid])
    assert job_row(db, key)["miss_count"] == 0
    assert result["manual"] == {"checked": 0, "gone": 0, "closed": 0}


def test_liveness_skips_dismissed_and_urlless_rows(db, seed_company, monkeypatch):
    cid = seed_company()
    dismissed = seed_manual(db, cid, status="dismissed", key="m-dis")
    db.execute(
        "INSERT INTO jobs (company_id, title, status, miss_count, source, dedupe_key, "
        "first_seen, last_seen) VALUES (?, 'No URL', 'active', 0, 'manual', ?, "
        "'2026-06-01', '2026-06-01')",
        (cid, f"manual:{cid}:m-nourl"),
    )
    db.commit()
    _verdict(monkeypatch, False)
    result = run(db, {"greenhouse": []})
    assert result["manual"] == {"checked": 0, "gone": 0, "closed": 0}
    assert job_row(db, dismissed)["miss_count"] == 0
    assert job_row(db, f"manual:{cid}:m-nourl")["miss_count"] == 0


def _fake_client(status: int, final_url: str):
    """Client whose GET resolves to `status` at `final_url` (redirects already
    followed, mirroring follow_redirects=True — httpx.Response.url is the final
    hop's URL)."""

    async def get(url):
        return httpx.Response(status, request=httpx.Request("GET", final_url))

    from types import SimpleNamespace

    return SimpleNamespace(get=get)


# Bound at import time — the autouse _stub_manual_liveness fixture replaces the
# module attribute, and these tests exercise the real classifier.
_REAL_CHECK_URL = refresh_mod._check_manual_url


def _check(client, url):
    return asyncio.run(_REAL_CHECK_URL(client, url))


def test_check_url_200_same_path_is_alive():
    url = "https://boards.greenhouse.io/exampleco/jobs/4100200300"
    # host hop (boards. -> job-boards.) with the path intact = still the posting
    client = _fake_client(200, "https://job-boards.greenhouse.io/exampleco/jobs/4100200300")
    assert _check(client, url) is True


def test_check_url_redirect_off_the_posting_path_is_gone():
    # greenhouse delists by 302ing the posting to the board root (?error=true)
    url = "https://job-boards.greenhouse.io/exampleco/jobs/4100200300"
    client = _fake_client(200, "https://job-boards.greenhouse.io/exampleco?error=true")
    assert _check(client, url) is False


def test_check_url_slug_canonicalization_is_alive():
    url = "https://x.example/jobs/123"
    client = _fake_client(200, "https://x.example/jobs/123-senior-designer")
    assert _check(client, url) is True


def test_check_url_hard_404_is_gone_and_403_is_indeterminate():
    url = "https://x.example/jobs/123"
    assert _check(_fake_client(404, url), url) is False
    assert _check(_fake_client(410, url), url) is False
    assert _check(_fake_client(403, url), url) is None
    assert _check(_fake_client(503, url), url) is None


def test_check_url_transport_failure_is_indeterminate(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr("jshq.ats.adapters._http._RETRY_DELAY_S", 0)

    async def get(url):
        raise httpx.ConnectError("down")

    assert _check(SimpleNamespace(get=get), "https://x.example/jobs/1") is None
