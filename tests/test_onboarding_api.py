"""Onboarding API (Phase 4): first-run detection + the readiness aggregate, the
raw-exercise roadmap store, and the field (discipline) writer. Runs offline; the
autouse fixtures keep the key absent and background onboarding stubbed."""

import json
import shutil

import pytest

from jshq import apikey, paths
from jshq.scoring import criteria as criteria_mod


@pytest.fixture
def blank_install(tmp_path, monkeypatch):
    """A fresh install: the NEUTRAL starter as the live criteria + voice guide, an
    empty roadmap, no key, and (via the `client` fixture) an empty DB. Everything
    the readiness signal reads is pointed at this throwaway dir."""
    crit = tmp_path / "fit_criteria.md"
    shutil.copy(paths.DEFAULTS_DIR / "fit_criteria.starter.md", crit)
    monkeypatch.setattr(criteria_mod, "CRITERIA_PATH", crit)
    monkeypatch.setattr(criteria_mod, "_cache", None)
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    shutil.copy(paths.DEFAULTS_DIR / "voice_guide.starter.md", tmp_path / "voice_guide.md")
    return tmp_path


def test_fresh_install_is_first_run_with_nothing_done(client, blank_install):
    body = client.get("/api/onboarding").json()
    assert body["first_run"] is True
    assert body["company_count"] == 0
    assert body["state"] == {}
    steps = body["steps"]
    assert steps["company"] == {"done": False, "required": True}
    for key in ("api_key", "field", "hard_filters", "wishlist", "matrix"):
        assert steps[key]["done"] is False, key
    # persona (the display name) and the voice guide are both optional and
    # AI-only with no wizard step, so neither is a counted step — counting
    # either would strand a user below 100% with no way to finish.
    assert "persona" not in steps
    assert "voice_guide" not in steps
    assert body["complete_count"] == 0
    assert body["total"] == 6
    assert body["criteria_error"] is None


def test_a_rejected_key_does_not_complete_the_api_key_step(client, db, blank_install):
    # #33: a saved-but-rejected key is present but useless. The test endpoint
    # records a 401 as a verdict (simulated here via the settings row it writes);
    # readiness must then NOT complete the step, and both the status + onboarding
    # payloads must flag it so the wizard/board never imply scoring is on.
    client.put("/api/settings/api-key", json={"key": "sk-ant-bogus-XXXX"})
    db.execute(
        "INSERT INTO settings (key, value) VALUES ('api_key_test_verdict', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (json.dumps("rejected"),),
    )
    db.commit()

    status = client.get("/api/settings/api-key").json()
    assert status["configured"] is True and status["rejected"] is True

    ob = client.get("/api/onboarding").json()
    assert ob["api_key_rejected"] is True
    assert ob["steps"]["api_key"]["done"] is False
    assert ob["steps"]["api_key"]["rejected"] is True

    # Saving a fresh key clears the stale verdict — a new key is untested, so it
    # completes the step again (configured, no rejection on record).
    client.put("/api/settings/api-key", json={"key": "sk-ant-fresh-YYYY"})
    again = client.get("/api/onboarding").json()
    assert again["api_key_rejected"] is False
    assert again["steps"]["api_key"]["done"] is True
    assert client.get("/api/settings/api-key").json()["rejected"] is False


def test_field_and_wishlist_flip_done(client, blank_install):
    # Setting a persona is harmless (still a real endpoint) but is NOT a tracked
    # step — the field write is what marks the profile done.
    client.put(
        "/api/scoring/persona",
        json={"display_name": "Sam Lee", "domain_label": "product management"},
    )
    client.put("/api/scoring/discipline", json={"field": "product management"})
    client.put(
        "/api/scoring/criteria",
        json={
            "tier1_params": criteria_mod.load_criteria().params,
            "tier2_criteria": [{"text": "Sustainable pace", "weight": 1.5}],
        },
    )
    steps = client.get("/api/onboarding").json()["steps"]
    assert "persona" not in steps
    assert steps["field"]["done"] is True
    assert steps["wishlist"]["done"] is True


