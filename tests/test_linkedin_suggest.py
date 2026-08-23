"""Suggest-with-AI for the LinkedIn role-check defaults: the on-demand
adjacent-titles endpoint (POST /api/settings/linkedin-titles/suggest), its
keyless 503, dedupe against the stored list, and the prompt/parse contract.
The wizard's deterministic derivation can't reach adjacent disciplines (a
designer also networks with UX researchers) — this call proposes those, and
nothing lands in the setting without an explicit frontend Add."""

import pytest

from jshq import aicfg, linkedin_titles
from jshq.main import app, get_analysis_client
from jshq.scoring.criteria import load_criteria
from test_haiku import fake_client

SUGGESTION = {
    "titles": [
        {"title": "Senior UX Researcher", "why": "research partners for design work"},
        {"title": "Research Manager", "why": "runs the adjacent research org"},
    ]
}


@pytest.fixture
def keyed(monkeypatch):
    """A key in the environment so get_analysis_client passes; the override
    below supplies the fake before any real client would be built."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")


def _suggest(client, fake):
    app.dependency_overrides[get_analysis_client] = lambda: fake
    try:
        return client.post("/api/settings/linkedin-titles/suggest")
    finally:
        app.dependency_overrides.pop(get_analysis_client, None)


def test_keyless_is_503_with_the_actionable_message(client):
    resp = client.post("/api/settings/linkedin-titles/suggest")
    assert resp.status_code == 503
    assert "Settings" in resp.json()["detail"]


def test_suggest_returns_titles(client, keyed):
    fake, state = fake_client(SUGGESTION)
    resp = _suggest(client, fake)
    assert resp.status_code == 200, resp.text
    titles = [s["title"] for s in resp.json()["suggestions"]]
    assert titles == ["Senior UX Researcher", "Research Manager"]
    assert state["calls"] == 1
    # The call is schema-forced and cheap-tier.
    assert state["kwargs"]["model"] == aicfg.DEFAULTS["linkedin_titles"]
    assert state["kwargs"]["output_config"]["format"]["schema"] == linkedin_titles.SCHEMA


def test_suggest_dedupes_against_the_stored_list(client, keyed):
    client.put(
        "/api/settings/linkedin_title_defaults",
        json={"value": ["senior ux researcher"]},  # case-insensitive collision
    )
    fake, state = fake_client(SUGGESTION)
    resp = _suggest(client, fake)
    titles = [s["title"] for s in resp.json()["suggestions"]]
    assert titles == ["Research Manager"]
    # And the stored list rode into the prompt as the don't-repeat contract.
    assert "senior ux researcher" in state["kwargs"]["system"][0]["text"]


def test_prompt_carries_field_bands_and_existing(client):
    criteria = load_criteria()
    system = linkedin_titles.build_prompt(criteria, ["Design Director"])
    assert criteria.domain_label in system
    assert "Design Director" in system
    assert "do NOT repeat" in system


def test_unusable_output_after_retry_is_502(client, keyed):
    fake, state = fake_client("not json", "still not json")
    resp = _suggest(client, fake)
    assert resp.status_code == 502
    assert state["calls"] == 2  # one retry, then give up


def test_all_duplicates_is_unusable_not_empty_200(client, keyed):
    """A reply that only restates the stored list is a failed call (retried,
    then 502) — never a silent empty success the UI would misread as done."""
    client.put(
        "/api/settings/linkedin_title_defaults",
        json={"value": ["Senior UX Researcher", "Research Manager"]},
    )
    fake, state = fake_client(SUGGESTION)
    resp = _suggest(client, fake)
    assert resp.status_code == 502
    assert state["calls"] == 2
