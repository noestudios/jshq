"""Pure tailoring logic (app/tailor.py): prompts, parsing, normalization,
apply. Hermetic — fake content + fake client, no API, no Chrome."""

import asyncio
import json

import pytest
from test_compose import fake_client
from test_resume_render import fake_content

from jshq import tailor
from jshq.resume import render
from jshq.scoring import criteria

EDITABLE_IDS = ["summary", "win-1", "win-2", "exp-1-b1", "exp-2-b1"]


@pytest.fixture
def persona_name(monkeypatch):
    """The builders read the name from the criteria doc at call time; pin it so
    these assertions test the prompt wording, not the shipped example persona."""
    name = "Sam Example"
    monkeypatch.setattr(tailor, "persona_display_name", lambda: name)
    return name


def fake_job(**overrides):
    job = {
        "title": "Head of Design",
        "company_name": "TestCo",
        "location": "Remote",
        "remote_type": "remote",
        "level_band": "director",
        "description_text": "We need a design leader who teaches craft.",
        "fit_score": 82,
        "fit_quadrant": "core",
        "near_miss_flags": '["comp unknown"]',
        "scoring_notes": "Strong fit on responsibilities.",
    }
    job.update(overrides)
    return job


def good_output(changes=None, **overrides):
    data = {
        "analysis": "They want a systems-minded design leader.",
        "changes": changes if changes is not None else [
            {"id": "summary", "new": "A tailored summary.", "rationale": "Lead with leadership."},
            {"id": "win-1", "new": "Did **bold** tailored things", "rationale": "Echoes the JD."},
        ],
        "cover_letter": "Dear team,\n\nI am excited.\n\nBest,\nPat",
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------- editable nodes


def test_editable_nodes_are_paragraphs_and_bullets_in_order():
    nodes = tailor.get_editable_nodes(fake_content())
    assert list(nodes) == EDITABLE_IDS
    assert nodes["summary"].startswith("A summary")
    assert nodes["exp-1-b1"] == "<scored> 100%"


# ---------------------------------------------------------------- prompts


def test_system_prompt_embeds_voice_guide_and_contract():
    system = tailor.build_system_prompt("## Tone\nDirect, warm.")
    assert "--- VOICE GUIDE ---" in system
    assert "Direct, warm." in system
    assert '"changes"' in system and '"cover_letter"' in system
    assert "EDITABLE IDS" in system


def test_system_prompt_without_guide_falls_back():
    system = tailor.build_system_prompt("")
    assert "--- VOICE GUIDE ---" not in system
    assert "No voice guide is available" in system


def test_prompts_take_the_persona_name_from_the_criteria_doc(persona_name, monkeypatch):
    """No name is baked into the code; a doc that names nobody leaves the
    prompts in neutral third person."""
    for system in (tailor.build_system_prompt(""), tailor.build_chat_system_prompt("")):
        assert f"{persona_name}'s resume" in system
        assert f"{persona_name}'s real experience" in system  # the change rules
    monkeypatch.setattr(tailor, "persona_display_name", lambda: criteria.NEUTRAL_DISPLAY_NAME)
    assert f"{criteria.NEUTRAL_DISPLAY_NAME}'s resume" in tailor.build_system_prompt("")


def test_resume_block_marks_editable_and_readonly_lines():
    block = tailor.build_resume_block(fake_content())
    for node_id in EDITABLE_IDS:
        assert f"[{node_id}]" in block
    assert "(read-only) Alpha & Beta | Gamma | Delta" in block
    assert "(read-only) Design: Tool A, Tool B" in block
    assert "(read-only) Lead, TestCo (Jan 2020 – Dec 2021)" in block
    assert block.rstrip().endswith(f"EDITABLE IDS: {', '.join(EDITABLE_IDS)}")
    assert "[exp-1]" not in block  # role ids are never offered as editable


def test_user_message_includes_fit_and_truncates_jd():
    job = fake_job(description_text="x" * (tailor.JD_CHAR_LIMIT + 500))
    user = tailor.build_user_message(job, fake_content(), None)
    assert "[truncated]" in user
    assert len(user) < tailor.JD_CHAR_LIMIT + 3_000  # JD capped, rest is fixed-ish
    assert "Fit score: 82/100" in user
    assert "comp unknown" in user
    assert "Scoring notes: Strong fit" in user


def test_user_message_appends_instructions(persona_name):
    user = tailor.build_user_message(fake_job(), fake_content(), "  less salesy  ")
    assert f"Additional instructions from {persona_name}: less salesy" in user
    assert "instructions" not in tailor.build_user_message(fake_job(), fake_content(), "  ")


# ---------------------------------------------------------------- parse_output


def test_parse_accepts_clean_fenced_and_prose_wrapped():
    payload = good_output()
    for text in (
        json.dumps(payload),
        f"```json\n{json.dumps(payload)}\n```",
        f"Here you go:\n{json.dumps(payload)}\nHope that helps!",
    ):
        assert tailor.parse_output(text)["analysis"] == payload["analysis"]


@pytest.mark.parametrize("text, match", [
    ("no braces here", "no JSON object"),
    ("{not valid json}", "not valid JSON"),
    ('{"analysis": 1, "changes": [], "cover_letter": "x"}', "analysis"),
    ('{"analysis": "a", "changes": "nope", "cover_letter": "x"}', "changes"),
    ('{"analysis": "a", "changes": []}', "cover_letter"),
    ('{"analysis": "a", "changes": [], "cover_letter": "  "}', "cover_letter"),
])
def test_parse_rejects_bad_output(text, match):
    with pytest.raises(tailor.TailorError, match=match):
        tailor.parse_output(text)


# ---------------------------------------------------------------- normalize


def test_normalize_fills_old_from_content_and_defaults_unapproved():
    plan, warnings = tailor.normalize_changes(good_output()["changes"], fake_content())
    assert warnings == []
    assert [c["id"] for c in plan] == ["summary", "win-1"]
    assert plan[0]["old"].startswith("A summary")  # server truth, not model's
    assert all(c["approved"] is False for c in plan)
    assert plan[1]["rationale"] == "Echoes the JD."


@pytest.mark.parametrize("change, warning", [
    ({"id": "nope", "new": "x"}, "unknown or read-only"),
    ({"id": "exp-1", "new": "x"}, "unknown or read-only"),  # role id = read-only
    ({"id": "win-1", "new": "   "}, "empty rewrite"),
    ({"id": "win-1", "new": 7}, "empty rewrite"),
    ("not a dict", "non-object"),
])
def test_normalize_drops_bad_entries_with_warnings(change, warning):
    plan, warnings = tailor.normalize_changes([change], fake_content())
    assert plan == []
    assert any(warning in w for w in warnings)


def test_normalize_drops_duplicates_and_noops():
    changes = [
        {"id": "win-1", "new": "first rewrite"},
        {"id": "win-1", "new": "second rewrite"},  # dupe: first wins
        {"id": "win-2", "new": "Did *italic* things"},  # identical = no-op
    ]
    plan, warnings = tailor.normalize_changes(changes, fake_content())
    assert [c["id"] for c in plan] == ["win-1"]
    assert plan[0]["new"] == "first rewrite"
    assert any("duplicate" in w for w in warnings)
    assert any("no-op" in w for w in warnings)


def test_normalize_caps_plan_size(monkeypatch):
    monkeypatch.setattr(tailor, "MAX_CHANGES", 2)
    changes = [{"id": i, "new": f"rewrite {i}"} for i in EDITABLE_IDS[:3]]
    plan, warnings = tailor.normalize_changes(changes, fake_content())
    assert len(plan) == 2
    assert any("first 2 of 3" in w for w in warnings)


# ---------------------------------------------------------------- apply


def _plan(approved_ids=()):
    plan, _ = tailor.normalize_changes(good_output()["changes"], fake_content())
    for change in plan:
        change["approved"] = change["id"] in approved_ids
    return plan


def test_apply_changes_patches_only_approved():
    content = fake_content()
    patched = tailor.apply_changes(content, _plan(approved_ids={"summary"}))
    assert patched["sections"][0]["text"] == "A tailored summary."
    assert patched["sections"][3]["bullets"][0]["text"] == "Did **bold** things"  # unapproved
    render.validate_content(patched)


def test_apply_changes_never_mutates_the_input():
    content = fake_content()
    tailor.apply_changes(content, _plan(approved_ids={"summary", "win-1"}))
    assert content == fake_content()


def test_apply_changes_raises_on_drift():
    content = fake_content()
    content["sections"][0]["text"] = "Someone edited the master since."
    with pytest.raises(tailor.TailorError, match="changed since"):
        tailor.apply_changes(content, _plan(approved_ids={"summary"}))


# ---------------------------------------------------------------- generate


def test_generate_returns_parsed_contract_first_try():
    fake, state = fake_client(json.dumps(good_output()))
    data, _ = asyncio.run(tailor.generate(fake, "system", "user"))
    assert data["cover_letter"].startswith("Dear team")
    assert state["calls"] == 1
    assert state["kwargs"]["max_tokens"] == tailor.MAX_TOKENS


def test_generate_retries_once_with_the_parse_error():
    from types import SimpleNamespace

    replies = iter(["sorry, no JSON from me", json.dumps(good_output())])
    state = {"calls": 0, "messages": None}

    async def create(**kwargs):
        state["calls"] += 1
        state["messages"] = kwargs["messages"]
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=next(replies))])

    fake = SimpleNamespace(messages=SimpleNamespace(create=create))
    data, _ = asyncio.run(tailor.generate(fake, "system", "user"))
    assert data["analysis"]
    assert state["calls"] == 2
    # the corrective turn carries the previous reply + the parse error
    assert state["messages"][1]["content"] == "sorry, no JSON from me"
    assert "could not be used" in state["messages"][2]["content"]


