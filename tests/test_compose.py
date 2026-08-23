"""POST /api/compose + the pure prompt/context builders in app/compose.py."""

import json
from datetime import date
from types import SimpleNamespace

import pytest

from jshq import aicfg, compose
from jshq.main import app, get_compose_client


def fake_client(text="Hi — thanks for the time today.", raise_exc=None):
    state = {"calls": 0, "kwargs": None}

    async def create(**kwargs):
        state["calls"] += 1
        state["kwargs"] = kwargs
        if raise_exc:
            raise raise_exc
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])

    return SimpleNamespace(messages=SimpleNamespace(create=create)), state


@pytest.fixture
def compose_client(client):
    """TestClient with a captured fake Anthropic client injected."""
    fake, state = fake_client()
    app.dependency_overrides[get_compose_client] = lambda: fake
    return client, state


def job_body(job_id, **overrides):
    return {"intent": "thank_you", "entity_type": "job", "entity_id": job_id, **overrides}


# --- pure builders ---


def test_intent_briefs_match_compose_in():
    from typing import get_args

    from jshq.models import ComposeIn

    literal = ComposeIn.model_fields["intent"].annotation
    assert set(get_args(literal)) == set(compose.INTENT_BRIEFS)


def test_system_prompt_embeds_voice_guide():
    system = compose.build_system_prompt("## Tone\nDirect, warm.")
    assert "--- VOICE GUIDE ---" in system
    assert "Direct, warm." in system
    assert "Output only the draft text" in system


def test_prompts_name_nobody_in_code():
    """Persona names come from the criteria doc, never from a code literal."""
    system = compose.build_system_prompt("## Tone\nDirect, warm.")
    assert "Chris" not in system
    assert "Chris" not in compose.build_user_message("outreach", "CTX", "be brief", None)
    for brief in compose.INTENT_BRIEFS.values():
        assert "Chris" not in brief


def test_system_prompt_without_guide_falls_back():
    system = compose.build_system_prompt("")
    assert "--- VOICE GUIDE ---" not in system
    assert "No voice guide is available" in system


def test_load_voice_guide_missing_file(tmp_path):
    assert compose.load_voice_guide(tmp_path / "nope.md") == ""


def test_voice_guide_path_prefers_data_dir_copy(tmp_path, monkeypatch):
    from jshq import paths

    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    # No live copy yet → the shipped default is used.
    assert compose.voice_guide_path() == compose.VOICE_GUIDE_PATH
    # Once a copy exists in DATA_DIR, that wins and load_voice_guide reads it.
    (tmp_path / "voice_guide.md").write_text("my own voice", encoding="utf-8")
    assert compose.voice_guide_path() == tmp_path / "voice_guide.md"
    assert compose.load_voice_guide() == "my own voice"


def test_save_voice_guide_writes_data_dir_atomically(tmp_path, monkeypatch):
    from jshq import paths

    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    compose.save_voice_guide("Write like a human.\nNo em dashes.")
    assert (tmp_path / "voice_guide.md").read_text(encoding="utf-8") == (
        "Write like a human.\nNo em dashes."
    )
    assert not (tmp_path / "voice_guide.md.tmp").exists()  # temp cleaned up
    assert compose.load_voice_guide() == "Write like a human.\nNo em dashes."


def test_job_context_truncates_long_jd(db, seed_job):
    job_id = seed_job(description_text="x" * 10_000)
    context = compose.build_entity_context(db, "job", job_id)
    assert "[truncated]" in context
    assert len(context) < 8_000


def test_job_context_includes_fields(db, seed_job):
    job_id = seed_job(title="Head of Design", location="Evanston, IL", remote_type="hybrid")
    context = compose.build_entity_context(db, "job", job_id)
    assert "Head of Design" in context
    assert "TestCo" in context
    assert "Evanston, IL" in context and "hybrid" in context


def test_contact_context_includes_notes(db, seed_contact):
    contact_id = seed_contact(name="Dana Lee", relationship_notes="Met at a mentoring AMA.")
    context = compose.build_entity_context(db, "contact", contact_id)
    assert "Dana Lee" in context
    assert "Met at a mentoring AMA." in context


def test_context_none_for_missing_entity(db):
    assert compose.build_entity_context(db, "job", 9999) is None
    assert compose.build_entity_context(db, "contact", 9999) is None


