"""run_scoring orchestrator — pending predicate, tier1 short-circuit, isolation."""

import asyncio
import json
from types import SimpleNamespace

from jshq import scoring

def tier2(values: dict) -> list:
    """The model's per-criterion array from {criterion -> score|None}. Quotes are
    filled for every scored criterion so payloads never trip the evidence rules."""
    return [
        {"n": n, "v": values.get(n), "q": "" if values.get(n) is None else "quoted evidence"}
        for n in range(1, 12)
    ]


# Aggregates to exactly 85 against the live weights in DATA_DIR/fit_criteria.md, and
# criterion 5 at +2 derives craft_lean +4 — so every cap and deduction assertion
# below reads the same as it did when the model emitted 85 and lean 4 directly.
# All eleven are evidenced, so no thin_posting flag either. If the doc's weights
# or scale change, this constant is the thing to re-derive. (2026-08-08: c4's
# weight dropped 1.5 → 0.5, −2.0 weighted points; c7 moved 0 → +2 to put them
# back, keeping the total at 18.75 → 85.)
GOOD_SUBSCORES = {1: 2, 2: 1, 3: 2, 4: 2, 5: 2, 6: 1, 7: 2, 8: 0, 9: 1, 10: 0, 11: 0}

GOOD_PAYLOAD = {
    "tier2": tier2(GOOD_SUBSCORES),
    "fit_quadrant": "energizing_strength",
    "management_type": "people_leader",
    "function": "product",
    "leads_discipline": "design",
    "confidence": "high",
    "near_miss_flags": [],
    "scoring_notes": "Strong fit.",
}


def fake_client(payload=GOOD_PAYLOAD, fail_titles=(), by_title=None, sequence=None):
    """by_title maps a job title to its own payload (sibling-divergence tests).
    sequence is a list consumed one entry per CALL, whatever the title — an
    Exception entry raises, a dict entry is the payload (escalation tests);
    after it runs dry, calls fall back to by_title/payload."""
    state = {"calls": 0, "titles": [], "kwargs": None}
    sequence = list(sequence or [])

    async def create(**kwargs):
        state["calls"] += 1
        state["kwargs"] = kwargs  # last call's args — lets a test read the system prompt
        title = kwargs["messages"][0]["content"].split("\n")[0].removeprefix("Title: ")
        state["titles"].append(title)
        if title in fail_titles:
            raise RuntimeError("simulated API error")
        body = (by_title or {}).get(title, payload)
        if sequence:
            body = sequence.pop(0)
            if isinstance(body, Exception):
                raise body
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(body))])

    return SimpleNamespace(messages=SimpleNamespace(create=create)), state


def seed_job(db, cid, title="Director of Design", **overrides):
    fields = {
        "company_id": cid, "title": title, "remote_type": "remote",
        "level_band": "director", "salary_min": 200000, "salary_max": 240000,
        "salary_stated": 1, "description_text": "Mentor designers.",
        "status": "active", "dedupe_key": f"{cid}:{title}",
    }
    fields.update(overrides)
    cols = ", ".join(fields)
    marks = ", ".join("?" * len(fields))
    cur = db.execute(f"INSERT INTO jobs ({cols}) VALUES ({marks})", tuple(fields.values()))
    db.commit()
    return cur.lastrowid


def run(db, client):
    return asyncio.run(scoring.run_scoring(db, client=client))


def test_scores_pending_job_and_writes_columns(db, seed_company):
    jid = seed_job(db, seed_company())
    client, state = fake_client()
    report = run(db, client)
    assert report == {"scored": 1, "tier1_failed": 0, "errors": 0, "rate_limited": 0, "escalated": 0, "sibling_overrides": 0, "cost": 0.0}
    assert state["calls"] == 1
    row = db.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()
    assert row["fit_score"] == 85
    assert row["fit_quadrant"] == "energizing_strength"
    assert row["scoring_notes"].startswith("[tension: teach_craft]")  # derived from craft_lean 4
    assert "- Read: people_leader · product · leads design · lean +4 (high confidence)" in row["scoring_notes"]
    assert json.loads(row["tier1_results"])["comp"] == "pass"
    detail = json.loads(row["score_detail"])
    assert detail["model_score"] == 85 and detail["craft_lean"] == 4
    assert detail["deductions"] == {}


def test_already_scored_jobs_skipped(db, seed_company):
    cid = seed_company()
    seed_job(db, cid, tier1_results='{"comp": "pass"}', fit_score=70)
    client, state = fake_client()
    assert run(db, client) == {"scored": 0, "tier1_failed": 0, "errors": 0, "rate_limited": 0, "escalated": 0, "sibling_overrides": 0, "cost": 0.0}
    assert state["calls"] == 0


def test_non_active_jobs_skipped(db, seed_company):
    seed_job(db, seed_company(), status="dismissed")
    client, state = fake_client()
    assert run(db, client)["scored"] == 0
    assert state["calls"] == 0


def test_tier1_hard_fail_skips_api_call(db, seed_company):
    jid = seed_job(db, seed_company(), salary_max=120000)
    client, state = fake_client()
    report = run(db, client)
    assert report == {"scored": 0, "tier1_failed": 1, "errors": 0, "rate_limited": 0, "escalated": 0, "sibling_overrides": 0, "cost": 0.0}
    assert state["calls"] == 0
    row = db.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()
    assert row["fit_score"] == 0
    assert row["scoring_notes"] == "Tier 1 fail: comp"
    assert json.loads(row["tier1_results"])["hard_fail"] is True


def test_elevation_survives_rescore(db, seed_company):
    # A manually elevated job keeps its flag through a rescore: run_scoring
    # rewrites the fit columns (here Tier-1 hard-fail → 0) but never the flag.
    jid = seed_job(db, seed_company(), salary_max=120000)  # below comp floor → hard fail
    db.execute("UPDATE jobs SET manually_elevated = 1 WHERE id = ?", (jid,))
    db.commit()
    client, _ = fake_client()
    run(db, client)
    row = db.execute("SELECT fit_score, manually_elevated FROM jobs WHERE id = ?", (jid,)).fetchone()
    assert row["fit_score"] == 0
    assert row["manually_elevated"] == 1


def test_excluded_sector_company_fails_tier1(db, seed_company):
    cid = seed_company(sector_flags='["gambling"]')
    seed_job(db, cid)
    client, state = fake_client()
    assert run(db, client)["tier1_failed"] == 1
    assert state["calls"] == 0


def test_near_miss_flags_merged_from_tier1_and_model(db, seed_company):
    jid = seed_job(
        db, seed_company(), salary_stated=0, salary_min=None, salary_max=None
    )
    payload = dict(GOOD_PAYLOAD, near_miss_flags=["pace_unclear"])
    client, _ = fake_client(payload)
    run(db, client)
    row = db.execute("SELECT near_miss_flags FROM jobs WHERE id = ?", (jid,)).fetchone()
    assert json.loads(row["near_miss_flags"]) == ["comp_unknown", "pace_unclear"]


