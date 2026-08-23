"""learned.py — proposal prompt/parse, haiku injection, settings stores, and the
run_scoring wiring that feeds accepted rules into the scorer (Phase 7i)."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from jshq import aicfg, scoring
from jshq.scoring import haiku, learned
from jshq.scoring.criteria import Criteria

CRITERIA = Criteria(
    params={},
    prose="THE CRITERIA PROSE",
    tier2=[{"text": f"Criterion {n}", "weight": 1.0} for n in range(1, 12)],
)
GOOD = {"rule_text": "Down-rank hands-on ML model-building roles.", "rationale": "JD is all model training."}
SCORE_PAYLOAD = {
    # criterion 5 at 0 -> derived craft_lean 0 -> "mixed" display label
    "tier2": [{"n": n, "v": 0, "q": "evidence"} for n in range(1, 12)],
    "fit_quadrant": "energizing_strength",
    "management_type": "unclear",
    "function": "product",
    "leads_discipline": "design",
    "confidence": "low",
    "near_miss_flags": [],
    "scoring_notes": "ok",
}


def fake_client(*payloads):
    """messages.create returns each payload (dict -> JSON, str -> raw) in order;
    records the last kwargs (incl. the system prompt) and counts calls."""
    state = {"calls": 0, "kwargs": None}

    async def create(**kwargs):
        state["kwargs"] = kwargs
        payload = payloads[min(state["calls"], len(payloads) - 1)]
        state["calls"] += 1
        text = json.dumps(payload) if isinstance(payload, dict) else payload
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])

    return SimpleNamespace(messages=SimpleNamespace(create=create)), state


def job(**overrides):
    base = {
        "title": "Design Lead",
        "company_name": "TestCo",
        "location": "Remote - US",
        "remote_type": "remote",
        "level_band": "director",
        "salary_min": 200000,
        "salary_max": 240000,
        "salary_stated": 1,
        "description_text": "Build and train ML models all day.",
    }
    base.update(overrides)
    return base


# --- propose_rule ------------------------------------------------------------


def test_propose_happy_path():
    client, state = fake_client(GOOD)
    out, _ = asyncio.run(learned.propose_rule(client, "SYS", "USER"))
    assert out["rule_text"].startswith("Down-rank")
    assert out["rationale"] == GOOD["rationale"]
    assert state["calls"] == 1
    assert state["kwargs"]["model"] == aicfg.DEFAULTS["learned"]
    assert state["kwargs"]["output_config"]["format"]["schema"] == learned.SCHEMA


def test_propose_retries_then_raises():
    client, state = fake_client("not json at all")
    with pytest.raises(learned.LearnedRuleError):
        asyncio.run(learned.propose_rule(client, "SYS", "USER"))
    assert state["calls"] == 2


def test_propose_bad_first_good_second():
    client, state = fake_client("garbage", GOOD)
    out, _ = asyncio.run(learned.propose_rule(client, "SYS", "USER"))
    assert out["rule_text"].startswith("Down-rank")
    assert state["calls"] == 2


def test_propose_empty_rule_text_rejected():
    client, _ = fake_client({"rule_text": "   ", "rationale": "x"})
    with pytest.raises(learned.LearnedRuleError):
        asyncio.run(learned.propose_rule(client, "SYS", "USER"))


# --- prompt building ---------------------------------------------------------


def test_proposal_prompt_includes_context():
    sys = learned.build_proposal_prompt(CRITERIA, "DISMISSAL LINES", ["Existing rule A"])
    assert "THE CRITERIA PROSE" in sys
    assert "DISMISSAL LINES" in sys
    assert "Existing rule A" in sys
    assert "scoring layer" in sys.lower()


def test_proposal_prompt_omits_empty_digest_and_rules():
    sys = learned.build_proposal_prompt(CRITERIA, "", [])
    assert "DISMISSAL" not in sys
    assert "do NOT propose" not in sys  # existing-rules block omitted


def test_build_user_message_reuses_haiku():
    assert learned.build_user_message(job()) == haiku.build_user_message(job())


# --- haiku injection ---------------------------------------------------------


def test_haiku_prompt_includes_learned_rules():
    prompt = haiku.build_system_prompt(CRITERIA, "", ["Down-rank ML roles."])
    assert "Down-rank ML roles." in prompt
    assert "role_mismatch" in prompt


def test_haiku_prompt_omits_learned_block_when_empty():
    prompt = haiku.build_system_prompt(CRITERIA, "")
    assert "role_mismatch" not in prompt
    # the SCHEMA the scorer validates against is untouched by the new param
    assert "tier2" in haiku.build_schema()["properties"]


# --- settings stores ---------------------------------------------------------


def test_settings_stores_roundtrip(db):
    assert learned.read_scoring_rules(db) == []
    assert learned.read_proposals(db) == []
    learned.write_scoring_rules(db, [{"id": "r1", "text": "x"}])
    learned.write_proposals(db, [{"id": "p1", "text": "y", "job_id": 1}])
    db.commit()
    assert learned.read_scoring_rules(db)[0]["id"] == "r1"
    assert learned.read_proposals(db)[0]["job_id"] == 1


# --- run_scoring wiring ------------------------------------------------------


def _seed_active_job(db, cid):
    fields = {
        "company_id": cid, "title": "Director of Design", "remote_type": "remote",
        "level_band": "director", "salary_min": 200000, "salary_max": 240000,
        "salary_stated": 1, "description_text": "Mentor designers.",
        "status": "active", "dedupe_key": f"{cid}:job",
    }
    cols = ", ".join(fields)
    marks = ", ".join("?" * len(fields))
    db.execute(f"INSERT INTO jobs ({cols}) VALUES ({marks})", tuple(fields.values()))
    db.commit()


def test_run_scoring_injects_active_rules_into_prompt(db, seed_company):
    _seed_active_job(db, seed_company())
    learned.write_scoring_rules(db, [{"id": "r1", "text": "Down-rank pure ML roles."}])
    db.commit()
    client, state = fake_client(SCORE_PAYLOAD)
    asyncio.run(scoring.run_scoring(db, client=client))
    assert state["calls"] == 1  # job passed Tier 1 and was scored
    system_text = state["kwargs"]["system"][0]["text"]
    assert "Down-rank pure ML roles." in system_text


def test_run_scoring_without_rules_has_no_learned_block(db, seed_company):
    _seed_active_job(db, seed_company())
    client, state = fake_client(SCORE_PAYLOAD)
    asyncio.run(scoring.run_scoring(db, client=client))
    # Assert on the block's heading, not the bare `role_mismatch` token — the
    # criteria doc's own prose legitimately mentions the token (the
    # score-adjustments maintainer note explains why it's excluded).
    assert "Learned role-mismatch rules" not in state["kwargs"]["system"][0]["text"]