def test_timeline_bounded_and_json_summarized(db, seed_job, seed_activity):
    job_id = seed_job()
    for i in range(20):
        seed_activity(entity_type="job", entity_id=job_id, type="note", content=f"note {i}")
    seed_activity(
        entity_type="job", entity_id=job_id, type="dismissal", date="2026-06-12",
        content=json.dumps({"reason": "wrong level", "title": "x"}),
    )
    seed_activity(
        entity_type="job", entity_id=job_id, type="compose", date="2026-06-12",
        content=json.dumps({"intent": "outreach", "draft": "Long draft " * 50}),
    )
    context = compose.build_entity_context(db, "job", job_id)
    timeline = context.split("Recent history (newest first):\n")[1]
    assert len(timeline.splitlines()) == compose.TIMELINE_LIMIT
    assert "dismissed: wrong level" in timeline
    assert "drafted outreach" in timeline
    assert all(len(line) <= compose.TIMELINE_LINE_LIMIT + 30 for line in timeline.splitlines())


def test_user_message_question_and_instructions():
    msg = compose.build_user_message(
        "application_answer", "CONTEXT", "keep it under 100 words", "Why this role?"
    )
    assert "Why this role?" in msg
    assert "keep it under 100 words" in msg
    msg2 = compose.build_user_message("thank_you", "CONTEXT", None, None)
    assert "Application question" not in msg2
    assert "Additional instructions" not in msg2


# --- endpoint ---


def test_compose_job_happy_path(compose_client, db, seed_job):
    http, state = compose_client
    job_id = seed_job()
    resp = http.post("/api/compose", json=job_body(job_id))
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["draft"] == "Hi, thanks for the time today."  # em dash swept from output
    assert payload["model"] == aicfg.DEFAULTS["compose"]
    assert state["calls"] == 1
    assert state["kwargs"]["model"] == aicfg.DEFAULTS["compose"]
    row = db.execute(
        "SELECT * FROM activities WHERE id = ?", (payload["activity_id"],)
    ).fetchone()
    assert row["type"] == "compose"
    assert row["entity_type"] == "job" and row["entity_id"] == job_id
    assert row["date"] == date.today().isoformat()
    logged = json.loads(row["content"])
    assert logged["intent"] == "thank_you"
    assert logged["draft"] == payload["draft"]
    assert logged["model"] == aicfg.DEFAULTS["compose"]


def test_compose_records_sonnet_spend(client, db, seed_job):
    # A usage-bearing response threads through the endpoint into usage_totals
    # (the other fakes omit .usage, so record_usage no-ops there). Phase: total
    # spend tracking extended from scoring to the compose/tailor/learned calls.
    usage = SimpleNamespace(
        input_tokens=1000, output_tokens=500,
        cache_read_input_tokens=0, cache_creation_input_tokens=0,
    )

    async def create(**kwargs):
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="Thanks!")], usage=usage
        )

    app.dependency_overrides[get_compose_client] = lambda: SimpleNamespace(
        messages=SimpleNamespace(create=create)
    )
    assert client.post("/api/compose", json=job_body(seed_job())).status_code == 200

    from jshq.usage import cost_of, read_usage_totals

    sonnet = read_usage_totals(db)["by_model"][aicfg.DEFAULTS["compose"]]
    assert sonnet["calls"] == 1
    assert (sonnet["input"], sonnet["output"]) == (1000, 500)
    # Priced at the model's rate for today — compute it rather than pin a literal
    # (Sonnet 5 is $2/$10 during the intro window, $3/$15 from 2026-09-01).
    assert sonnet["cost"] == round(cost_of(aicfg.DEFAULTS["compose"], usage), 6)


def test_compose_contact_happy_path(compose_client, seed_contact):
    http, state = compose_client
    contact_id = seed_contact(name="Dana Lee")
    resp = http.post(
        "/api/compose",
        json={"intent": "outreach", "entity_type": "contact", "entity_id": contact_id},
    )
    assert resp.status_code == 200
    assert "Dana Lee" in state["kwargs"]["messages"][0]["content"]


def test_compose_context_includes_activities(compose_client, seed_job, seed_activity):
    http, state = compose_client
    job_id = seed_job()
    seed_activity(
        entity_type="job", entity_id=job_id, type="interview",
        content="Panel with the VP of Design",
    )
    resp = http.post("/api/compose", json=job_body(job_id))
    assert resp.status_code == 200
    assert "Panel with the VP of Design" in state["kwargs"]["messages"][0]["content"]


def test_compose_new_intents_accepted(compose_client, seed_contact):
    http, state = compose_client
    contact_id = seed_contact()
    for intent in ("connection_note", "reconnect"):
        resp = http.post(
            "/api/compose",
            json={"intent": intent, "entity_type": "contact", "entity_id": contact_id},
        )
        assert resp.status_code == 200
        assert intent in state["kwargs"]["messages"][0]["content"]