def test_generate_gives_up_after_one_retry():
    fake, state = fake_client("still not json")
    with pytest.raises(tailor.TailorError, match="after retry"):
        asyncio.run(tailor.generate(fake, "system", "user"))
    assert state["calls"] == 2


# ---------------------------------------------------------------- chat (7f)


def chat_output(**overrides):
    data = {
        "reply": "Done — updated.",
        "changes": [],
        "remove": [],
        "cover_letter": None,
    }
    data.update(overrides)
    return data


def test_chat_system_prompt_shares_rules_and_swaps_contract():
    system = tailor.build_chat_system_prompt("## Tone\nDirect, warm.")
    assert "--- VOICE GUIDE ---" in system
    assert "Direct, warm." in system
    assert "Resume change rules:" in system
    assert "Cover letter rules:" in system
    assert '"reply"' in system and '"remove"' in system
    assert '"analysis"' not in system  # chat contract, not the generate one
    assert tailor.build_chat_system_prompt("").count("No voice guide") == 1


def test_chat_user_message_carries_current_plan_letter_and_approvals(persona_name):
    plan = _plan(approved_ids={"summary"})
    plan[0]["new"] = "Hand-edited text"  # manual PATCH edits must reach the model
    user = tailor.build_chat_user_message(
        fake_job(), plan, "Dear team, the current letter.", fake_content(), "  make it pop  "
    )
    assert "[summary] (approved) -> Hand-edited text" in user
    assert "[win-1] (not yet approved) -> Did **bold** tailored things" in user
    assert "Dear team, the current letter." in user
    assert f"Message from {persona_name}: make it pop" in user
    assert "EDITABLE IDS" in user  # full resume context rides along