def test_low_confidence_derives_a_visible_flag_without_deducting(db, seed_company):
    # confidence was stored and consumed by nothing (2026-08-08); `low` now
    # rides the near-miss pipeline as a derived flag. Never a deduction — the
    # key is not in the adjustments table — so the score is untouched.
    jid = seed_job(db, seed_company())
    client, _ = fake_client(dict(GOOD_PAYLOAD, confidence="low"))
    run(db, client)
    row = db.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()
    assert json.loads(row["near_miss_flags"]) == ["low_confidence"]
    assert row["fit_score"] == 85
    detail = json.loads(row["score_detail"])
    assert detail["confidence"] == "low" and detail["deductions"] == {}


def test_medium_confidence_derives_no_flag(db, seed_company):
    jid = seed_job(db, seed_company())
    client, _ = fake_client(dict(GOOD_PAYLOAD, confidence="medium"))
    run(db, client)
    row = db.execute("SELECT near_miss_flags FROM jobs WHERE id = ?", (jid,)).fetchone()
    assert json.loads(row["near_miss_flags"]) == []


def test_one_failing_job_does_not_stop_batch(db, seed_company):
    cid = seed_company()
    seed_job(db, cid, title="Bad Job")
    good = seed_job(db, cid, title="Director of Design")
    client, _ = fake_client(fail_titles=("Bad Job",))
    report = run(db, client)
    assert report == {"scored": 1, "tier1_failed": 0, "errors": 1, "rate_limited": 0, "escalated": 0, "sibling_overrides": 0, "cost": 0.0}
    assert db.execute("SELECT fit_score FROM jobs WHERE id = ?", (good,)).fetchone()[0] == 85
    # failed job stays NULL -> retried next refresh
    bad = db.execute("SELECT tier1_results FROM jobs WHERE title = 'Bad Job'").fetchone()
    assert bad["tier1_results"] is None


def test_no_key_and_no_client_skips(db, seed_company):
    from jshq import apikey

    seed_job(db, seed_company())
    report = asyncio.run(scoring.run_scoring(db))  # autouse fixture removed the key
    assert report["skipped"] == apikey.MISSING_MESSAGE
    assert "Settings" in report["skipped"]  # the skip reason is actionable


def test_broken_criteria_doc_skips_loudly(db, seed_company, monkeypatch, tmp_path):
    from jshq.scoring import criteria as criteria_mod

    bad = tmp_path / "fit_criteria.md"
    bad.write_text("no params block")
    monkeypatch.setattr(scoring, "load_criteria", lambda: criteria_mod.load_criteria(bad))
    seed_job(db, seed_company())
    client, state = fake_client()
    report = run(db, client)
    assert report["skipped"].startswith("criteria error:")
    assert state["calls"] == 0


def test_empty_tier2_skips_before_any_call(db, seed_company, monkeypatch):
    # A blank-slate wish list (no Tier 2) must skip cleanly with an actionable
    # message, not raise inside build_system_prompt and die in the rescore task.
    from jshq import paths
    from jshq.scoring import criteria as criteria_mod

    starter = paths.DEFAULTS_DIR / "fit_criteria.starter.md"
    monkeypatch.setattr(scoring, "load_criteria", lambda: criteria_mod.load_criteria(starter))
    seed_job(db, seed_company())
    client, state = fake_client()
    report = run(db, client)
    assert report["skipped"] == scoring.NO_CRITERIA_MESSAGE
    assert "wish list" in report["skipped"].lower()  # actionable
    assert state["calls"] == 0  # no API call burned on an empty rubric


def test_blank_slate_prompt_omits_in_band_wrong_function(tmp_path):
    # With no taxonomy block the function check is neutralized: the prompt must
    # not declare an in-band discipline or a "wrong function". The Alex reference
    # (which HAS a taxonomy block) still does.
    import shutil

    from jshq import paths
    from jshq.scoring import criteria as criteria_mod, haiku

    doc = tmp_path / "fit_criteria.md"
    shutil.copy(paths.DEFAULTS_DIR / "fit_criteria.starter.md", doc)
    crit = criteria_mod.write_criteria(
        criteria_mod.load_criteria(doc).params,
        [{"text": "Sustainable pace", "weight": 1.0}],
        path=doc,
    )
    assert crit.taxonomy_is_default is True
    prompt = haiku.build_system_prompt(crit, "")
    assert "wrong function" not in prompt.lower()
    assert "This search is for" not in prompt

    alex = criteria_mod.load_criteria(paths.DEFAULTS_DIR / "fit_criteria.md")
    assert alex.taxonomy_is_default is False
    assert "wrong function" in haiku.build_system_prompt(alex, "").lower()


def _usage_client(usage):
    async def create(**kwargs):
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(GOOD_PAYLOAD))],
            usage=usage,
        )

    return SimpleNamespace(messages=SimpleNamespace(create=create))


def test_records_usage_and_reports_cost(db, seed_company):
    from jshq import usage as usage_mod

    seed_job(db, seed_company())
    client = _usage_client(
        SimpleNamespace(
            input_tokens=1000, output_tokens=200,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        )
    )
    report = run(db, client)
    assert report["scored"] == 1
    assert report["cost"] > 0  # 1000·$1/M + 200·$5/M = $0.002
    hk = usage_mod.read_usage_totals(db)["by_model"]["claude-haiku-4-5"]
    assert hk["calls"] == 1 and hk["input"] == 1000 and hk["cost"] > 0


def test_bills_tokens_for_a_job_that_fails_parsing(db, seed_company):
    # A job whose model output fails parsing on both attempts still made two
    # billable calls; their tokens must reach the ledger. They used to be dropped
    # (score_one returned [] usages and the accumulator skipped the error branch),
    # so the cost total silently under-reported the failed-parse subset.
    from jshq import usage as usage_mod

    seed_job(db, seed_company())
    bad = dict(GOOD_PAYLOAD, tier2=[])  # wrong length -> _parse_tier2 raises both attempts

    async def create(**kwargs):
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(bad))],
            usage=SimpleNamespace(
                input_tokens=500, output_tokens=100,
                cache_read_input_tokens=0, cache_creation_input_tokens=0,
            ),
        )

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    report = run(db, client)
    assert report["scored"] == 0 and report["errors"] == 1
    hk = usage_mod.read_usage_totals(db)["by_model"]["claude-haiku-4-5"]
    assert hk["calls"] == 2 and hk["input"] == 1000  # both failed attempts billed


def test_rate_limit_counted_separately(db, seed_company):
    # _is_rate_limit keys on the exception class name, so a locally-named
    # RateLimitError stands in for the SDK's without constructing a real one.
    class RateLimitError(Exception):
        pass

    seed_job(db, seed_company())

    async def create(**kwargs):
        raise RateLimitError("429")

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    report = run(db, client)
    assert report["scored"] == 0
    assert report["errors"] == 1
    assert report["rate_limited"] == 1


