"""Synthesis API: keyed propose, keyless prompt + paste-back, preview → apply."""

import json

from jshq.main import app, get_compose_client
from test_haiku import fake_client
from test_synthesis import ROADMAP, payload


def put_roadmap(client, roadmap=ROADMAP):
    assert client.put("/api/onboarding/roadmap", json=roadmap).status_code == 200


def park_paste(client, p=None):
    r = client.post("/api/scoring/synthesis/reply", json={"reply": json.dumps(p or payload())})
    assert r.status_code == 200, r.text
    return r.json()["proposal"]


def test_get_reports_availability_and_parked_state(client, criteria_doc):
    put_roadmap(client, {"wishlist": [], "matrix": {}})
    r = client.get("/api/scoring/synthesis").json()
    assert r == {"proposal": None, "available": False}
    put_roadmap(client)
    assert client.get("/api/scoring/synthesis").json()["available"] is True


def test_keyless_prompt_renders_or_422s(client, criteria_doc):
    put_roadmap(client)
    r = client.get("/api/scoring/synthesis/prompt")
    assert r.status_code == 200
    prompt = r.json()["prompt"]
    assert "RAW-START" in prompt and '"quadrants"' in prompt
    put_roadmap(client, {"wishlist": [], "matrix": {}})
    r = client.get("/api/scoring/synthesis/prompt")
    assert r.status_code == 422
    assert "nothing to synthesize" in r.json()["detail"]


def test_keyed_propose_parks_a_proposal(client, criteria_doc):
    put_roadmap(client)
    fake, state = fake_client(payload())
    app.dependency_overrides[get_compose_client] = lambda: fake
    try:
        r = client.post("/api/scoring/synthesis")
        assert r.status_code == 200, r.text
        proposal = r.json()["proposal"]
        assert proposal["source"] == "api" and proposal["model"]
        assert state["calls"] == 1
        assert client.get("/api/scoring/synthesis").json()["proposal"]["id"] == proposal["id"]
    finally:
        app.dependency_overrides.pop(get_compose_client, None)


def test_keyed_propose_is_503_without_a_key(client, criteria_doc):
    put_roadmap(client)
    r = client.post("/api/scoring/synthesis")
    assert r.status_code == 503
    assert "Settings" in r.json()["detail"]  # apikey.MISSING_MESSAGE is actionable


def test_keyed_propose_failure_is_502(client, criteria_doc):
    put_roadmap(client)

    class Boom:
        class messages:
            @staticmethod
            async def create(**kwargs):
                raise RuntimeError("api melted")

    app.dependency_overrides[get_compose_client] = lambda: Boom()
    try:
        r = client.post("/api/scoring/synthesis")
        assert r.status_code == 502
        assert "synthesis failed" in r.json()["detail"]
        assert client.get("/api/scoring/synthesis").json()["proposal"] is None
    finally:
        app.dependency_overrides.pop(get_compose_client, None)


def test_paste_back_validates_and_parks(client, criteria_doc):
    proposal = park_paste(client)
    assert proposal["source"] == "paste" and proposal["model"] is None

    r = client.post("/api/scoring/synthesis/reply", json={"reply": "not json"})
    assert r.status_code == 422
    assert "not valid JSON" in r.json()["detail"]

    r = client.post("/api/scoring/synthesis/reply", json={"reply": "x" * (64 * 1024 + 1)})
    assert r.status_code == 422
    assert "too large" in r.json()["detail"]


def test_apply_writes_the_fenced_section_and_clears(client, criteria_doc):
    park_paste(client)
    tier2_before = client.get("/api/scoring/criteria").json()["tier2_criteria"]
    r = client.post("/api/scoring/synthesis/apply", json={})
    assert r.status_code == 200, r.text
    text = criteria_doc.read_text(encoding="utf-8")
    assert text.index("<!-- synthesis:start -->") < text.index("## Scoring rubric")
    assert "## Fulfillment matrix — quadrants" in text
    # prose-only apply: the ranked list is untouched
    assert r.json()["tier2_criteria"] == tier2_before
    assert client.get("/api/scoring/synthesis").json()["proposal"] is None


def test_apply_tier2_merges_refinements(client, criteria_doc):
    p = payload(tier2_refinements=[
        {"index": 1, "text": "Refined first criterion", "craft": False,
         "bonus_only": False, "weight": 2.0},
    ])
    park_paste(client, p)
    r = client.post("/api/scoring/synthesis/apply", json={"apply_tier2": True})
    assert r.status_code == 200, r.text
    first = r.json()["tier2_criteria"][0]
    assert first["text"] == "Refined first criterion"
    assert first["weight"] == 2.0


def test_apply_tier2_with_a_changed_list_409s_and_keeps_the_draft(client, criteria_doc):
    park_paste(client)
    # shrink the ranked list through the criteria API (markers preserved)
    doc = client.get("/api/scoring/criteria").json()
    doc["tier2_criteria"] = doc["tier2_criteria"][:-1]
    assert client.put("/api/scoring/criteria", json=doc).status_code == 200

    r = client.post("/api/scoring/synthesis/apply", json={"apply_tier2": True})
    assert r.status_code == 409
    assert "re-run synthesis" in r.json()["detail"]
    assert client.get("/api/scoring/synthesis").json()["proposal"] is not None


def test_apply_tier2_with_a_same_length_reorder_409s(client, criteria_doc):
    # Refinements address criteria by 1-based index. A count-only staleness
    # guard let a Settings reorder (count unchanged) slip through and land
    # each refinement's text/weight/craft on whatever criterion now occupies
    # the position — including silently moving the craft axis. The texts
    # fingerprint catches it.
    park_paste(client)
    doc = client.get("/api/scoring/criteria").json()
    doc["tier2_criteria"] = doc["tier2_criteria"][::-1]
    assert client.put("/api/scoring/criteria", json=doc).status_code == 200

    r = client.post("/api/scoring/synthesis/apply", json={"apply_tier2": True})
    assert r.status_code == 409
    assert "re-run synthesis" in r.json()["detail"]
    assert client.get("/api/scoring/synthesis").json()["proposal"] is not None


def test_apply_failure_keeps_doc_and_draft(client, criteria_doc):
    p = payload()
    for k in ("energizing_strength", "energizing_growth", "draining_growth", "draining_strength"):
        p["quadrants"][k]["activities"] = [f"a{i} " + "x" * 290 for i in range(8)]
    park_paste(client, p)
    before = criteria_doc.read_bytes()
    r = client.post("/api/scoring/synthesis/apply", json={})
    assert r.status_code == 422
    assert "too long" in r.json()["detail"]
    assert criteria_doc.read_bytes() == before
    assert client.get("/api/scoring/synthesis").json()["proposal"] is not None


def test_apply_without_a_draft_404s(client, criteria_doc):
    assert client.post("/api/scoring/synthesis/apply", json={}).status_code == 404


def test_discard(client, criteria_doc):
    park_paste(client)
    r = client.delete("/api/scoring/synthesis")
    assert r.status_code == 200 and r.json()["proposal"] is None
    assert client.get("/api/scoring/synthesis").json()["proposal"] is None
