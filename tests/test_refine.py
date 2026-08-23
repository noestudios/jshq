"""POST /api/refine-tells + the prompt/parse in app/refine.py.

Hermetic: fake Anthropic client (structured JSON), no live API."""

import json
from types import SimpleNamespace

from jshq import aicfg, compose, refine
from jshq.main import app, get_compose_client

EM_DASH = "—"


def fake_refine_client(payload):
    state = {"calls": 0, "kwargs": None}

    async def create(**kwargs):
        state["calls"] += 1
        state["kwargs"] = kwargs
        usage = SimpleNamespace(
            input_tokens=800, output_tokens=300,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        )
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(payload))], usage=usage
        )

    return SimpleNamespace(messages=SimpleNamespace(create=create)), state


def test_refine_prompt_embeds_voice_guide_and_rubric():
    system = refine.build_system_prompt("MY VOICE", compose.load_ai_tells())
    assert "MY VOICE" in system
    assert "AI-TELL RUBRIC" in system
    assert "never change facts" in system.lower()
    assert EM_DASH not in system
    assert "Chris" not in system  # the persona name comes from the criteria doc


def test_refine_parses_structured_output_and_sweeps():
    resp = SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps({
        "score": 8,
        "tells_fixed": ["em-dash", "cliche-metaphor"],
        "refined_text": "I led the rebuild — cleaner now.",  # a stray em dash
    }))])
    out = refine._parse(resp)
    assert out["score"] == 8
    assert out["tells_fixed"] == ["em-dash", "cliche-metaphor"]
    assert EM_DASH not in out["refined_text"]  # swept as a backstop
    assert out["refined_text"] == "I led the rebuild, cleaner now."


def test_refine_endpoint_returns_and_records_spend(client, db):
    fake, state = fake_refine_client({
        "score": 9, "tells_fixed": ["em-dash"], "refined_text": "Cleaner copy, no tells.",
    })
    app.dependency_overrides[get_compose_client] = lambda: fake
    resp = client.post("/api/refine-tells", json={"text": "Some draft — with a tell."})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["refined_text"] == "Cleaner copy, no tells."
    assert body["score"] == 9 and body["tells_fixed"] == ["em-dash"]
    assert state["calls"] == 1

    from jshq.usage import read_usage_totals

    assert read_usage_totals(db)["by_model"][aicfg.DEFAULTS["refine"]]["calls"] == 1


def test_refine_empty_text_422(client):
    # Give the client dep a fake so the empty-body 422 surfaces before the
    # no-API-key 503 that get_compose_client would otherwise raise.
    fake, _ = fake_refine_client({"score": 10, "tells_fixed": [], "refined_text": "x"})
    app.dependency_overrides[get_compose_client] = lambda: fake
    assert client.post("/api/refine-tells", json={"text": "   "}).status_code == 422


def test_refine_unusable_output_502(client):
    fake, _ = fake_refine_client({"score": 5, "tells_fixed": [], "refined_text": ""})
    app.dependency_overrides[get_compose_client] = lambda: fake
    assert client.post("/api/refine-tells", json={"text": "draft"}).status_code == 502