def test_estimate_rescore_counts_without_ai(db, seed_company):
    cid = seed_company()
    seed_job(db, cid, title="Pass Me")  # passes Tier 1
    seed_job(db, cid, title="Fail Me", salary_max=120000)  # hard comp fail
    est = scoring.estimate_rescore(db)
    assert est == {"active": 2, "to_score": 1, "tier1_failed": 1}


# --- scoring redesign: deduction table, score_detail, boilerplate stripping ---


def _criteria_with_adjustments(monkeypatch, adjustments):
    """Real doc's params/prose with a CONTROLLED deduction table — tests must
    not couple to the live doc's tunable values (the criteria-destriction
    lesson)."""
    import dataclasses

    from jshq.scoring.criteria import load_criteria

    fixed = dataclasses.replace(load_criteria(), adjustments=adjustments)
    monkeypatch.setattr(scoring, "load_criteria", lambda: fixed)


def test_deduction_table_applied_end_to_end(db, seed_company, monkeypatch):
    _criteria_with_adjustments(monkeypatch, {"scope_gap": 8, "pace_unclear": 4})
    jid = seed_job(db, seed_company())
    payload = dict(GOOD_PAYLOAD, near_miss_flags=["scope_gap", "pace_unclear", "novel_flag"])
    client, _ = fake_client(payload)
    run(db, client)
    row = db.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()
    assert row["fit_score"] == 85 - 8 - 4  # unknown flag deducts nothing
    detail = json.loads(row["score_detail"])
    assert detail["model_score"] == 85
    assert detail["deductions"] == {"scope_gap": 8, "pace_unclear": 4}
    assert "- Adjustments: 85 - 4 pace_unclear - 8 scope_gap → 73" in row["scoring_notes"]


def test_deduction_clamps_at_zero(db, seed_company, monkeypatch):
    # aggregate() floors at 1 so an AI-scored job never collides with the
    # Tier-1 hard-fail sentinel — but a deduction can still drive it to 0.
    _criteria_with_adjustments(monkeypatch, {"scope_gap": 25})
    jid = seed_job(db, seed_company())
    payload = dict(
        GOOD_PAYLOAD,
        tier2=tier2({n: -2 for n in range(1, 12)}),  # aggregates to 10
        near_miss_flags=["scope_gap"],
    )
    client, _ = fake_client(payload)
    run(db, client)
    assert db.execute("SELECT fit_score FROM jobs WHERE id = ?", (jid,)).fetchone()[0] == 0


def test_hard_fail_clears_stale_score_detail(db, seed_company):
    jid = seed_job(db, seed_company(), salary_max=120000, score_detail='{"model_score": 72}')
    db.execute("UPDATE jobs SET tier1_results = NULL WHERE id = ?", (jid,))
    db.commit()
    client, _ = fake_client()
    run(db, client)
    row = db.execute("SELECT fit_score, score_detail FROM jobs WHERE id = ?", (jid,)).fetchone()
    assert row["fit_score"] == 0 and row["score_detail"] is None


def test_shared_boilerplate_stripped_from_prompt(db, seed_company):
    from jshq.scoring.boilerplate import MARKER

    cid = seed_company()
    shared = (
        "At TestCo, our design competencies are Human-Centered, Business-Focused, "
        "Problem-Solving, Collaboration and Communication — we change banking for good "
        "and celebrate an inclusive culture across every team, location and level."
    )
    for n in range(3):
        unique = f"Role {n}: " + f"lead squad {n} through discovery and delivery. " * 25
        seed_job(db, cid, title=f"Director {n}", description_text=f"{unique}\n\n{shared}")

    captured = []

    async def create(**kwargs):
        captured.append(kwargs["messages"][0]["content"])
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(GOOD_PAYLOAD))]
        )

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    report = asyncio.run(scoring.run_scoring(db, client=client))
    assert report["scored"] == 3
    for msg in captured:
        assert "change banking for good" not in msg  # shared block stripped
        assert MARKER in msg
        assert "through discovery and delivery" in msg  # role text kept
    # the DB text is untouched — stripping is prompt-time only
    kept = db.execute("SELECT description_text FROM jobs WHERE company_id = ?", (cid,)).fetchall()
    assert all("change banking for good" in r[0] for r in kept)


# --- status population (2026-08-08) ----------------------------------------


def test_rescore_skips_non_active_by_default(db, seed_company):
    """The refresh pipeline must never spend calls on jobs that are done with."""
    cid = seed_company()
    seed_job(db, cid, title="Active Director")
    seed_job(db, cid, title="Applied Director", status="applied")
    seed_job(db, cid, title="Dismissed Director", status="dismissed")
    client, state = fake_client()
    report = asyncio.run(scoring.run_scoring(db, only_pending=False, client=client))
    assert report["scored"] == 1
    assert state["titles"] == ["Active Director"]


def test_rescore_reaches_named_statuses(db, seed_company):
    """Widening the population is how applied/dismissed rows get off the old
    rubric — and it must not disturb their status or their elevation flag."""
    cid = seed_company()
    seed_job(db, cid, title="Active Director")
    jid = seed_job(db, cid, title="Applied Director", status="applied")
    db.execute("UPDATE jobs SET manually_elevated = 1 WHERE id = ?", (jid,))
    db.commit()
    client, state = fake_client()
    report = asyncio.run(scoring.run_scoring(
        db, only_pending=False, client=client, statuses=("applied", "dismissed")
    ))
    assert report["scored"] == 1
    assert state["titles"] == ["Applied Director"]
    row = db.execute(
        "SELECT status, manually_elevated, fit_score FROM jobs WHERE id = ?", (jid,)
    ).fetchone()
    assert row["status"] == "applied" and row["manually_elevated"] == 1
    assert row["fit_score"] > 0


def test_only_scored_skips_rows_that_never_had_a_score(db, seed_company):
    """Widening --statuses alone also picks up rows dismissed before they were
    ever scored; --only-scored is what makes it a rubric migration, not a bill."""
    cid = seed_company()
    stale = seed_job(db, cid, title="Stale Director", status="dismissed",
                     score_detail='{"model_score": 82}')
    seed_job(db, cid, title="Never Scored", status="dismissed")
    db.commit()
    client, state = fake_client()
    report = asyncio.run(scoring.run_scoring(
        db, only_pending=False, client=client, statuses=("dismissed",), only_scored=True
    ))
    assert report["scored"] == 1
    assert state["titles"] == ["Stale Director"]
    assert json.loads(
        db.execute("SELECT score_detail FROM jobs WHERE id = ?", (stale,)).fetchone()[0]
    )["model_score"] != 82  # rewritten by the new rubric


