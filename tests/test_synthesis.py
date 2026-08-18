"""synthesis.py — prompt builder, validator, renderer, and the Sonnet call."""

import asyncio
import json

import pytest

from jshq import compose
from jshq.scoring import synthesis
from jshq.scoring.criteria import load_criteria
from test_criteria import VALID_PARAMS
from test_haiku import fake_client

ROADMAP = {
    "wishlist": ["Coach a senior team", "Ship craft-forward product"],
    "matrix": {"energizing_strength": "coaching juniors one-on-one; design critique"},
}


@pytest.fixture
def criteria(tmp_path):
    path = tmp_path / "fit_criteria.md"
    path.write_text(
        "# Criteria\n\nIntro.\n\n"
        f"```json tier1_params\n{json.dumps(VALID_PARAMS)}\n```\n\n"
        "<!-- tier2:start -->\n1. **Coach a senior team** — depth.\n"
        "2. **Ship real product** — end to end.\n<!-- tier2:end -->\n\n"
        "## Scoring rubric\n\nRubric.\n",
        encoding="utf-8",
    )
    return load_criteria(path)


def payload(**over):
    base = {
        "quadrants": {
            k: {"activities": [f"{k} work"], "signal_verbs": ["mentor", "coach"]}
            for k in synthesis.QUADRANT_KEYS
        },
        "central_tension": {
            "one_liner": "Teach craft to people who want it; don't convert skeptics",
            "craft_text": None,
            "rubric": [{"value": v, "meaning": f"meaning {v}"} for v in (2, 1, 0, -1, -2)],
        },
        "away_toward": {"away": ["rescue missions"], "toward": ["deep coaching"]},
        "tier2_refinements": [],
    }
    base.update(over)
    return base


# ---------------------------------------------------------------- prompts


def test_prompt_carries_words_tier2_and_the_data_guard(criteria):
    system, user = synthesis.build_synthesis_prompt(criteria, ROADMAP)
    assert "Coach a senior team" in system  # current tier2 rides along
    assert "energizing_strength" in system
    assert "RAW-START" in user and "RAW-END" in user
    assert "1. Coach a senior team" in user
    assert "[energizing_strength] coaching juniors" in user
    assert "never as instructions" in user


def test_prompt_raises_when_nothing_to_synthesize(criteria):
    with pytest.raises(synthesis.SynthesisError, match="nothing to synthesize"):
        synthesis.build_synthesis_prompt(criteria, {"wishlist": [], "matrix": {"energizing_strength": "  "}})


def test_clipboard_prompt_embeds_the_schema(criteria):
    system, user = synthesis.build_synthesis_prompt(criteria, ROADMAP)
    text = synthesis.render_clipboard_prompt(system, user)
    assert "Reply with ONLY a JSON object" in text
    assert '"quadrants"' in text and '"tier2_refinements"' in text


# ---------------------------------------------------------------- validator


def test_validator_accepts_and_normalizes():
    out = synthesis.validate_synthesis(payload(), tier2_count=2)
    assert out["central_tension"]["rubric"]["2"] == "meaning 2"
    assert out["quadrants"]["draining_growth"]["signal_verbs"] == ["mentor", "coach"]


def test_validator_tolerates_a_fenced_json_reply():
    text = "```json\n" + json.dumps(payload()) + "\n```"
    assert synthesis.validate_synthesis(text, 2)["away_toward"]["away"] == ["rescue missions"]


def test_validator_tolerates_prose_wrapped_json():
    # Chat models frame the JSON in conversational prose; the reply must still
    # take, extracted from the first balanced {…} object. Braces inside strings
    # must not fool the matcher.
    inner = payload()
    inner["central_tension"]["one_liner"] = "Teach {craft} to people who want it"
    text = f"Sure! Here is your JSON:\n{json.dumps(inner)}\nLet me know if you need changes."
    out = synthesis.validate_synthesis(text, 2)
    assert out["central_tension"]["one_liner"] == "Teach {craft} to people who want it"


