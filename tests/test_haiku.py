"""haiku.py — prompt construction and the fake-client scoring path."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from jshq.scoring import haiku
from jshq.scoring.criteria import NEUTRAL_DISPLAY_NAME, Criteria

# An 11-criterion rubric shaped like the shipped one (craft axis on 5,
# bonus-only on 11), stated explicitly rather than leaning on the legacy
# fallback — these tests assert on the prompt those markers produce.
ELEVEN = [
    {
        "text": f"Criterion {n}",
        "weight": 1.0,
        "craft": n == 5,
        "bonus_only": n == 11,
    }
    for n in range(1, 12)
]

CRITERIA = Criteria(
    params={},
    prose="THE CRITERIA PROSE",
    tier2=ELEVEN,
    craft_criterion=5,
    no_negative_criteria=frozenset({11}),
    craft_explicit=True,
)

def tier2(values: dict) -> list:
    """The model's per-criterion array from {criterion -> score|None}."""
    return [
        {"n": n, "v": values.get(n), "q": "" if values.get(n) is None else "quoted evidence"}
        for n in range(1, 12)
    ]


GOOD_SUBSCORES = {1: 2, 2: 1, 3: 2, 4: 2, 5: 2, 6: 1, 7: 0, 8: 0, 9: 1, 10: 0, 11: 0}

GOOD_PAYLOAD = {
    "tier2": tier2(GOOD_SUBSCORES),
    "fit_quadrant": "energizing_strength",
    "management_type": "people_leader",
    "function": "product",
    "leads_discipline": "design",
    "confidence": "high",
    "near_miss_flags": ["scope_gap"],
    "scoring_notes": "Strong mentoring focus.",
}


def fake_client(*payloads):
    """Client whose messages.create returns each payload (dict -> JSON text,
    str -> raw text) in sequence; counts calls."""
    state = {"calls": 0}

    async def create(**kwargs):
        state["kwargs"] = kwargs
        payload = payloads[min(state["calls"], len(payloads) - 1)]
        state["calls"] += 1
        text = json.dumps(payload) if isinstance(payload, dict) else payload
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    return client, state


def job(**overrides):
    base = {
        "title": "Director of Product Design",
        "company_name": "TestCo",
        "location": "Remote - US",
        "remote_type": "remote",
        "level_band": "director",
        "salary_min": 200000,
        "salary_max": 240000,
        "salary_stated": 1,
        "description_text": "Lead and mentor a team of designers.",
    }
    base.update(overrides)
    return base


def test_system_prompt_contains_prose_digest_and_output_spec():
    prompt = haiku.build_system_prompt(CRITERIA, "DIGEST LINES")
    assert "THE CRITERIA PROSE" in prompt
    assert "DIGEST LINES" in prompt
    assert "tier2" in prompt
    assert "management_type" in prompt and "confidence" in prompt
    # scoring_notes asks for scannable markdown (summary line + bullets), so the
    # job-detail Scoring section renders as a lead + list, not one prose block.
    assert "scoring_notes" in prompt
    assert "bullets" in prompt and "markdown" in prompt


def test_system_prompt_names_the_persona_from_the_criteria_doc():
    # The person the rubric is for is CONFIG, not code: display_name and
    # domain_label come from the doc's persona block, so no owner's name is
    # baked into the source (and none is sent to the API by an install that
    # names nobody).
    crit = Criteria(
        params={},
        prose="P",
        tier2=ELEVEN,
        persona={"display_name": "Sam Example", "domain_label": "widget-wrangling"},
    )
    prompt = haiku.build_system_prompt(crit, "")
    assert "Sam Example" in prompt
    assert "widget-wrangling job postings" in prompt
    assert "Chris" not in prompt


def test_system_prompt_uses_the_neutral_name_when_the_doc_names_nobody():
    prompt = haiku.build_system_prompt(CRITERIA, "")
    assert f"one specific person, {NEUTRAL_DISPLAY_NAME}." in prompt
    assert "Chris" not in prompt


def test_system_prompt_management_type_requires_people_outcomes():
    # The 2026-07 tightening: peer-level activities must be named as NOT
    # people leadership, and an explicit IC designation is categorical —
    # the wording that let "help with hiring" read as people_leader is gone.
    prompt = haiku.build_system_prompt(CRITERIA, "")
    assert "owning people outcomes" in prompt
    assert "NOT people leadership" in prompt
    assert "categorically ic" in prompt
    assert "Never infer people_leader from the title alone" in prompt


def test_system_prompt_function_check_disambiguates_product():
    # The function check (2026-07): leads_discipline is in the output spec and
    # 'product' is pinned to product MANAGEMENT — the design-fluent-PM
    # confusion, where a Director of Product managing PMs read as design
    # leadership.
    prompt = haiku.build_system_prompt(CRITERIA, "")
    assert "leads_discipline" in prompt
    assert "the discipline the role LEADS" in prompt
    assert "product MANAGEMENT" in prompt and "NOT product design" in prompt
    assert "it does not pass" in prompt  # unclear flags for review, never passes