def test_statuses_are_bound_not_interpolated(db, seed_company):
    """These values reach a WHERE clause; a quote in one must not be SQL."""
    seed_job(db, seed_company(), title="Active Director")
    client, _ = fake_client()
    report = asyncio.run(scoring.run_scoring(
        db, only_pending=False, client=client, statuses=("active'; DROP TABLE jobs; --",)
    ))
    assert report["scored"] == 0
    assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1


def test_job_ids_scope_reaches_a_non_active_pending_job(db, seed_company):
    """The details-edit rescore (2026-08-10): the edit NULLs the fit
    columns of a job in ANY status, so its rescore must pass job_ids + the
    row's own status or a non-active job is silently skipped and stays
    unscored forever. Scoping by id is the other half of the safety: widening
    statuses alone would sweep in every OTHER pending row of that status (the
    never-scored dismissed/applied rows a backfill deliberately skips)."""
    cid = seed_company()
    edited = seed_job(db, cid, title="Edited Applied Director", status="applied")
    seed_job(db, cid, title="Other Pending Applied", status="applied")
    client, state = fake_client()
    report = asyncio.run(scoring.run_scoring(
        db, client=client, statuses=("applied",), job_ids=(edited,)
    ))
    assert report["scored"] == 1
    assert state["titles"] == ["Edited Applied Director"]
    assert db.execute(
        "SELECT fit_score FROM jobs WHERE id = ?", (edited,)
    ).fetchone()[0] > 0


def test_job_ids_are_bound_not_interpolated(db, seed_company):
    """job_ids reach the same WHERE clause as statuses; same discipline."""
    seed_job(db, seed_company(), title="Active Director")
    client, _ = fake_client()
    report = asyncio.run(scoring.run_scoring(
        db, only_pending=False, client=client, job_ids=("1'; DROP TABLE jobs; --",)
    ))
    assert report["scored"] == 0
    assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1


# --- shared prompt-input construction (2026-08-08) -------------------------
#
# build_prompt_inputs / prompt_job exist so the pipeline and
# scripts/score_distribution.py cannot send the model different things. The
# harness used to re-implement this and drifted: it measured an empty dismissal
# digest, no learned rules, and unstripped JDs, then that measurement was used
# to approve a rescore it did not cover. These tests pin the two halves — that
# the helper carries every input, and that run_scoring uses THE SAME helper.


def _prompt_inputs(db, jobs):
    from jshq.scoring.criteria import load_criteria

    return scoring.build_prompt_inputs(db, load_criteria(), jobs)


def test_build_prompt_inputs_carries_digest_and_learned_rules(db, seed_company):
    from jshq.scoring import learned

    db.execute(
        "INSERT INTO activities (entity_type, entity_id, type, content) VALUES (?,?,?,?)",
        ("job", 1, "dismissal", json.dumps({"title": "Head of Slides", "reason": "wrong level"})),
    )
    learned.write_scoring_rules(db, [{"id": "r1", "text": "Down-rank pure ML roles."}])
    db.commit()
    system, _ = _prompt_inputs(db, [])
    assert "Head of Slides" in system  # dismissal digest
    assert "Down-rank pure ML roles." in system  # learned rules


def test_run_scoring_sends_exactly_build_prompt_inputs_prompt(db, seed_company):
    """The regression guard on the extraction: a second construction of the
    system prompt anywhere is a measurement that proves nothing."""
    from jshq.scoring import learned

    cid = seed_company()
    db.execute(
        "INSERT INTO activities (entity_type, entity_id, type, content) VALUES (?,?,?,?)",
        ("job", 1, "dismissal", json.dumps({"title": "Head of Slides", "reason": "wrong level"})),
    )
    learned.write_scoring_rules(db, [{"id": "r1", "text": "Down-rank pure ML roles."}])
    jid = seed_job(db, cid)
    client, state = fake_client()
    run(db, client)
    assert state["calls"] == 1
    job = db.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()
    expected, _ = _prompt_inputs(db, [job])
    assert state["kwargs"]["system"][0]["text"] == expected


def test_prompt_job_strips_shared_boilerplate_without_mutating_the_row(db, seed_company):
    from jshq.scoring.boilerplate import MARKER

    cid = seed_company()
    shared = (
        "At TestCo, our design competencies are Human-Centered, Business-Focused, "
        "Problem-Solving, Collaboration and Communication — we change banking for good "
        "and celebrate an inclusive culture across every team, location and level."
    )
    for n in range(3):
        unique = f"Role {n}: " + f"lead squad {n} through discovery and delivery. " * 25
        seed_job(db, cid, title=f"Director {n}", description_text=f"{unique}\n\n{shared}")
    jobs = db.execute("SELECT * FROM jobs WHERE company_id = ?", (cid,)).fetchall()

    _, fingerprints = _prompt_inputs(db, jobs)
    assert set(fingerprints) == {cid}

    stripped = scoring.prompt_job(jobs[0], fingerprints)
    assert "change banking for good" not in stripped["description_text"]
    assert MARKER in stripped["description_text"]
    # the source row and the DB text are both untouched — prompt-time only
    assert "change banking for good" in jobs[0]["description_text"]
    kept = db.execute("SELECT description_text FROM jobs WHERE company_id = ?", (cid,)).fetchall()
    assert all("change banking for good" in r[0] for r in kept)


def test_prompt_job_is_a_noop_below_min_siblings(db, seed_company):
    """Two postings are not boilerplate. The fingerprint map is empty, and
    prompt_job must hand the model the full JD rather than an empty string."""
    cid = seed_company()
    shared = "Shared culture blurb. " * 20
    for n in range(2):
        seed_job(db, cid, title=f"Director {n}", description_text=f"Role {n}.\n\n{shared}")
    jobs = db.execute("SELECT * FROM jobs WHERE company_id = ?", (cid,)).fetchall()
    _, fingerprints = _prompt_inputs(db, jobs)
    assert fingerprints == {}
    assert scoring.prompt_job(jobs[0], fingerprints)["description_text"] == jobs[0][
        "description_text"
    ]


# --- IC hard cap (2026-07): categorical designation + score_caps ceiling ---


def _criteria_with(monkeypatch, adjustments=None, caps=None):
    """Real doc's params/prose with CONTROLLED adjustments AND caps — tests
    must not couple to the live doc's tunable values."""
    import dataclasses

    from jshq.scoring.criteria import load_criteria

    fixed = dataclasses.replace(
        load_criteria(),
        adjustments=adjustments if adjustments is not None else {},
        caps=caps if caps is not None else {},
    )
    monkeypatch.setattr(scoring, "load_criteria", lambda: fixed)


def test_is_ic_designated():
    assert scoring.is_ic_designated("Senior Product Designer", "ic")
    assert scoring.is_ic_designated(
        "Product Design Director (Individual Contributor)", "director"
    )
    assert scoring.is_ic_designated("Individual-Contributor Design Track", "manager")
    assert not scoring.is_ic_designated("Director of Design", "director")
    assert not scoring.is_ic_designated(None, "director")
    assert not scoring.is_ic_designated("Head of Design", None)