def test_compose_404_missing_entity(compose_client):
    http, _ = compose_client
    assert http.post("/api/compose", json=job_body(9999)).status_code == 404
    resp = http.post(
        "/api/compose", json={"intent": "outreach", "entity_type": "contact", "entity_id": 9999}
    )
    assert resp.status_code == 404


def test_compose_422_validation(compose_client, seed_job):
    http, _ = compose_client
    job_id = seed_job()
    assert http.post("/api/compose", json=job_body(job_id, intent="poem")).status_code == 422
    # application_answer requires a question…
    assert (
        http.post("/api/compose", json=job_body(job_id, intent="application_answer")).status_code
        == 422
    )
    # …and question is invalid with any other intent.
    assert (
        http.post("/api/compose", json=job_body(job_id, question="Why?")).status_code == 422
    )


def test_compose_503_without_api_key(client, seed_job):
    # No dependency override here: the autouse _no_live_anthropic fixture has
    # stripped the key, so the real get_compose_client must refuse.
    from jshq import apikey

    resp = client.post("/api/compose", json=job_body(seed_job()))
    assert resp.status_code == 503
    assert resp.json()["detail"] == apikey.MISSING_MESSAGE
    assert "Settings" in resp.json()["detail"]  # actionable, not just a diagnosis


def test_compose_502_on_model_error_logs_nothing(client, db, seed_job):
    fake, _ = fake_client(raise_exc=RuntimeError("simulated API error"))
    app.dependency_overrides[get_compose_client] = lambda: fake
    resp = client.post("/api/compose", json=job_body(seed_job()))
    assert resp.status_code == 502
    assert "[JSHQ-501]" in resp.json()["detail"]  # code, not prose — wording is free to change
    rows = db.execute("SELECT 1 FROM activities WHERE type = 'compose'").fetchall()
    assert rows == []


def test_compose_502_on_empty_draft(client, seed_job):
    fake, _ = fake_client(text="   ")
    app.dependency_overrides[get_compose_client] = lambda: fake
    assert client.post("/api/compose", json=job_body(seed_job())).status_code == 502


def test_compose_row_listed_via_activities_api(compose_client, seed_job):
    http, _ = compose_client
    job_id = seed_job()
    assert http.post("/api/compose", json=job_body(job_id)).status_code == 200
    rows = http.get(
        f"/api/activities?entity_type=job&entity_id={job_id}&types=compose"
    ).json()
    assert len(rows) == 1
    assert json.loads(rows[0]["content"])["intent"] == "thank_you"


# --- AI-tell hygiene: em-dash sweep + prompt cleanup + rubric integration ---

EM_DASH = "—"


def test_strip_em_dashes():
    assert compose.strip_em_dashes("a — b") == "a, b"
    assert compose.strip_em_dashes("a—b") == "a, b"
    assert compose.strip_em_dashes("a ― b") == "a, b"  # horizontal bar too
    # en dashes in ranges survive
    assert compose.strip_em_dashes("$150–190k") == "$150–190k"
    assert compose.strip_em_dashes("250–350 words") == "250–350 words"
    # paragraph breaks (\n\n) are preserved, not collapsed into the comma
    assert compose.strip_em_dashes("p1 — x\n\np2") == "p1, x\n\np2"
    # idempotent
    once = compose.strip_em_dashes("built the team — shipped in six weeks")
    assert compose.strip_em_dashes(once) == once
    assert EM_DASH not in once


def test_compose_prompts_and_briefs_have_no_em_dashes():
    vg = compose.load_voice_guide()
    tells = compose.ai_tells_prompt_block()
    assert EM_DASH not in compose.build_system_prompt(vg, tells)
    for intent in compose.INTENT_BRIEFS:
        assert EM_DASH not in compose.INTENT_BRIEFS[intent]
        assert EM_DASH not in compose.build_user_message(intent, "CTX", None, None)
    assert EM_DASH not in tells  # the rubric obeys its own hard rule


def test_ai_tells_integrated_into_generation_prompt():
    tells = compose.ai_tells_prompt_block()
    assert "AI tells" in tells or "AI-Tell" in tells
    assert "## Appendix" not in tells  # the LLM-judge appendix is refine-only
    prompt = compose.build_system_prompt("VOICE", tells)
    assert "no em dashes" in prompt.lower()
    assert "AI-TELL RUBRIC" in prompt