def test_schema_requires_leads_discipline():
    schema = haiku.build_schema(CRITERIA)
    assert "leads_discipline" in schema["required"]
    assert schema["properties"]["leads_discipline"]["enum"] == list(
        CRITERIA.taxonomy["disciplines"]
    )


def test_system_prompt_injects_flag_vocabulary():
    crit = Criteria(
        params={}, prose="P", tier2=ELEVEN, adjustments={"convert_sell_undertone": 10}
    )
    prompt = haiku.build_system_prompt(crit, "")
    # canonical tokens = adjustments table ∪ tier1 names, so deductions bind
    assert "convert_sell_undertone" in prompt
    assert "comp_below_target" in prompt


def test_system_prompt_omits_empty_digest():
    assert "DIGEST" not in haiku.build_system_prompt(CRITERIA, "")


def test_user_message_fields_and_salary():
    msg = haiku.build_user_message(job())
    assert "Director of Product Design" in msg
    assert "TestCo" in msg
    assert "$200,000–$240,000 (stated)" in msg
    assert "Lead and mentor" in msg


def test_user_message_unstated_salary():
    msg = haiku.build_user_message(job(salary_stated=0, salary_min=None, salary_max=None))
    assert "not stated" in msg


def test_user_message_truncates_long_jd():
    msg = haiku.build_user_message(job(description_text="x" * 20000))
    assert "[truncated]" in msg
    assert len(msg) < 13000


def test_score_job_happy_path():
    client, state = fake_client(GOOD_PAYLOAD)
    data, _usage = asyncio.run(haiku.score_job(client, "SYSTEM", job()))
    # The model emits no score at all — only the per-criterion reads.
    assert "fit_score" not in data
    assert data["tier2"] == GOOD_SUBSCORES
    assert state["calls"] == 1
    assert state["kwargs"]["model"] == haiku.MODEL
    assert state["kwargs"]["output_config"]["format"]["schema"] == haiku.build_schema()


def test_score_job_returns_usage():
    async def create(**kwargs):
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(GOOD_PAYLOAD))],
            usage=SimpleNamespace(input_tokens=120, output_tokens=30),
        )

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    data, used = asyncio.run(haiku.score_job(client, "SYSTEM", job()))
    assert data["tier2"][5] == 2
    assert used.input_tokens == 120


def test_scoring_error_carries_failed_attempt_usages():
    # A job that fails parsing on BOTH attempts still made two billable calls;
    # ScoringError carries their usages so run_scoring can bill them (the cost
    # total used to silently drop the failed-parse subset).
    from jshq import usage

    async def create(**kwargs):
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(dict(GOOD_PAYLOAD, tier2=[])))],
            usage=SimpleNamespace(input_tokens=100, output_tokens=20),
        )

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    with pytest.raises(haiku.ScoringError) as ei:
        asyncio.run(haiku.score_job(client, "SYSTEM", job(), CRITERIA))
    billed = usage.usages_of(ei.value)
    assert len(billed) == 2  # both attempts
    assert all(u.input_tokens == 100 for u in billed)


def test_subscore_clamped_to_range():
    client, _ = fake_client(dict(GOOD_PAYLOAD, tier2=tier2({**GOOD_SUBSCORES, 1: 7})))
    data, _usage = asyncio.run(haiku.score_job(client, "SYSTEM", job()))
    assert data["tier2"][1] == haiku.SUB_MAX


def test_bonus_only_criterion_floored_at_zero():
    # Criterion 11 (AI) is "neutral when absent, never negative" per the doc —
    # enforced in code so a stray negative reads as 0 instead of penalizing.
    client, _ = fake_client(dict(GOOD_PAYLOAD, tier2=tier2({**GOOD_SUBSCORES, 11: -2})))
    data, _usage = asyncio.run(haiku.score_job(client, "SYSTEM", job()))
    assert data["tier2"][11] == 0


def test_null_craft_criterion_rejected():
    # The [craft] criterion is the central tension test; every posting has
    # responsibility verbs, so a null there is a hedge and must fail the retry
    # loop. CRITERIA marks criterion 5 explicitly.
    client, _ = fake_client(dict(GOOD_PAYLOAD, tier2=tier2({**GOOD_SUBSCORES, 5: None})))
    with pytest.raises(haiku.ScoringError):
        asyncio.run(haiku.score_job(client, "SYSTEM", job(), CRITERIA))


def test_null_craft_criterion_allowed_when_the_axis_was_only_inferred():
    # A marker-less 11-criterion doc gets craft=5 by legacy inference. The
    # author never designated it, so rejecting their honest null would leave the
    # job permanently unscored after the retry — the lean just reads 0.
    inferred = Criteria(
        params={},
        prose="P",
        tier2=[{"text": f"Criterion {n}", "weight": 1.0} for n in range(1, 12)],
        craft_criterion=5,
        craft_explicit=False,
    )
    client, _ = fake_client(dict(GOOD_PAYLOAD, tier2=tier2({**GOOD_SUBSCORES, 5: None})))
    data, _usage = asyncio.run(haiku.score_job(client, "SYSTEM", job(), inferred))
    assert data["tier2"][5] is None