def test_ic_band_overrides_model_read_and_caps(db, seed_company, monkeypatch):
    # The craft-heavy-IC defect end-to-end: ic-banded title, model misreads
    # people_leader at 85 -> forced ic, capped, both reads recorded.
    _criteria_with(monkeypatch, caps={"ic": 55})
    jid = seed_job(db, seed_company(), title="Senior / Staff Product Designer",
                   level_band="ic")
    client, _ = fake_client()  # GOOD_PAYLOAD: people_leader, 85
    run(db, client)
    row = db.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()
    assert row["fit_score"] == 55
    detail = json.loads(row["score_detail"])
    assert detail["model_score"] == 85
    assert detail["management_type"] == "ic"
    assert detail["model_management_type"] == "people_leader"
    assert detail["cap"] == 55
    assert "- Read: ic (IC-designated; model read people_leader) · product" in row["scoring_notes"]
    assert "- IC cap: 85 → 55" in row["scoring_notes"]


def test_ic_title_phrase_overrides_stale_director_band(db, seed_company, monkeypatch):
    # The stale-band case: stored band still 'director' (pre-backfill), but the
    # title's explicit IC phrase designates it — the cap must not wait for a
    # band refresh.
    _criteria_with(monkeypatch, caps={"ic": 55})
    jid = seed_job(db, seed_company(),
                   title="Product Design Director (Individual Contributor)",
                   level_band="director")
    client, _ = fake_client()
    run(db, client)
    row = db.execute("SELECT fit_score, score_detail FROM jobs WHERE id = ?", (jid,)).fetchone()
    assert row["fit_score"] == 55
    assert json.loads(row["score_detail"])["management_type"] == "ic"


def test_cap_keys_on_final_management_type(db, seed_company, monkeypatch):
    # A non-IC-designated title whose JD the model correctly reads as ic
    # (a "Lead"-titled case seen live) still caps — the ceiling keys on the final read.
    _criteria_with(monkeypatch, caps={"ic": 55})
    jid = seed_job(db, seed_company(), title="Lead Product Designer",
                   level_band="manager")
    payload = dict(GOOD_PAYLOAD, management_type="ic")
    client, _ = fake_client(payload)
    run(db, client)
    row = db.execute("SELECT fit_score, score_detail, scoring_notes FROM jobs WHERE id = ?",
                     (jid,)).fetchone()
    assert row["fit_score"] == 55
    detail = json.loads(row["score_detail"])
    assert detail["management_type"] == "ic"
    assert "model_management_type" not in detail  # read not overridden, just capped
    assert "- Read: ic · product" in row["scoring_notes"]


def test_cap_applies_before_deductions(db, seed_company, monkeypatch):
    # cap-then-deduct: every named flag stays visible in the arithmetic
    # instead of being absorbed by the ceiling.
    _criteria_with(monkeypatch, adjustments={"scope_gap": 8}, caps={"ic": 55})
    jid = seed_job(db, seed_company(), title="Staff Designer", level_band="ic")
    payload = dict(GOOD_PAYLOAD, near_miss_flags=["scope_gap"])
    client, _ = fake_client(payload)
    run(db, client)
    row = db.execute("SELECT fit_score, scoring_notes, score_detail FROM jobs WHERE id = ?",
                     (jid,)).fetchone()
    assert row["fit_score"] == 55 - 8
    assert "- IC cap: 85 → 55" in row["scoring_notes"]
    assert "- Adjustments: 55 - 8 scope_gap → 47" in row["scoring_notes"]
    assert json.loads(row["score_detail"])["deductions"] == {"scope_gap": 8}


def test_people_leader_never_capped(db, seed_company, monkeypatch):
    # Uncapped, unoverridden output stays byte-identical to the pre-cap format.
    _criteria_with(monkeypatch, caps={"ic": 55})
    jid = seed_job(db, seed_company())  # director band, people_leader read
    client, _ = fake_client()
    run(db, client)
    row = db.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()
    assert row["fit_score"] == 85
    detail = json.loads(row["score_detail"])
    assert "cap" not in detail and "model_management_type" not in detail
    assert "- Read: people_leader · product · leads design · lean +4 (high confidence)" in row["scoring_notes"]
    assert "IC cap" not in row["scoring_notes"]


def test_score_under_cap_writes_no_cap_keys(db, seed_company, monkeypatch):
    # A capped-type job already scoring below the ceiling records nothing —
    # the cap never engaged.
    _criteria_with(monkeypatch, caps={"ic": 55})
    jid = seed_job(db, seed_company(), title="Product Designer", level_band="ic")
    # Neutral everywhere but mildly convert-leaning on criterion 5 -> 51, under
    # the 55 ceiling on its own merits.
    payload = dict(
        GOOD_PAYLOAD,
        tier2=tier2({**{n: 0 for n in range(1, 12)}, 5: -1}),
        management_type="ic",
    )
    client, _ = fake_client(payload)
    run(db, client)
    row = db.execute("SELECT fit_score, score_detail, scoring_notes FROM jobs WHERE id = ?",
                     (jid,)).fetchone()
    assert row["fit_score"] == 51
    assert "cap" not in json.loads(row["score_detail"])
    assert "IC cap" not in row["scoring_notes"]


def test_empty_caps_table_disables_capping(db, seed_company, monkeypatch):
    _criteria_with(monkeypatch, caps={})
    jid = seed_job(db, seed_company(), title="Staff Product Designer", level_band="ic")
    client, _ = fake_client()
    run(db, client)
    row = db.execute("SELECT fit_score, score_detail FROM jobs WHERE id = ?", (jid,)).fetchone()
    assert row["fit_score"] == 85  # override still recorded, but no ceiling
    assert json.loads(row["score_detail"])["management_type"] == "ic"


# --- junior band cap (2026-08): score_caps keyed on the level band ---


def test_junior_band_caps(db, seed_company, monkeypatch):
    # An intern title caps on its band even when the model reads generously —
    # the ceiling keys on derive_level_band's deterministic read.
    _criteria_with(monkeypatch, caps={"ic": 55, "junior": 25})
    jid = seed_job(db, seed_company(), title="Product Design Intern",
                   level_band="junior")
    client, _ = fake_client()  # GOOD_PAYLOAD: people_leader, 85
    run(db, client)
    row = db.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()
    assert row["fit_score"] == 25
    detail = json.loads(row["score_detail"])
    assert detail["band_cap"] == 25
    assert "cap" not in detail and "function_cap" not in detail
    assert "- Junior band cap: 85 → 25" in row["scoring_notes"]