def test_chat_user_message_with_empty_plan():
    user = tailor.build_chat_user_message(fake_job(), [], "Letter.", fake_content(), "hi")
    assert "(no resume changes planned)" in user


# ---------------------------------------------------------------- parse_chat_output


def test_parse_chat_defaults_optional_fields_and_strips():
    text = f"```json\n{json.dumps({'reply': ' Sure. ', 'cover_letter': '  '})}\n```"
    assert tailor.parse_chat_output(text) == {
        "reply": "Sure.", "changes": [], "remove": [], "cover_letter": None,
    }
    data = tailor.parse_chat_output(json.dumps(
        {"reply": "x", "changes": None, "remove": None, "cover_letter": "New letter."}
    ))
    assert data["changes"] == [] and data["remove"] == []
    assert data["cover_letter"] == "New letter."


@pytest.mark.parametrize("text, match", [
    ("no braces here", "no JSON object"),
    ('{"reply": "  "}', "reply"),
    ('{"changes": []}', "reply"),
    ('{"reply": "x", "changes": "nope"}', "changes"),
    ('{"reply": "x", "remove": "nope"}', "remove"),
    ('{"reply": "x", "cover_letter": 7}', "cover_letter"),
])
def test_parse_chat_rejects_bad_output(text, match):
    with pytest.raises(tailor.TailorError, match=match):
        tailor.parse_chat_output(text)


# ---------------------------------------------------------------- merge


def test_merge_revise_keeps_approved_old_and_order():
    plan = _plan(approved_ids={"win-1"})
    parsed = chat_output(changes=[
        {"id": "win-1", "new": "Softer rewrite", "rationale": "Less salesy."},
    ])
    merged, warnings = tailor.merge_chat_changes(plan, fake_content(), parsed)
    assert warnings == []
    assert [c["id"] for c in merged] == ["summary", "win-1"]
    win = merged[1]
    assert win["new"] == "Softer rewrite"
    assert win["approved"] is True  # the user asked for the edit; flag survives
    assert win["old"] == "Did **bold** things"  # drift guard intact
    assert win["rationale"] == "Less salesy."
    assert plan[1]["new"] == "Did **bold** tailored things"  # input not mutated


def test_merge_revise_without_rationale_keeps_the_old_one():
    plan = _plan()
    parsed = chat_output(changes=[{"id": "win-1", "new": "Softer rewrite"}])
    merged, _ = tailor.merge_chat_changes(plan, fake_content(), parsed)
    assert merged[1]["rationale"] == "Echoes the JD."