def test_hard_filters_and_key_flip_done(client, blank_install, monkeypatch):
    params = criteria_mod.load_criteria().params
    params["comp_floor"] = 150000
    client.put("/api/scoring/criteria", json={"tier1_params": params, "tier2_criteria": []})
    monkeypatch.setattr(apikey, "is_configured", lambda: True)
    steps = client.get("/api/onboarding").json()["steps"]
    assert steps["hard_filters"]["done"] is True
    assert steps["api_key"]["done"] is True


def test_declining_a_key_completes_the_api_key_step(client, blank_install):
    # Keyless is a first-class supported mode: an explicit "I don't want a key"
    # completes the api_key step, so a keyless-by-choice user isn't stranded below
    # 100% forever (the tracker only hides once complete_count == total).
    before = client.get("/api/onboarding").json()
    assert before["steps"]["api_key"]["done"] is False
    assert before["api_key_declined"] is False

    client.put("/api/settings/api_key_declined", json={"value": True})
    after = client.get("/api/onboarding").json()
    assert after["steps"]["api_key"]["done"] is True
    assert after["api_key_declined"] is True
    assert after["complete_count"] == before["complete_count"] + 1

    # Unchecking reverts it — the step is not-done again.
    client.put("/api/settings/api_key_declined", json={"value": False})
    reverted = client.get("/api/onboarding").json()
    assert reverted["steps"]["api_key"]["done"] is False
    assert reverted["api_key_declined"] is False


def test_tracker_dismissed_flag_round_trips(client, blank_install):
    # FLOW-02: the "I'm set — hide this" ✕ on the pill persists a settings row and
    # rides the payload so the frontend can gate on it. It's an acknowledgement, not
    # a readiness change — complete_count is untouched, only the nudge suppressed.
    before = client.get("/api/onboarding").json()
    assert before["tracker_dismissed"] is False

    client.put("/api/settings/onboarding_tracker_dismissed", json={"value": True})
    after = client.get("/api/onboarding").json()
    assert after["tracker_dismissed"] is True
    # Purely a nudge switch: the underlying readiness count does not move.
    assert after["complete_count"] == before["complete_count"]

    # Reversible — clearing it brings the pill's nudge back.
    client.put("/api/settings/onboarding_tracker_dismissed", json={"value": False})
    reverted = client.get("/api/onboarding").json()
    assert reverted["tracker_dismissed"] is False


def test_roadmap_round_trips_and_flips_matrix_done(client, blank_install):
    assert client.get("/api/onboarding/roadmap").json()["roadmap"] == {}
    payload = {
        "wishlist": [{"criterion": "pace", "detail": "sane hours", "weight": 2}],
        "matrix": {"energizing_strength": "mentoring", "draining_strength": "sales"},
    }
    r = client.put("/api/onboarding/roadmap", json=payload)
    assert r.status_code == 200
    assert r.json()["roadmap"] == payload
    assert client.get("/api/onboarding/roadmap").json()["roadmap"] == payload
    assert client.get("/api/onboarding").json()["steps"]["matrix"]["done"] is True


def test_roadmap_keeps_extra_keys_and_rejects_oversize(client, blank_install):
    # extra='allow' — an evolving exercise shape survives without an API change.
    r = client.put("/api/onboarding/roadmap", json={"notes": "raw brain-dump"})
    assert r.json()["roadmap"] == {"notes": "raw brain-dump"}
    big = client.put("/api/onboarding/roadmap", json={"wishlist": ["x" * 300_000]})
    assert big.status_code == 422
    # The oversize write is rejected; the previous save is untouched.
    assert client.get("/api/onboarding/roadmap").json()["roadmap"] == {"notes": "raw brain-dump"}


def test_dismiss_and_complete_transitions(client, blank_install):
    assert client.get("/api/onboarding").json()["first_run"] is True
    body = client.put("/api/onboarding", json={"dismissed": True}).json()
    assert body["state"]["dismissed_at"]
    assert body["first_run"] is False  # dismissing ends first-run
    done = client.put("/api/onboarding", json={"completed": True}).json()
    assert done["state"]["completed_at"]
    assert done["state"]["dismissed_at"]  # earlier dismissal preserved