def test_junior_band_cap_beats_higher_ic_cap(db, seed_company, monkeypatch):
    # junior band + ic management read: lowest ceiling wins, attribution goes
    # to the band cap that actually bound.
    _criteria_with(monkeypatch, caps={"ic": 55, "junior": 25})
    jid = seed_job(db, seed_company(), title="Junior Product Designer",
                   level_band="junior")
    payload = dict(GOOD_PAYLOAD, management_type="ic")
    client, _ = fake_client(payload)
    run(db, client)
    row = db.execute("SELECT fit_score, score_detail, scoring_notes FROM jobs WHERE id = ?",
                     (jid,)).fetchone()
    assert row["fit_score"] == 25
    assert json.loads(row["score_detail"])["band_cap"] == 25
    assert "- Junior band cap: 85 → 25" in row["scoring_notes"]
    assert "IC cap" not in row["scoring_notes"]


# --- Function check (2026-07): leads_discipline -> wrong_function / function_unclear ---


TOY_TIER2 = [{"text": f"c{n}", "weight": w} for n, w in enumerate([2.0, 1.0, 0.5], 1)]
TOY_SCALE = {"slope": 10.0, "intercept": 50.0, "silence": {1: -1.0}}


def test_aggregate_arithmetic_and_evidenced_count():
    # 10 * (2*2 + 1*1 + 0.5*0) + 50 = 100
    assert scoring.aggregate({1: 2, 2: 1, 3: 0}, TOY_TIER2, TOY_SCALE) == (100, 3)
    # all explicitly zero lands on the intercept
    assert scoring.aggregate({1: 0, 2: 0, 3: 0}, TOY_TIER2, TOY_SCALE) == (50, 3)
    # a null takes the declared silence value (c1: -1) and is NOT counted as
    # evidenced; a null with no declared value contributes 0
    assert scoring.aggregate({1: None, 2: 0, 3: None}, TOY_TIER2, TOY_SCALE) == (30, 1)


def test_aggregate_floors_at_one_never_zero():
    # 0 is the Tier-1 hard-fail sentinel (isHardFailFit / active_job_count), so
    # an AI-scored job must never land there however bad its sub-scores are.
    assert scoring.aggregate({1: -2, 2: -2, 3: -2}, TOY_TIER2, TOY_SCALE)[0] == 1
    assert scoring.aggregate({1: 2, 2: 2, 3: 2}, TOY_TIER2, TOY_SCALE)[0] == 100


def test_aggregate_silence_accepts_json_string_keys():
    # The doc block parses to int keys, but a scale round-tripped through JSON
    # carries strings — both must resolve or silence would silently vanish.
    scale = {"slope": 10.0, "intercept": 50.0, "silence": {"1": -1.0}}
    # c1 null -> -1.0 * w2.0 = -2.0 raw -> 10*(-2)+50 = 30. Were the string key
    # to miss, silence would read 0 and this would be the bare intercept, 50.
    assert scoring.aggregate({1: None, 2: 0, 3: 0}, TOY_TIER2, scale) == (30, 2)


def test_aggregate_matches_the_live_doc_anchor_points():
    """The three points the scale was fitted to. If these move, the doc's slope
    or intercept changed and the whole distribution shifted with them."""
    from jshq.scoring.criteria import load_criteria

    c = load_criteria()
    n = len(c.tier2)
    # every criterion unevidenced (criterion 5 can't be null, so it sits at 0;
    # c4's silence is 0, so its 2026-08-08 weight change leaves this anchor put)
    all_silent = {i: (0 if i == 5 else None) for i in range(1, n + 1)}
    assert scoring.aggregate(all_silent, c.tier2, c.scale)[0] == 47
    # every criterion explicitly balanced — evidence of balance beats absence
    assert scoring.aggregate({i: 0 for i in range(1, n + 1)}, c.tier2, c.scale)[0] == 55
    # Σw = 13.5 since c4 went to 0.5: ±2 everywhere is 55 ± 1.6·27 → 98 / 12
    # (was 100 clamped / 9 at Σw = 14.5)
    assert scoring.aggregate({i: 2 for i in range(1, n + 1)}, c.tier2, c.scale)[0] == 98
    assert scoring.aggregate({i: -2 for i in range(1, n + 1)}, c.tier2, c.scale)[0] == 12


def test_derive_craft_lean_doubles_the_craft_criterion():
    # {-2..+2} -> {-4..+4} keeps _tension_label's ±2 thresholds working, so the
    # "[tension: x] " prefix the frontend parses is unchanged.
    assert scoring.derive_craft_lean({5: 2}, 5) == 4
    assert scoring.derive_craft_lean({5: -2}, 5) == -4
    assert scoring.derive_craft_lean({5: 0}, 5) == 0
    assert scoring.derive_craft_lean({}, 5) == 0  # defensive: parser forbids null
    # The axis follows the doc's [craft] marker, not a fixed position.
    assert scoring.derive_craft_lean({3: 2, 5: -2}, 3) == 4


def test_derive_craft_lean_is_zero_when_the_doc_declares_no_craft_axis():
    # A rubric need not have a craft/convert axis. The lean is then 0, which
    # _tension_label reads as "mixed", so the frontend's prefix still renders.
    assert scoring.derive_craft_lean({5: 2}, None) == 0
    assert scoring._tension_label(scoring.derive_craft_lean({5: 2}, None)) == "mixed"


def test_score_detail_carries_subscores_quotes_and_count(db, seed_company):
    jid = seed_job(db, seed_company())
    client, _ = fake_client()
    run(db, client)
    detail = json.loads(
        db.execute("SELECT score_detail FROM jobs WHERE id = ?", (jid,)).fetchone()[0]
    )
    # str keys: score_detail round-trips through JSON, which has no int keys
    assert detail["subscores"] == {str(n): v for n, v in GOOD_SUBSCORES.items()}
    assert detail["evidenced_count"] == 11
    assert detail["subscore_quotes"]["5"] == "quoted evidence"


def test_thin_posting_flagged_not_deducted(db, seed_company, monkeypatch):
    # A posting evidencing almost nothing gets marked, not penalized twice —
    # the silence values already priced the absence.
    _criteria_with(monkeypatch, adjustments={"scope_gap": 8})
    jid = seed_job(db, seed_company())
    thin = {n: (2 if n == 5 else None) for n in range(1, 12)}
    client, _ = fake_client(dict(GOOD_PAYLOAD, tier2=tier2(thin)))
    run(db, client)
    row = db.execute("SELECT near_miss_flags, score_detail FROM jobs WHERE id = ?",
                     (jid,)).fetchone()
    detail = json.loads(row["score_detail"])
    assert "thin_posting" in json.loads(row["near_miss_flags"])
    assert detail["evidenced_count"] == 1
    assert detail["deductions"] == {}


def test_live_doc_gives_thin_posting_no_points():
    # The flag is a filter and a monitoring signal, not a penalty. If it ever
    # gains points in the doc, the absence is being charged for twice.
    from jshq.scoring.criteria import load_criteria

    assert not load_criteria().adjustments.get("thin_posting")