def test_merge_add_appends_unapproved_with_server_old():
    parsed = chat_output(changes=[
        {"id": "win-2", "new": "New tailored line", "rationale": "Also this."},
    ])
    merged, warnings = tailor.merge_chat_changes(_plan(), fake_content(), parsed)
    assert warnings == []
    assert [c["id"] for c in merged] == ["summary", "win-1", "win-2"]
    assert merged[2]["approved"] is False
    assert merged[2]["old"] == "Did *italic* things"


def test_merge_remove_drops_by_id_and_warns_on_unknown():
    parsed = chat_output(remove=["win-1", "ghost"])
    merged, warnings = tailor.merge_chat_changes(_plan(), fake_content(), parsed)
    assert [c["id"] for c in merged] == ["summary"]
    assert any("ghost" in w for w in warnings)


@pytest.mark.parametrize("change, warning", [
    ({"id": "nope", "new": "x"}, "unknown or read-only"),
    ({"id": "exp-1", "new": "x"}, "unknown or read-only"),
    ({"id": "win-1", "new": "   "}, "empty rewrite"),
    ({"id": "win-1", "new": 7}, "empty rewrite"),
    ("not a dict", "non-object"),
])
def test_merge_drops_bad_entries_with_warnings(change, warning):
    merged, warnings = tailor.merge_chat_changes([], fake_content(), chat_output(changes=[change]))
    assert merged == []
    assert any(warning in w for w in warnings)


def test_merge_drops_duplicates_within_a_turn():
    parsed = chat_output(changes=[
        {"id": "win-2", "new": "first"},
        {"id": "win-2", "new": "second"},
    ])
    merged, warnings = tailor.merge_chat_changes([], fake_content(), parsed)
    assert [c["new"] for c in merged] == ["first"]
    assert any("duplicate" in w for w in warnings)


def test_merge_revert_to_original_text_removes_the_change():
    plan = _plan(approved_ids={"win-1"})
    parsed = chat_output(changes=[{"id": "win-1", "new": "Did **bold** things"}])
    merged, warnings = tailor.merge_chat_changes(plan, fake_content(), parsed)
    assert [c["id"] for c in merged] == ["summary"]
    assert any("reverted" in w for w in warnings)
    # a no-op that was never planned just warns
    merged, warnings = tailor.merge_chat_changes(
        [], fake_content(), chat_output(changes=[{"id": "win-2", "new": "Did *italic* things"}])
    )
    assert merged == []
    assert any("no-op" in w for w in warnings)


def test_merge_respects_change_cap_but_still_revises_at_cap(monkeypatch):
    monkeypatch.setattr(tailor, "MAX_CHANGES", 2)
    plan = _plan()  # already at the cap of 2
    merged, warnings = tailor.merge_chat_changes(
        plan, fake_content(), chat_output(changes=[{"id": "win-2", "new": "x"}])
    )
    assert len(merged) == 2
    assert any("cap" in w for w in warnings)
    merged, warnings = tailor.merge_chat_changes(
        plan, fake_content(), chat_output(changes=[{"id": "win-1", "new": "revised at cap"}])
    )
    assert warnings == []
    assert next(c for c in merged if c["id"] == "win-1")["new"] == "revised at cap"


def test_merge_remove_then_change_same_id_is_a_fresh_readd():
    plan = _plan(approved_ids={"win-1"})
    parsed = chat_output(
        remove=["win-1"],
        changes=[{"id": "win-1", "new": "Fresh take", "rationale": "Redo."}],
    )
    merged, _ = tailor.merge_chat_changes(plan, fake_content(), parsed)
    win = next(c for c in merged if c["id"] == "win-1")
    assert win["new"] == "Fresh take"
    assert win["approved"] is False  # removed first, so this is an add


# ---------------------------------------------------------------- chat call


def test_chat_call_uses_chat_budget_and_retries_after_history():
    from types import SimpleNamespace

    replies = iter(["sorry, prose only", json.dumps(chat_output(reply="ok"))])
    state = {"calls": 0, "kwargs": None}

    async def create(**kwargs):
        state["calls"] += 1
        state["kwargs"] = kwargs
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=next(replies))])

    fake = SimpleNamespace(messages=SimpleNamespace(create=create))
    history = [
        {"role": "user", "content": "earlier ask"},
        {"role": "assistant", "content": "earlier reply"},
        {"role": "user", "content": "new turn with context"},
    ]
    data, _ = asyncio.run(tailor.chat(fake, "system", history))
    assert data["reply"] == "ok"
    assert state["calls"] == 2
    assert state["kwargs"]["max_tokens"] == tailor.CHAT_MAX_TOKENS
    messages = state["kwargs"]["messages"]
    assert messages[:3] == history  # corrective turns append after the thread
    assert messages[3]["content"] == "sorry, prose only"
    assert "could not be used" in messages[4]["content"]