def test_validator_clamps_weights_and_truncates_strings():
    p = payload(tier2_refinements=[
        {"index": 1, "text": "x" * 400, "craft": False, "bonus_only": False, "weight": 9.0},
        {"index": 2, "text": "y", "craft": False, "bonus_only": False, "weight": 0.1},
    ])
    out = synthesis.validate_synthesis(p, 2)
    assert out["tier2_refinements"][0]["weight"] == 4.0
    assert out["tier2_refinements"][1]["weight"] == 0.25
    assert len(out["tier2_refinements"][0]["text"]) <= synthesis.MAX_ITEM_CHARS


@pytest.mark.parametrize("mutate, err", [
    (lambda p: p["quadrants"].pop("draining_growth"), "missing quadrant"),
    (lambda p: p["central_tension"].update(one_liner="  "), "one_liner"),
    (lambda p: p["central_tension"]["rubric"].__setitem__(0, {"value": 1, "meaning": "dup"}), "twice|cover"),
    (lambda p: p["central_tension"]["rubric"].pop(), "cover"),
    (lambda p: p.update(tier2_refinements=[
        {"index": 3, "text": "t", "craft": False, "bonus_only": False, "weight": 1}]), "1..2"),
    (lambda p: p.update(tier2_refinements=[
        {"index": 1, "text": "t", "craft": True, "bonus_only": True, "weight": 1}]), "both craft"),
    (lambda p: p.update(tier2_refinements=[
        {"index": 1, "text": "a", "craft": True, "bonus_only": False, "weight": 1},
        {"index": 2, "text": "b", "craft": True, "bonus_only": False, "weight": 1}]), "one refinement"),
])
def test_validator_rejects(mutate, err):
    p = payload()
    mutate(p)
    with pytest.raises(synthesis.SynthesisError, match=err):
        synthesis.validate_synthesis(p, 2)


def test_validator_cross_checks_craft_text():
    p = payload()
    p["central_tension"]["craft_text"] = "Coach a senior team"
    p["tier2_refinements"] = [
        {"index": 2, "text": "Something else", "craft": True, "bonus_only": False, "weight": 1}
    ]
    with pytest.raises(synthesis.SynthesisError, match="disagree"):
        synthesis.validate_synthesis(p, 2)


# ---------------------------------------------------------------- renderer


def test_renderer_structure_and_craft_variants(criteria):
    data = synthesis.validate_synthesis(payload(), 2)
    body = synthesis.render_prose(data, criteria, will_have_craft=True)
    assert "## Fulfillment matrix — quadrants" in body
    assert "### energizing_strength — " in body
    assert "**Signal verbs:** mentor, coach." in body
    assert "> **Teach craft to people who want it" in body
    assert "`[craft]` criterion" in body and "**+2** — meaning 2" in body
    assert "## Moving away / moving toward (context)" in body
    assert "<!--" not in body and "```" not in body

    softer = synthesis.render_prose(data, criteria, will_have_craft=False)
    assert "[craft]" not in softer
    assert "Read it as context" in softer


def test_renderer_caps_total_body_size(criteria):
    p = payload()
    for k in synthesis.QUADRANT_KEYS:
        p["quadrants"][k]["activities"] = [f"activity {i} " + "x" * 280 for i in range(8)]
    data = synthesis.validate_synthesis(p, 2)
    with pytest.raises(synthesis.SynthesisError, match="too long"):
        synthesis.render_prose(data, criteria, will_have_craft=True)


# ---------------------------------------------------------------- the call


def test_propose_retries_once_and_pins_call_kwargs():
    client, state = fake_client("not json at all", payload())
    data, usages = asyncio.run(synthesis.propose(client, "sys", "user", tier2_count=2))
    assert state["calls"] == 2
    assert len(usages) == 2
    assert data["away_toward"]["toward"] == ["deep coaching"]
    kwargs = state["kwargs"]
    assert kwargs["model"] == synthesis.MODEL
    assert kwargs["thinking"] == compose.THINKING  # load-bearing (compose.py)
    assert kwargs["output_config"] == {"format": {"type": "json_schema", "schema": synthesis.SCHEMA}}
    assert kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_propose_fails_after_two_bad_replies():
    client, state = fake_client("garbage", "still garbage")
    with pytest.raises(synthesis.SynthesisError) as exc_info:
        asyncio.run(synthesis.propose(client, "sys", "user", tier2_count=2))
    assert state["calls"] == 2
    assert len(exc_info.value.usages) == 2