def test_well_evidenced_posting_not_flagged_thin(db, seed_company):
    jid = seed_job(db, seed_company())
    client, _ = fake_client()  # GOOD_PAYLOAD evidences all eleven
    run(db, client)
    flags = json.loads(
        db.execute("SELECT near_miss_flags FROM jobs WHERE id = ?", (jid,)).fetchone()[0]
    )
    assert "thin_posting" not in flags


def test_one_notch_down_costs_in_proportion_to_weight(db, seed_company):
    """The defect this redesign exists to fix: seven postings differing on ranked
    criteria collided on 82, tied on every emitted field, because the score was a
    habitual integer rather than a function of the criteria.

    Perfect injectivity is impossible — 6^11 sub-score vectors onto 92 integers —
    so the guarantee is not "no two postings ever share a score". It is that the
    score is a monotone function of the vector: dropping a notch always costs,
    and dropping a heavier criterion never costs less than a lighter one.
    """
    from jshq.scoring.criteria import load_criteria

    weights = [item["weight"] for item in load_criteria().tier2]
    cid = seed_company()
    scored = []
    # criterion 11 is excluded: it is floored at 0, so a notch down is a no-op
    # by design (the doc's "neutral when absent, never negative").
    for n in range(1, 11):
        variant = dict(GOOD_SUBSCORES)
        variant[n] -= 1
        jid = seed_job(db, cid, title=f"Director of Design {n}")
        client, _ = fake_client(dict(GOOD_PAYLOAD, tier2=tier2(variant)))
        run(db, client)
        score = db.execute(
            "SELECT fit_score FROM jobs WHERE id = ?", (jid,)
        ).fetchone()[0]
        scored.append((weights[n - 1], score))

    assert all(s < 85 for _, s in scored), f"a notch down cost nothing: {scored}"
    # heavier criterion -> weakly lower score
    ordered = [s for _, s in sorted(scored, key=lambda ws: -ws[0])]
    assert ordered == sorted(ordered), f"not monotone in weight: {scored}"
    # and the costs genuinely spread rather than collapsing onto one value
    assert len({s for _, s in scored}) >= 4, f"single-notch costs collapsed: {scored}"


def test_content_leadership_is_wrong_function(db, seed_company, monkeypatch):
    """Two content-director roles scored 79-83 because DISCIPLINES had no
    "content" token and the nearest lexical match was "design" — while the doc
    says content leadership is out of band. The enum now names it; the
    fall-through in function_check_flag needs no change."""
    _criteria_with(monkeypatch, caps={"wrong_function": 20})
    jid = seed_job(db, seed_company(), title="Senior Director, Content Design")
    client, _ = fake_client(dict(GOOD_PAYLOAD, leads_discipline="content"))
    run(db, client)
    row = db.execute("SELECT fit_score, near_miss_flags, score_detail FROM jobs WHERE id = ?",
                     (jid,)).fetchone()
    assert "wrong_function" in json.loads(row["near_miss_flags"])
    assert row["fit_score"] == 20
    assert json.loads(row["score_detail"])["function_cap"] == 20


def test_research_leadership_stays_in_band(db, seed_company, monkeypatch):
    """The counterpart guard: researchers are deliberately NOT in the doc's
    exclusion list, so a research team under a design leader must keep reading
    design. Giving "research" its own discipline token would hard-cap all of
    them at 20 — the shipped taxonomy must not have one."""
    from jshq.scoring.criteria import CRITERIA_PATH, load_criteria

    assert "research" not in load_criteria(CRITERIA_PATH).taxonomy["disciplines"]
    _criteria_with(monkeypatch, caps={"wrong_function": 20})
    jid = seed_job(db, seed_company(), title="Director, Design Research")
    client, _ = fake_client()  # GOOD_PAYLOAD reads leads_discipline "design"
    run(db, client)
    row = db.execute("SELECT fit_score, near_miss_flags FROM jobs WHERE id = ?",
                     (jid,)).fetchone()
    assert "wrong_function" not in json.loads(row["near_miss_flags"])
    assert row["fit_score"] == 85


def test_function_check_follows_the_docs_in_band_disciplines():
    """The point of the taxonomy block: an engineering-leadership search must be
    expressible without touching code. Before Phase 2 the pass condition was the
    literal string "design", so every job a non-design user saw was hard-capped."""
    eng = frozenset({"engineering"})
    assert scoring.function_check_flag("engineering", "people_leader", eng) is None
    assert scoring.function_check_flag("design", "people_leader", eng) == "wrong_function"
    # more than one in-band discipline is legal (e.g. design + research)
    both = frozenset({"design", "research"})
    assert scoring.function_check_flag("research", "people_leader", both) is None
    assert scoring.function_check_flag("design", "people_leader", both) is None
    # unclear still never passes, whatever the search is for
    assert scoring.function_check_flag("unclear", "people_leader", eng) == "function_unclear"


def test_function_check_flag():
    assert scoring.function_check_flag("content", "people_leader") == "wrong_function"
    assert scoring.function_check_flag("design", "people_leader") is None
    assert scoring.function_check_flag("product", "people_leader") == "wrong_function"


def test_function_check_flag_neutralized_on_blank_slate():
    # No taxonomy block yet ⇒ the user has not declared their field, so nothing is
    # the "wrong function"; the cap is skipped until a taxonomy is set.
    assert scoring.function_check_flag("product", "people_leader", taxonomy_is_default=True) is None
    assert scoring.function_check_flag("unclear", "people_leader", taxonomy_is_default=True) is None
    # A configured taxonomy (the default False) still caps as before.
    assert scoring.function_check_flag("product", "people_leader") == "wrong_function"
    assert scoring.function_check_flag("engineering", "unclear") == "wrong_function"
    assert scoring.function_check_flag("other", "people_leader") == "wrong_function"
    assert scoring.function_check_flag("unclear", "people_leader") == "function_unclear"
    assert scoring.function_check_flag("unclear", "unclear") == "function_unclear"
    # an ic seat has no led discipline — the IC cap owns it, no noise flag...
    assert scoring.function_check_flag("unclear", "ic") is None
    # ...but an IC seat in the wrong discipline still flags
    assert scoring.function_check_flag("product", "ic") == "wrong_function"


def test_wrong_function_flags_and_caps(db, seed_company, monkeypatch):
    # The wrong-function defect end-to-end: Director-band PM role, model reads
    # people_leader at 85 with leads product -> hard flag + cap, both
    # inspectable in score_detail.
    _criteria_with(monkeypatch, caps={"ic": 55, "wrong_function": 20})
    jid = seed_job(db, seed_company(), title="Director of Product")
    payload = dict(GOOD_PAYLOAD, leads_discipline="product")
    client, _ = fake_client(payload)
    run(db, client)
    row = db.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()
    assert row["fit_score"] == 20
    assert "wrong_function" in json.loads(row["near_miss_flags"])
    detail = json.loads(row["score_detail"])
    assert detail["model_score"] == 85
    assert detail["leads_discipline"] == "product"
    assert detail["function_cap"] == 20
    assert "cap" not in detail
    assert "- Read: people_leader · product · leads product" in row["scoring_notes"]
    assert "- Wrong function (leads product): 85 → 20" in row["scoring_notes"]