def test_null_subscore_preserved_for_other_criteria():
    client, _ = fake_client(dict(GOOD_PAYLOAD, tier2=tier2({**GOOD_SUBSCORES, 8: None})))
    data, _usage = asyncio.run(haiku.score_job(client, "SYSTEM", job()))
    # null must survive as null, distinct from 0 — code decides what silence costs.
    assert data["tier2"][8] is None and data["tier2"][7] == 0


def test_short_tier2_array_rejected():
    payload = dict(GOOD_PAYLOAD, tier2=tier2(GOOD_SUBSCORES)[:9])
    client, _ = fake_client(payload)
    with pytest.raises(haiku.ScoringError):
        asyncio.run(haiku.score_job(client, "SYSTEM", job()))


def test_retry_runs_warm_after_a_temp0_failure():
    """A temp-0 failure is deterministic for that exact request, so a verbatim
    retry re-buys the same bad output (seen live 2026-08-10: four identical
    short-tier2 responses across two invocations). Attempt 1 stays at
    TEMPERATURE for run-to-run stability; the retry must run at
    RETRY_TEMPERATURE so it can leave the degenerate mode."""
    temps = []
    payloads = [dict(GOOD_PAYLOAD, tier2=tier2(GOOD_SUBSCORES)[:2]), GOOD_PAYLOAD]
    state = {"calls": 0}

    async def create(**kwargs):
        temps.append(kwargs["temperature"])
        payload = payloads[min(state["calls"], len(payloads) - 1)]
        state["calls"] += 1
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(payload))])

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    data, _usage = asyncio.run(haiku.score_job(client, "SYSTEM", job()))
    assert temps == [haiku.TEMPERATURE, haiku.RETRY_TEMPERATURE]
    assert data["tier2"][1] == GOOD_SUBSCORES[1]


def test_duplicate_criterion_rejected():
    entries = tier2(GOOD_SUBSCORES)
    entries[3] = dict(entries[2])  # criterion 3 twice, criterion 4 missing
    client, _ = fake_client(dict(GOOD_PAYLOAD, tier2=entries))
    with pytest.raises(haiku.ScoringError):
        asyncio.run(haiku.score_job(client, "SYSTEM", job()))


def test_quotes_kept_alongside_subscores():
    client, _ = fake_client(GOOD_PAYLOAD)
    data, _usage = asyncio.run(haiku.score_job(client, "SYSTEM", job()))
    assert data["tier2_quotes"][5] == "quoted evidence"


def test_bad_output_retries_once_then_raises():
    client, state = fake_client("not json at all")
    with pytest.raises(haiku.ScoringError):
        asyncio.run(haiku.score_job(client, "SYSTEM", job()))
    assert state["calls"] == 2


def test_bad_first_output_good_second_succeeds():
    client, state = fake_client("garbage", GOOD_PAYLOAD)
    data, _usage = asyncio.run(haiku.score_job(client, "SYSTEM", job()))
    assert data["fit_quadrant"] == "energizing_strength"
    assert state["calls"] == 2


def test_out_of_enum_quadrant_rejected():
    client, _ = fake_client(dict(GOOD_PAYLOAD, fit_quadrant="vibes"))
    with pytest.raises(haiku.ScoringError):
        asyncio.run(haiku.score_job(client, "SYSTEM", job()))


def test_out_of_enum_management_type_rejected():
    client, _ = fake_client(dict(GOOD_PAYLOAD, management_type="freelancer"))
    with pytest.raises(haiku.ScoringError):
        asyncio.run(haiku.score_job(client, "SYSTEM", job()))


def test_out_of_enum_leads_discipline_rejected():
    client, _ = fake_client(dict(GOOD_PAYLOAD, leads_discipline="marketing"))
    with pytest.raises(haiku.ScoringError):
        asyncio.run(haiku.score_job(client, "SYSTEM", job()))


def test_schema_has_no_score_or_lean():
    # Both were removed (2026-08): the score is computed from the sub-scores and
    # craft_lean is derived from criterion 5, so neither can be anchored to.
    assert "fit_score" not in haiku.build_schema()["properties"]
    assert "craft_lean" not in haiku.build_schema()["properties"]
    assert "tier2" in haiku.build_schema()["required"]


def test_schema_orders_tier2_before_notes():
    # Structured outputs generate in schema order: extract per criterion first,
    # then summarize — not compose a narrative and back-fill sub-scores to it.
    keys = list(haiku.build_schema()["properties"])
    assert keys.index("tier2") < keys.index("scoring_notes")


def test_user_message_has_no_level_band_line():
    # The derived-band line anchored the temp-0 model to band-shaped scores
    # (the flat-72 pathology) — the title alone carries level now.
    assert "Level band" not in haiku.build_user_message(job())
