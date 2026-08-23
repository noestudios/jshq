"""API for the semantic JD/role-mismatch feature (Phase 7i): the on-demand
cached proposal endpoint, the typed scoring_rule suggestion channel, accept/
ignore, and the active-rules GET/DELETE."""

from jshq.main import app, get_analysis_client
from test_haiku import fake_client  # dict -> JSON-text fake; counts calls

PROPOSAL = {"rule_text": "Down-rank hands-on ML model-building roles.", "rationale": "JD is all training."}


def _propose(client, job_id):
    """Create one proposal via the endpoint with a fake client; return its body."""
    fake, state = fake_client(PROPOSAL)
    app.dependency_overrides[get_analysis_client] = lambda: fake
    try:
        resp = client.post(f"/api/jobs/{job_id}/scoring-rule-proposal")
        assert resp.status_code == 200, resp.text
        return resp.json(), state
    finally:
        app.dependency_overrides.pop(get_analysis_client, None)


def test_propose_creates_typed_suggestion_and_caches(client, seed_job):
    job_id = seed_job(description_text="Build and train ML models all day.")
    fake, state = fake_client(PROPOSAL)
    app.dependency_overrides[get_analysis_client] = lambda: fake
    try:
        p = client.post(f"/api/jobs/{job_id}/scoring-rule-proposal").json()
        assert p["text"].startswith("Down-rank")
        assert p["job_id"] == job_id and p["source"] == "description"

        # surfaces as a typed scoring_rule suggestion
        sug = client.get("/api/suggestions").json()
        assert "scoring_rule" in sug
        assert any(x["id"] == p["id"] for x in sug["scoring_rule"])

        # second call for the same job is a cache hit — no new model call
        p2 = client.post(f"/api/jobs/{job_id}/scoring-rule-proposal").json()
        assert p2["id"] == p["id"]
        assert state["calls"] == 1

        # ?refresh=true forces a fresh call (and replaces the cached one)
        p3 = client.post(f"/api/jobs/{job_id}/scoring-rule-proposal?refresh=true").json()
        assert state["calls"] == 2
        # still one proposal per job (the new one replaced the old)
        ids = [x["id"] for x in client.get("/api/suggestions").json()["scoring_rule"]]
        assert ids == [p3["id"]]
    finally:
        app.dependency_overrides.pop(get_analysis_client, None)


def test_propose_404_unknown_job(client):
    fake, _ = fake_client(PROPOSAL)
    app.dependency_overrides[get_analysis_client] = lambda: fake
    try:
        assert client.post("/api/jobs/9999/scoring-rule-proposal").status_code == 404
    finally:
        app.dependency_overrides.pop(get_analysis_client, None)


def test_accept_moves_proposal_to_active(client, seed_job):
    job_id = seed_job(description_text="Build ML models.")
    p, _ = _propose(client, job_id)
    r = client.post("/api/suggestions/scoring-rule", json={"id": p["id"], "action": "accept"})
    assert r.status_code == 200
    body = r.json()
    assert any(x["id"] == p["id"] for x in body["rules"])
    assert body["proposals"] == []
    # active-rules endpoint reflects it; the pending queue no longer carries it
    assert any(x["id"] == p["id"] for x in client.get("/api/scoring-rules").json()["rules"])
    assert client.get("/api/suggestions").json()["scoring_rule"] == []


def test_ignore_drops_proposal_without_activating(client, seed_job):
    job_id = seed_job(description_text="Build ML models.")
    p, _ = _propose(client, job_id)
    r = client.post("/api/suggestions/scoring-rule", json={"id": p["id"], "action": "ignore"})
    assert r.status_code == 200
    assert r.json()["rules"] == []
    assert client.get("/api/scoring-rules").json()["rules"] == []
    assert client.get("/api/suggestions").json()["scoring_rule"] == []


def test_act_404_on_stale_proposal(client):
    r = client.post("/api/suggestions/scoring-rule", json={"id": "nope", "action": "accept"})
    assert r.status_code == 404


def test_delete_active_rule(client, seed_job):
    job_id = seed_job(description_text="Build ML models.")
    p, _ = _propose(client, job_id)
    client.post("/api/suggestions/scoring-rule", json={"id": p["id"], "action": "accept"})
    r = client.delete(f"/api/scoring-rules/{p['id']}")
    assert r.status_code == 200 and r.json()["rules"] == []
    # idempotent-ish: deleting again is a 404
    assert client.delete(f"/api/scoring-rules/{p['id']}").status_code == 404