def test_unclear_discipline_flags_for_review_and_caps(db, seed_company, monkeypatch):
    _criteria_with(monkeypatch, caps={"function_unclear": 55})
    jid = seed_job(db, seed_company())
    payload = dict(GOOD_PAYLOAD, leads_discipline="unclear")
    client, _ = fake_client(payload)
    run(db, client)
    row = db.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()
    assert row["fit_score"] == 55
    assert "function_unclear" in json.loads(row["near_miss_flags"])
    assert "- Function unclear: 85 → 55" in row["scoring_notes"]
    assert json.loads(row["score_detail"])["function_cap"] == 55


def test_design_discipline_leaves_arithmetic_unchanged(db, seed_company, monkeypatch):
    _criteria_with(monkeypatch, caps={"ic": 55, "wrong_function": 20, "function_unclear": 55})
    jid = seed_job(db, seed_company())  # GOOD_PAYLOAD leads design
    client, _ = fake_client()
    run(db, client)
    row = db.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()
    assert row["fit_score"] == 85
    assert json.loads(row["near_miss_flags"]) == []
    detail = json.loads(row["score_detail"])
    assert detail["leads_discipline"] == "design"
    assert "function_cap" not in detail and "cap" not in detail
    assert "Wrong function" not in row["scoring_notes"]


def test_lowest_of_mgmt_and_function_caps_wins(db, seed_company, monkeypatch):
    # ic-designated AND wrong-function: the lower ceiling binds (here the
    # function cap), and cap-before-deduct still holds.
    _criteria_with(monkeypatch, adjustments={"scope_gap": 8},
                   caps={"ic": 55, "wrong_function": 20})
    jid = seed_job(db, seed_company(), title="Staff Product Manager", level_band="ic")
    payload = dict(GOOD_PAYLOAD, leads_discipline="product", near_miss_flags=["scope_gap"])
    client, _ = fake_client(payload)
    run(db, client)
    row = db.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()
    assert row["fit_score"] == 20 - 8
    assert "- Wrong function (leads product): 85 → 20" in row["scoring_notes"]
    assert "- Adjustments: 20 - 8 scope_gap → 12" in row["scoring_notes"]
    detail = json.loads(row["score_detail"])
    assert detail["function_cap"] == 20 and "cap" not in detail


def test_mgmt_cap_lower_than_function_cap_attributes_ic(db, seed_company, monkeypatch):
    # When the mgmt cap is the binding one, the IC-cap line and the 'cap'
    # detail key keep their pre-function-check contract.
    _criteria_with(monkeypatch, caps={"ic": 55, "wrong_function": 60})
    jid = seed_job(db, seed_company(), title="Staff Product Manager", level_band="ic")
    payload = dict(GOOD_PAYLOAD, leads_discipline="product")
    client, _ = fake_client(payload)
    run(db, client)
    row = db.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()
    assert row["fit_score"] == 55
    assert "- IC cap: 85 → 55" in row["scoring_notes"]
    detail = json.loads(row["score_detail"])
    assert detail["cap"] == 55 and "function_cap" not in detail
    assert "wrong_function" in json.loads(row["near_miss_flags"])


def test_ic_role_unclear_discipline_not_flagged(db, seed_company, monkeypatch):
    # An IC seat has no led discipline: no function_unclear noise on every IC
    # designer posting — the IC cap already owns those.
    _criteria_with(monkeypatch, caps={"ic": 55, "function_unclear": 50})
    jid = seed_job(db, seed_company(), title="Staff Product Designer", level_band="ic")
    payload = dict(GOOD_PAYLOAD, leads_discipline="unclear")
    client, _ = fake_client(payload)
    run(db, client)
    row = db.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()
    assert row["fit_score"] == 55  # the ic cap, not function_unclear's 50
    assert "function_unclear" not in json.loads(row["near_miss_flags"])
    assert "- IC cap: 85 → 55" in row["scoring_notes"]


def test_progress_counts_gated_jobs_up_front(db, seed_company):
    # A rescore over N active jobs reports progress over ALL of them, not just
    # the AI-scored subset: tier1 fails count as done immediately (146/181, not
    # a confusing 0/35), then AI-scored jobs tick the counter to total.
    cid = seed_company()
    seed_job(db, cid, title="Director of Design")                          # scorable
    seed_job(db, cid, title="Senior Design Manager", salary_max=120000)    # tier1 fail (comp)
    seed_job(db, cid, title="Head of Design", salary_max=100000)           # tier1 fail (comp)
    client, _ = fake_client()
    progress = []
    report = asyncio.run(scoring.run_scoring(
        db, only_pending=False, client=client,
        on_progress=lambda done, total, errors: progress.append((done, total, errors)),
    ))
    assert report["scored"] == 1 and report["tier1_failed"] == 2
    assert progress[0] == (2, 3, 0)   # gated writes published before the first batch
    assert progress[-1] == (3, 3, 0)  # ends at total, not at len(to_score)


def test_analysis_override_threads_model_and_request_shape(db, seed_company):
    """ai_models.analysis reroutes scoring end to end: the call carries the
    override id, drops temperature (the Sonnet-5 tier 400s on sampling params),
    sends thinking=disabled, and the ledger bills the model actually used —
    never the shipped default."""
    from jshq import aicfg
    from jshq import usage as usage_mod

    seed_job(db, seed_company())
    state = {}

    async def create(**kwargs):
        state.clear()
        state.update(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(GOOD_PAYLOAD))],
            usage=SimpleNamespace(
                input_tokens=100, output_tokens=10,
                cache_read_input_tokens=0, cache_creation_input_tokens=0,
            ),
        )

    client = SimpleNamespace(messages=SimpleNamespace(create=create))

    # Default (no ai_models row): the Haiku tier, temp 0, no thinking param.
    assert run(db, client)["scored"] == 1
    assert state["model"] == aicfg.DEFAULTS["scoring"]
    assert state["temperature"] == 0.0
    assert "thinking" not in state

    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)",
        (aicfg.SETTING_KEY, json.dumps({"analysis": "claude-sonnet-5"})),
    )
    db.commit()
    report = asyncio.run(scoring.run_scoring(db, only_pending=False, client=client))
    assert report["scored"] == 1
    assert state["model"] == "claude-sonnet-5"
    assert "temperature" not in state
    assert state["thinking"] == {"type": "disabled"}
    by = usage_mod.read_usage_totals(db)["by_model"]
    assert by["claude-sonnet-5"]["calls"] == 1  # billed as the model that ran
    assert report["cost"] > 0