def test_adding_a_company_ends_first_run(client, blank_install):
    client.post("/api/companies", json={"name": "Acme"})
    body = client.get("/api/onboarding").json()
    assert body["company_count"] == 1
    assert body["first_run"] is False
    assert body["steps"]["company"]["done"] is True


def test_discipline_writer_sets_in_band_and_neutralizes_default(client, blank_install):
    assert criteria_mod.load_criteria().taxonomy_is_default is True
    r = client.put("/api/scoring/discipline", json={"field": "Data Engineering"})
    assert r.status_code == 200
    assert r.json()["in_band_disciplines"] == ["data_engineering"]
    after = criteria_mod.load_criteria()
    assert after.taxonomy_is_default is False
    assert after.taxonomy["in_band_disciplines"] == ["data_engineering"]
    assert "Data Engineering" in after.taxonomy["disciplines"]["data_engineering"]


def test_discipline_writer_rejects_blank(client, blank_install):
    assert client.put("/api/scoring/discipline", json={"field": "   "}).status_code == 422


def test_discipline_writer_splits_commas_into_separate_fields(client, blank_install):
    # The wizard's placeholder teaches a comma list ("e.g. backend engineering,
    # product design, data science"); fusing it into one product_management_design
    # token made the fields inseparable in Settings and in the enum sent to the
    # model. Each comma-separated field is its own in-band discipline.
    r = client.put(
        "/api/scoring/discipline", json={"field": "Product Management,  growth marketing"}
    )
    assert r.status_code == 200
    assert r.json()["in_band_disciplines"] == ["product_management", "growth_marketing"]
    disciplines = criteria_mod.load_criteria().taxonomy["disciplines"]
    assert disciplines["product_management"] == "Product Management"
    assert disciplines["growth_marketing"] == "growth marketing"
    # The reserved out-of-band keys survive alongside the user's fields.
    assert "other" in disciplines and "unclear" in disciplines


def test_discipline_writer_never_shadows_reserved_keys(client, blank_install):
    # A user whose field literally slugs to "other" must not overwrite the
    # out-of-band bucket the function check depends on.
    r = client.put("/api/scoring/discipline", json={"field": "Other"})
    assert r.status_code == 200
    assert r.json()["in_band_disciplines"] == ["other_field"]
    disciplines = criteria_mod.load_criteria().taxonomy["disciplines"]
    assert disciplines["other"] == "a role in some other field"
    assert disciplines["other_field"] == "Other"


def test_matrix_of_empty_cells_does_not_count_as_done(client, blank_install):
    # The wizard co-writes the matrix dict alongside the wishlist; four empty
    # strings mean the user saved the OTHER exercise, and the tracker must not
    # report the matrix complete for a user who never touched it.
    empty = {k: "" for k in (
        "energizing_strength", "energizing_growth", "draining_growth", "draining_strength"
    )}
    client.put("/api/onboarding/roadmap", json={"wishlist": ["pace"], "matrix": empty})
    assert client.get("/api/onboarding").json()["steps"]["matrix"]["done"] is False
    client.put(
        "/api/onboarding/roadmap",
        json={"wishlist": ["pace"], "matrix": {**empty, "energizing_strength": "mentoring"}},
    )
    assert client.get("/api/onboarding").json()["steps"]["matrix"]["done"] is True


def test_criteria_example_serves_the_alex_reference(client):
    body = client.get("/api/scoring/criteria-example").json()
    assert "160000" in body["markdown"]  # the shipped Alex example, read-only


def test_broken_criteria_surfaces_error_without_crashing(client, blank_install):
    (blank_install / "fit_criteria.md").write_text("no params block", encoding="utf-8")
    criteria_mod._cache = None
    body = client.get("/api/onboarding").json()
    assert body["criteria_error"]
    assert body["steps"]["wishlist"]["done"] is False  # degrades, does not 500
