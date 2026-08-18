"""criteria.py — params block extraction, validation, cache, and the real doc."""

import json

import pytest

from jshq.scoring.criteria import (
    CRITERIA_PATH,
    NEUTRAL_DISPLAY_NAME,
    CriteriaError,
    load_criteria,
    parse_tier2,
    PERSONA_MAX_LEN,
    persona_display_name,
    read_editable,
    render_params_summary,
    render_tier2,
    write_criteria,
    write_persona,
)

VALID_PARAMS = {
    "comp_floor": 180000,
    "comp_target": 200000,
    "location_allowlist": ["evanston", "skokie"],
    "company_location_overrides": {"Exampleco Group": ["copenhagen", "london"]},
    "remote_regions": ["united states", "us", "anywhere"],
    "excluded_sectors": ["healthcare", "defense"],
    "target_title_bands": ["director", "senior_director"],
    "flag_title_bands": {"manager": "below_band", "vp_plus": "scope_gap"},
}


def write_doc(tmp_path, params=VALID_PARAMS, block=None):
    block = block if block is not None else f"```json tier1_params\n{json.dumps(params)}\n```"
    path = tmp_path / "fit_criteria.md"
    path.write_text(f"# Criteria\n\nIntro prose.\n\n{block}\n\nClosing prose.\n")
    return path


def test_parses_params_and_strips_block_from_prose(tmp_path):
    c = load_criteria(write_doc(tmp_path))
    assert c.params["comp_floor"] == 180000
    assert "Intro prose." in c.prose
    assert "Closing prose." in c.prose
    assert "tier1_params" not in c.prose


def test_missing_block_raises(tmp_path):
    with pytest.raises(CriteriaError, match="tier1_params"):
        load_criteria(write_doc(tmp_path, block="no fenced block here"))


def test_invalid_json_raises(tmp_path):
    with pytest.raises(CriteriaError, match="not valid JSON"):
        load_criteria(write_doc(tmp_path, block="```json tier1_params\n{nope}\n```"))


def test_missing_key_raises(tmp_path):
    params = {k: v for k, v in VALID_PARAMS.items() if k != "comp_floor"}
    with pytest.raises(CriteriaError, match="comp_floor"):
        load_criteria(write_doc(tmp_path, params))


def test_wrong_type_raises(tmp_path):
    params = dict(VALID_PARAMS, location_allowlist="evanston")
    with pytest.raises(CriteriaError, match="location_allowlist"):
        load_criteria(write_doc(tmp_path, params))


def test_missing_file_raises(tmp_path):
    with pytest.raises(CriteriaError, match="not found"):
        load_criteria(tmp_path / "absent.md")


def test_real_doc_parses():
    c = load_criteria(CRITERIA_PATH)
    assert c.params["comp_floor"] == 160000
    assert "gambling" in c.params["excluded_sectors"]
    assert "central tension" in c.prose.lower()


# --- location_radius (Phase 7i, optional key) ---------------------------


def test_real_doc_has_valid_radius():
    # Validates the live doc has a STRUCTURALLY sound location_radius block, not
    # the exact tuned values (the user adjusts radius/center/estimate over time).
    r = load_criteria(CRITERIA_PATH).params.get("location_radius")
    assert r is not None
    assert isinstance(r["center"]["lat"], (int, float))
    assert isinstance(r["center"]["lng"], (int, float))
    assert r["center"].get("label")
    assert isinstance(r["radius_minutes"], (int, float)) and r["radius_minutes"] > 0
    assert r["estimate"]["detour_factor"] > 0 and r["estimate"]["avg_mph"] > 0


def test_radius_absent_is_allowed(tmp_path):
    # location_radius is optional — a doc without it loads (pre-7i behavior).
    c = load_criteria(write_doc(tmp_path))  # VALID_PARAMS carries no radius
    assert c.params.get("location_radius") is None


def test_radius_null_is_allowed(tmp_path):
    c = load_criteria(write_doc(tmp_path, dict(VALID_PARAMS, location_radius=None)))
    assert c.params["location_radius"] is None


def test_valid_radius_loads(tmp_path):
    params = dict(
        VALID_PARAMS,
        location_radius={
            "center": {"lat": 39.1, "lng": -77.0},
            "radius_minutes": 25,
            "estimate": {"detour_factor": 1.5, "avg_mph": 30},
        },
    )
    c = load_criteria(write_doc(tmp_path, params))
    assert c.params["location_radius"]["radius_minutes"] == 25


def test_radius_loads_without_estimate(tmp_path):
    # estimate is optional — the gate falls back to its defaults.
    params = dict(
        VALID_PARAMS, location_radius={"center": {"lat": 39.1, "lng": -77.0}, "radius_minutes": 25}
    )
    assert load_criteria(write_doc(tmp_path, params)).params["location_radius"]["radius_minutes"] == 25


def test_radius_missing_center_coord_raises(tmp_path):
    params = dict(VALID_PARAMS, location_radius={"center": {"lat": 39.1}, "radius_minutes": 25})
    with pytest.raises(CriteriaError, match="location_radius"):
        load_criteria(write_doc(tmp_path, params))


def test_radius_nonpositive_minutes_raises(tmp_path):
    params = dict(
        VALID_PARAMS, location_radius={"center": {"lat": 39.1, "lng": -77.0}, "radius_minutes": 0}
    )
    with pytest.raises(CriteriaError, match="radius_minutes"):
        load_criteria(write_doc(tmp_path, params))


def test_radius_bad_estimate_factor_raises(tmp_path):
    params = dict(
        VALID_PARAMS,
        location_radius={
            "center": {"lat": 39.1, "lng": -77.0},
            "radius_minutes": 25,
            "estimate": {"avg_mph": 0},
        },
    )
    with pytest.raises(CriteriaError, match="avg_mph"):
        load_criteria(write_doc(tmp_path, params))


def test_radius_not_object_raises(tmp_path):
    with pytest.raises(CriteriaError, match="location_radius"):
        load_criteria(write_doc(tmp_path, dict(VALID_PARAMS, location_radius="30 minutes")))


def test_cache_uses_mtime_for_default_path():
    first = load_criteria(CRITERIA_PATH)
    second = load_criteria(CRITERIA_PATH)
    assert first is second  # same mtime → cached object


def test_criteria_doc_endpoint_serves_the_real_doc(client):
    response = client.get("/api/scoring/criteria-doc")
    assert response.status_code == 200
    markdown = response.json()["markdown"]
    assert "Tier 1" in markdown
    # The generated plain-English summary is spliced in...
    assert "Current Tier 1 settings" in markdown
    assert "**Compensation**" in markdown
    # ...ahead of the raw block, which stays in the text (the viewer collapses it).
    assert "```json tier1_params" in markdown


def test_render_params_summary_reads_as_prose():
    summary = render_params_summary(
        {**VALID_PARAMS, "location_radius": {"center": {"label": "Evanston, IL"}, "radius_minutes": 45}}
    )
    assert "### Current Tier 1 settings" in summary
    assert "$180,000" in summary and "target $200,000" in summary  # thousands + target
    assert "45 minutes' drive of Evanston, IL" in summary
    assert "2 towns (evanston, skokie)" in summary
    assert "per-company exceptions for Exampleco Group" in summary
    assert "senior director" in summary  # snake_case humanized
    assert "healthcare, defense" in summary
    assert "vp plus" in summary  # flagged band keys humanized
    assert "```" not in summary  # plain markdown bullets, never a raw fence


def test_render_params_summary_omits_commute_without_radius():
    # location_radius is optional; VALID_PARAMS carries none → no commute line.
    summary = render_params_summary(VALID_PARAMS)
    assert "Commute" not in summary
    assert "**Compensation**" in summary


# --- Tier 2 weights (Phase 8) -------------------------------------------


def _wrap_tier2(body: str) -> str:
    return f"<!-- tier2:start -->\n{body}\n<!-- tier2:end -->"


def _item(text, weight=1.0, craft=False, bonus_only=False):
    """The full parsed shape, so these tests state every field they expect."""
    return {"text": text, "weight": weight, "craft": craft, "bonus_only": bonus_only}


def test_parse_tier2_defaults_weight_when_absent():
    items = parse_tier2(_wrap_tier2("1. First item\n2. Second item"))
    assert items == [_item("First item"), _item("Second item")]


def test_parse_tier2_reads_inline_weight():
    items = parse_tier2(_wrap_tier2("1. Heavy [w: 2]\n2. Light [w: 0.5]"))
    assert items == [_item("Heavy", 2.0), _item("Light", 0.5)]


def test_parse_tier2_tolerates_whitespace_in_weight_token():
    # a hand edit with a stray space before `]` still parses (the doc is the
    # editable source of truth; the app's renderer never emits inner spaces)
    items = parse_tier2(_wrap_tier2("1. Heavy [w: 2 ]"))
    assert items == [_item("Heavy", 2.0)]


def test_parse_tier2_clamps_out_of_range_weight():
    items = parse_tier2(_wrap_tier2("1. Too big [w: 9]\n2. Too small [w: 0.01]"))
    assert items[0]["weight"] == 4.0
    assert items[1]["weight"] == 0.25


def test_parse_tier2_keeps_bracketed_text_that_is_not_a_weight():
    # only a trailing `[w: N]` is a weight token — other brackets stay in the text
    items = parse_tier2(_wrap_tier2("1. Has [brackets] but no weight"))
    assert items == [_item("Has [brackets] but no weight")]


# --- [craft] / [bonus] attribute markers (Phase 2) -----------------------


def test_parse_tier2_reads_attribute_markers():
    items = parse_tier2(
        _wrap_tier2("1. Axis [craft] [w: 2.5]\n2. Extra [bonus]\n3. Plain")
    )
    assert items == [
        _item("Axis", 2.5, craft=True),
        _item("Extra", bonus_only=True),
        _item("Plain"),
    ]


def test_parse_tier2_tolerates_marker_order():
    # The doc is hand-editable; only the renderer emits the canonical order.
    assert parse_tier2(_wrap_tier2("1. Axis [w: 2] [craft]")) == [
        _item("Axis", 2.0, craft=True)
    ]


def test_render_tier2_emits_canonical_marker_order():
    rendered = render_tier2([_item("Axis", 2.5, craft=True, bonus_only=True)])
    assert rendered == "1. Axis [craft] [bonus] [w: 2.5]"
    # and round-trips
    assert parse_tier2(_wrap_tier2(rendered)) == [
        _item("Axis", 2.5, craft=True, bonus_only=True)
    ]


def test_render_tier2_omits_markers_at_their_defaults():
    # The byte-stable no-op-write contract: an unmarked doc stays unmarked.
    assert render_tier2([_item("Plain")]) == "1. Plain"


def test_two_craft_markers_raise(tmp_path):
    # craft_lean derives from exactly one criterion; two would make the tension
    # label depend on parse order.
    path = tmp_path / "fit_criteria.md"
    path.write_text(
        f"```json tier1_params\n{json.dumps(VALID_PARAMS)}\n```\n"
        + _wrap_tier2("1. One [craft]\n2. Two [craft]")
    )
    with pytest.raises(CriteriaError, match=r"\[craft\]"):
        load_criteria(path)


def _tier2_doc(tmp_path, lines):
    path = tmp_path / "fit_criteria.md"
    path.write_text(
        f"```json tier1_params\n{json.dumps(VALID_PARAMS)}\n```\n" + _wrap_tier2(lines)
    )
    return path


@pytest.mark.parametrize(
    "line",
    [
        "1. Axis [craft].",  # trailing punctuation
        "1. Axis **[craft]**",  # wrapped in emphasis
        "1. Axis [craft] and more text",  # mid-line
        "1. Axis [bonus]!",
    ],
)
def test_a_marker_that_is_not_at_the_end_of_the_line_fails_loud(tmp_path, line):
    # Left as ordinary text this is indistinguishable from a doc with no markers,
    # which would silently re-arm the legacy craft=5 fallback and point the lean
    # at the wrong criterion on every job.
    with pytest.raises(CriteriaError, match="not at the end"):
        load_criteria(_tier2_doc(tmp_path, line))


def test_marker_tolerates_inner_spaces_like_the_weight_token(tmp_path):
    # The doc is hand-editable and `[w: 2 ]` has always parsed; `[ craft ]` must
    # too, or a stray space silently changes the rubric's shape.
    c = load_criteria(_tier2_doc(tmp_path, "1. Axis [ craft ]\n2. Extra [ bonus ]"))
    assert c.craft_criterion == 1
    assert c.no_negative_criteria == frozenset({2})


def test_craft_and_bonus_on_the_same_criterion_raise(tmp_path):
    # The bonus clamp floors the axis at 0, so the lean could never go negative
    # and the convert/sell tension label would be unreachable.
    with pytest.raises(CriteriaError, match="both"):
        load_criteria(_tier2_doc(tmp_path, "1. Axis [craft] [bonus]"))


def test_double_weight_token_keeps_the_trailing_one(tmp_path):
    # Behavior parity with the single-strip parser this replaced.
    items = parse_tier2(_wrap_tier2("1. Heavy [w: 2] [w: 3]"))
    assert items[0]["weight"] == 3.0


def test_markerless_eleven_criterion_doc_keeps_its_old_meaning(tmp_path):
    # BACK-COMPAT: a doc written before Phase 2 has no markers. It was written
    # against the shipped 11-criterion rubric, whose craft axis is 5 and whose
    # bonus-only criterion is 11, so it must keep behaving identically.
    lines = "\n".join(f"{i}. Criterion {i}" for i in range(1, 12))
    path = tmp_path / "fit_criteria.md"
    path.write_text(
        f"```json tier1_params\n{json.dumps(VALID_PARAMS)}\n```\n" + _wrap_tier2(lines)
    )
    c = load_criteria(path)
    assert c.tier2_count == 11
    assert c.craft_criterion == 5
    assert c.no_negative_criteria == frozenset({11})


def test_markerless_doc_of_another_length_gets_no_craft_axis(tmp_path):
    # No markers and not the legacy shape: guessing a position would mis-derive
    # every tension label, so the axis is simply absent and the lean is 0.
    lines = "\n".join(f"{i}. Criterion {i}" for i in range(1, 8))
    path = tmp_path / "fit_criteria.md"
    path.write_text(
        f"```json tier1_params\n{json.dumps(VALID_PARAMS)}\n```\n" + _wrap_tier2(lines)
    )
    c = load_criteria(path)
    assert c.tier2_count == 7
    assert c.craft_criterion is None
    assert c.no_negative_criteria == frozenset()


def test_a_doc_with_phase2_blocks_never_gets_legacy_positions(tmp_path):
    # The 11-item positional fallback exists for pre-Phase-2 docs, which have
    # none of the Phase-2 machine blocks. A wizard/starter-derived doc always
    # carries a persona block, so a user who happens to rank exactly 11 wishes
    # must NOT silently inherit craft=5 / bonus-only=11 from a rubric they
    # never saw.
    lines = "\n".join(f"{i}. Criterion {i}" for i in range(1, 12))
    path = tmp_path / "fit_criteria.md"
    path.write_text(
        '```json persona\n{"display_name": null, "domain_label": "roles"}\n```\n'
        f"```json tier1_params\n{json.dumps(VALID_PARAMS)}\n```\n" + _wrap_tier2(lines)
    )
    c = load_criteria(path)
    assert c.tier2_count == 11
    assert c.craft_criterion is None
    assert c.no_negative_criteria == frozenset()


def test_a_doc_using_markers_does_not_fall_back_to_the_legacy_positions(tmp_path):
    # Markers are in use, so the author simply declared no craft axis. Falling
    # back to criterion 5 here would override an explicit choice.
    lines = "\n".join(f"{i}. Criterion {i}" for i in range(1, 12))
    lines = lines.replace("7. Criterion 7", "7. Criterion 7 [bonus]")
    path = tmp_path / "fit_criteria.md"
    path.write_text(
        f"```json tier1_params\n{json.dumps(VALID_PARAMS)}\n```\n" + _wrap_tier2(lines)
    )
    c = load_criteria(path)
    assert c.craft_criterion is None
    assert c.no_negative_criteria == frozenset({7})


def test_reordering_moves_the_marker_with_its_criterion(tmp_path):
    # The whole reason the markers are inline tokens rather than a position-keyed
    # block: the Settings editor reorders this list, and a craft axis that stayed
    # behind would silently start measuring a different criterion.
    src = tmp_path / "fit_criteria.md"
    src.write_text(
        f"```json tier1_params\n{json.dumps(VALID_PARAMS)}\n```\n"
        + _wrap_tier2("1. One\n2. Two\n3. Three")
    )
    params, tier2 = read_editable(src)
    tier2[0]["craft"] = True
    write_criteria(params, tier2, src)
    assert load_criteria(src).craft_criterion == 1
    # the editor's "move down" on the first row
    params, tier2 = read_editable(src)
    write_criteria(params, [tier2[1], tier2[0]] + tier2[2:], src)
    assert load_criteria(src).craft_criterion == 2


def test_render_tier2_omits_default_weight_suffix():
    assert render_tier2([{"text": "Plain", "weight": 1.0}]) == "1. Plain"


def test_render_tier2_emits_nondefault_weight():
    rendered = render_tier2([{"text": "Heavy", "weight": 2.0}, {"text": "Half", "weight": 0.5}])
    assert rendered == "1. Heavy [w: 2]\n2. Half [w: 0.5]"


def test_render_tier2_accepts_legacy_strings():
    assert render_tier2(["Bare string"]) == "1. Bare string"


def test_tier2_round_trips_through_parse_and_render():
    body = "1. First\n2. Weighted [w: 1.5]\n3. Third"
    items = parse_tier2(_wrap_tier2(body))
    assert render_tier2(items) == body


# --- score_adjustments block (scoring redesign) ---


def _doc_with_adjustments(tmp_path, table_json):
    path = tmp_path / "fit_criteria.md"
    path.write_text(
        "# Criteria\n\nProse before.\n\n"
        f"```json tier1_params\n{json.dumps(VALID_PARAMS)}\n```\n\n"
        f"```json score_adjustments\n{table_json}\n```\n\nProse after.\n"
    )
    return path


def test_adjustments_parse_happy_path(tmp_path):
    c = load_criteria(_doc_with_adjustments(tmp_path, '{"scope_gap": 8, "pace_unclear": 4}'))
    assert c.adjustments == {"scope_gap": 8, "pace_unclear": 4}


def test_adjustments_absent_defaults_empty(tmp_path):
    c = load_criteria(write_doc(tmp_path))
    assert c.adjustments == {}


def test_adjustments_block_stripped_from_prose(tmp_path):
    c = load_criteria(_doc_with_adjustments(tmp_path, '{"scope_gap": 8}'))
    assert "score_adjustments" not in c.prose
    assert '"scope_gap"' not in c.prose  # point values must never reach the model
    assert "Prose before." in c.prose and "Prose after." in c.prose


def test_adjustments_non_int_value_raises(tmp_path):
    with pytest.raises(CriteriaError):
        load_criteria(_doc_with_adjustments(tmp_path, '{"scope_gap": "big"}'))


def test_adjustments_out_of_range_raises(tmp_path):
    with pytest.raises(CriteriaError):
        load_criteria(_doc_with_adjustments(tmp_path, '{"scope_gap": 26}'))


def test_adjustments_non_object_raises(tmp_path):
    with pytest.raises(CriteriaError):
        load_criteria(_doc_with_adjustments(tmp_path, '[1, 2]'))


def test_real_doc_adjustments_parse():
    # Structure only, not values — the user tunes the table (criteria-destriction rule).
    c = load_criteria(CRITERIA_PATH)
    assert isinstance(c.adjustments, dict) and len(c.adjustments) > 0
    assert all(isinstance(v, int) and 0 <= v <= 25 for v in c.adjustments.values())


# --- score_caps block (IC hard cap) ---


def _doc_with_caps(tmp_path, table_json):
    path = tmp_path / "fit_criteria.md"
    path.write_text(
        "# Criteria\n\nProse before.\n\n"
        f"```json tier1_params\n{json.dumps(VALID_PARAMS)}\n```\n\n"
        f"```json score_caps\n{table_json}\n```\n\nProse after.\n"
    )
    return path


def test_caps_parse_happy_path(tmp_path):
    c = load_criteria(_doc_with_caps(tmp_path, '{"ic": 55}'))
    assert c.caps == {"ic": 55}


def test_caps_accept_function_check_keys(tmp_path):
    # Function check (2026-07): wrong_function / function_unclear key on the
    # model's leads_discipline read rather than management_type.
    c = load_criteria(
        _doc_with_caps(tmp_path, '{"ic": 55, "wrong_function": 20, "function_unclear": 55}')
    )
    assert c.caps == {"ic": 55, "wrong_function": 20, "function_unclear": 55}


def test_caps_accept_junior_band_key(tmp_path):
    # Band caps (2026-08): junior keys on the deterministic level band.
    c = load_criteria(_doc_with_caps(tmp_path, '{"ic": 55, "junior": 25}'))
    assert c.caps == {"ic": 55, "junior": 25}


def test_caps_absent_defaults_empty(tmp_path):
    c = load_criteria(write_doc(tmp_path))
    assert c.caps == {}


def test_caps_block_stripped_from_prose(tmp_path):
    c = load_criteria(_doc_with_caps(tmp_path, '{"ic": 55}'))
    assert "score_caps" not in c.prose
    assert '"ic"' not in c.prose  # cap values must never reach the model
    assert "Prose before." in c.prose and "Prose after." in c.prose


def test_caps_uncappable_key_raises(tmp_path):
    # people_leader must never carry a cap; a typo like "IC" must fail loudly.
    with pytest.raises(CriteriaError, match="not cappable"):
        load_criteria(_doc_with_caps(tmp_path, '{"people_leader": 55}'))
    with pytest.raises(CriteriaError, match="not cappable"):
        load_criteria(_doc_with_caps(tmp_path, '{"IC": 55}'))


def test_caps_non_int_value_raises(tmp_path):
    with pytest.raises(CriteriaError):
        load_criteria(_doc_with_caps(tmp_path, '{"ic": "low"}'))


def test_caps_out_of_range_raises(tmp_path):
    with pytest.raises(CriteriaError):
        load_criteria(_doc_with_caps(tmp_path, '{"ic": 101}'))


def test_caps_non_object_raises(tmp_path):
    with pytest.raises(CriteriaError):
        load_criteria(_doc_with_caps(tmp_path, '[55]'))


def test_real_doc_caps_parse():
    # Structure only, not values — the user tunes the ceiling in the doc.
    c = load_criteria(CRITERIA_PATH)
    assert "ic" in c.caps
    assert "wrong_function" in c.caps and "function_unclear" in c.caps
    assert all(isinstance(v, int) and 0 <= v <= 100 for v in c.caps.values())


def test_real_doc_prose_carries_function_check():
    # The function-check section must reach the model (it is prose, not a
    # machine block), including the vocabulary rule that design-adjacent
    # language only counts when the role owns design headcount.
    c = load_criteria(CRITERIA_PATH)
    assert "## Function check" in c.prose
    assert "design reports into the role" in c.prose


# --- score_scale block (sub-score aggregation, 2026-08) ---


def _doc_with_scale(tmp_path, table_json, tier2="1. First\n2. Second\n3. Third"):
    path = tmp_path / "fit_criteria.md"
    path.write_text(
        "# Criteria\n\nProse before.\n\n"
        f"```json tier1_params\n{json.dumps(VALID_PARAMS)}\n```\n\n"
        f"<!-- tier2:start -->\n{tier2}\n<!-- tier2:end -->\n\n"
        f"```json score_scale\n{table_json}\n```\n\nProse after.\n"
    )
    return path


def test_scale_parse_happy_path(tmp_path):
    c = load_criteria(_doc_with_scale(tmp_path, '{"slope": 2, "intercept": 40, '
                                                '"silence": {"1": -1, "3": -0.5}}'))
    # silence keys normalize to ints — they index the Tier 2 list
    assert c.scale == {"slope": 2.0, "intercept": 40.0, "silence": {1: -1.0, 3: -0.5}}


def test_scale_absent_defaults(tmp_path):
    c = load_criteria(write_doc(tmp_path))
    assert c.scale["silence"] == {}
    assert c.scale["slope"] > 0


def test_scale_block_stripped_from_prose(tmp_path):
    # Same reason as caps/adjustments: shown the silence values, the model would
    # pre-apply them instead of honestly reporting a criterion unevidenced.
    c = load_criteria(_doc_with_scale(tmp_path, '{"slope": 2, "intercept": 40}'))
    assert "Prose before." in c.prose and "Prose after." in c.prose
    assert '"slope"' not in c.prose


def test_scale_rejects_silence_key_outside_the_tier2_list(tmp_path):
    # Guards the one real trap: reordering or shortening the Tier 2 list
    # silently re-points positional silence keys.
    with pytest.raises(CriteriaError, match="outside the Tier 2 list"):
        load_criteria(_doc_with_scale(tmp_path, '{"silence": {"9": -1}}'))


def test_scale_rejects_out_of_range_silence(tmp_path):
    with pytest.raises(CriteriaError):
        load_criteria(_doc_with_scale(tmp_path, '{"silence": {"1": -9}}'))


def test_scale_rejects_non_numeric_and_unknown_keys(tmp_path):
    with pytest.raises(CriteriaError):
        load_criteria(_doc_with_scale(tmp_path, '{"slope": "fast"}'))
    with pytest.raises(CriteriaError):
        load_criteria(_doc_with_scale(tmp_path, '{"intercept": 400}'))
    with pytest.raises(CriteriaError, match="unknown keys"):
        load_criteria(_doc_with_scale(tmp_path, '{"slop": 2}'))


def test_scale_rejects_invalid_json(tmp_path):
    with pytest.raises(CriteriaError, match="not valid JSON"):
        load_criteria(_doc_with_scale(tmp_path, "{not json}"))


def test_real_doc_scale_and_weights_are_consistent():
    """The live doc's aggregation inputs. tier2 rides on Criteria so the scorer,
    the write path and calibrate_scoring.py all read one list."""
    c = load_criteria(CRITERIA_PATH)
    assert len(c.tier2) == 11
    assert all(item["weight"] > 0 for item in c.tier2)
    # every silence key indexes a real criterion
    assert all(1 <= n <= len(c.tier2) for n in c.scale["silence"])
    # criterion 5 (the central tension test) carries the most weight
    weights = [item["weight"] for item in c.tier2]
    assert weights[4] == max(weights)
    # mission silence is deliberately 0 (absent, not declared) — a negative
    # would penalize every company that simply harms no one
    assert c.scale["silence"].get(4, 0) == 0


def test_the_prompt_asks_for_exactly_the_criteria_the_doc_defines():
    """The count haiku asks the model for used to be a hand-maintained constant.
    If it and the doc's list diverge, criteria past the model's count sit
    permanently unevidenced, quietly taking their silence value on every job.

    Asserted against the rendered PROMPT rather than against tier2_contract's
    return value, which would just restate the function's own body."""
    from jshq.scoring import haiku

    c = load_criteria(CRITERIA_PATH)
    prompt = haiku.build_system_prompt(c, "")
    assert f"array of {c.tier2_count} objects" in prompt
    assert f"Criterion {c.craft_criterion} is never null" in prompt
    # every bonus-only criterion is named, not just the lowest
    for n in c.no_negative_criteria:
        assert f"{n}" in prompt.split("never negative")[0].rsplit("\n", 1)[-1]


def test_shipped_doc_marks_its_craft_and_bonus_criteria():
    # The shipped rubric's central tension test is criterion 5 and its AI
    # criterion (11) is bonus-only. Before Phase 2 those positions lived in
    # haiku.py; the markers in the doc are now the only statement of them.
    c = load_criteria(CRITERIA_PATH)
    assert c.craft_criterion == 5
    assert c.no_negative_criteria == frozenset({11})
    assert c.tier2[4]["craft"] is True
    assert c.tier2[10]["bonus_only"] is True


# --- persona block (Phase 2, optional) ----------------------------------


def _doc_with_persona(tmp_path, table_json):
    path = tmp_path / "fit_criteria.md"
    path.write_text(
        "# Criteria\n\nProse before.\n\n"
        f"```json tier1_params\n{json.dumps(VALID_PARAMS)}\n```\n\n"
        f"```json persona\n{table_json}\n```\n\nProse after.\n"
    )
    return path


def test_persona_parse_happy_path(tmp_path):
    c = load_criteria(
        _doc_with_persona(tmp_path, '{"display_name": "Sam Okonkwo", "domain_label": "data-science"}')
    )
    assert c.display_name == "Sam Okonkwo"
    assert c.domain_label == "data-science"


def test_persona_absent_gets_neutral_wording(tmp_path):
    # A doc without a persona block prompts like a fresh install: neutral name,
    # the starter doc's field-neutral domain phrase. (Until Phase 5b this
    # defaulted to the upstream owner's "design-leadership" — a doc that named
    # no field silently steered every scoring call toward design.)
    c = load_criteria(write_doc(tmp_path))  # VALID_PARAMS doc carries no persona
    assert c.display_name == NEUTRAL_DISPLAY_NAME == "the candidate"
    assert c.domain_label == "the roles you are searching for"


def test_persona_null_display_name_is_legal(tmp_path):
    # An explicit null means "name nobody" — a valid choice, not a broken doc.
    c = load_criteria(_doc_with_persona(tmp_path, '{"display_name": null}'))
    assert c.display_name == NEUTRAL_DISPLAY_NAME
    assert c.domain_label == "the roles you are searching for"


def test_persona_invalid_json_raises(tmp_path):
    with pytest.raises(CriteriaError, match="not valid JSON"):
        load_criteria(_doc_with_persona(tmp_path, "{not json}"))


def test_persona_non_object_raises(tmp_path):
    with pytest.raises(CriteriaError, match="JSON object"):
        load_criteria(_doc_with_persona(tmp_path, '["Sam Okonkwo"]'))


def test_persona_unknown_key_raises(tmp_path):
    # A typo like "name" would otherwise leave every prompt silently anonymous.
    with pytest.raises(CriteriaError, match="unknown keys"):
        load_criteria(_doc_with_persona(tmp_path, '{"name": "Sam Okonkwo"}'))


def test_persona_empty_display_name_raises(tmp_path):
    # "" and "   " are edits in progress, not a deliberate null — fail loud.
    with pytest.raises(CriteriaError, match="display_name"):
        load_criteria(_doc_with_persona(tmp_path, '{"display_name": ""}'))
    with pytest.raises(CriteriaError, match="display_name"):
        load_criteria(_doc_with_persona(tmp_path, '{"display_name": "   "}'))


def test_persona_non_string_domain_label_raises(tmp_path):
    with pytest.raises(CriteriaError, match="domain_label"):
        load_criteria(_doc_with_persona(tmp_path, '{"domain_label": 7}'))


def test_persona_block_stripped_from_prose(tmp_path):
    # Same rule as caps/adjustments/scale: the prose goes to the model verbatim,
    # so the raw machine block must never appear in it.
    c = load_criteria(_doc_with_persona(tmp_path, '{"display_name": "Sam Okonkwo"}'))
    assert '"display_name"' not in c.prose
    assert "```json persona" not in c.prose
    assert "Prose before." in c.prose and "Prose after." in c.prose


def test_real_doc_names_the_example_persona():
    # The shipped doc names the fictional example persona rather than leaving the
    # prompts anonymous, and no real name from the upstream project survives in
    # the prose the model reads (CLAUDE.md: no personal data).
    c = load_criteria(CRITERIA_PATH)
    assert c.display_name and c.display_name != NEUTRAL_DISPLAY_NAME
    assert c.domain_label
    assert "Chris" not in c.prose
    assert '"display_name"' not in c.prose


def test_persona_display_name_helper_reads_the_doc(tmp_path):
    # The prompt builders that don't otherwise load criteria go through this.
    assert (
        persona_display_name(_doc_with_persona(tmp_path, '{"display_name": "Sam Okonkwo"}'))
        == "Sam Okonkwo"
    )


def test_persona_display_name_helper_degrades_on_a_broken_doc(tmp_path):
    # Naming is cosmetic: a criteria typo must not take drafting down with it.
    # Scoring still fails loud on the very same doc.
    path = _doc_with_persona(tmp_path, '{"display_name": ')
    with pytest.raises(CriteriaError):
        load_criteria(path)
    assert persona_display_name(path) == NEUTRAL_DISPLAY_NAME


def test_persona_display_name_rejects_a_double_quote(tmp_path):
    # The name is interpolated into the JSON shape examples the tailoring
    # prompts show the model. A quote would hand it a malformed example of the
    # exact format it is being asked to reproduce.
    with pytest.raises(CriteriaError, match="double quote"):
        load_criteria(_doc_with_persona(tmp_path, '{"display_name": "Bo \\" Vance"}'))


def test_persona_values_must_be_single_line_and_bounded(tmp_path):
    # Both values are pasted into every prompt: a newline breaks the prompt's
    # block structure, and a pasted paragraph costs tokens on every call.
    with pytest.raises(CriteriaError, match="single line"):
        load_criteria(_doc_with_persona(tmp_path, '{"display_name": "Sam\\nOkonkwo"}'))
    long_name = json.dumps("S" * (PERSONA_MAX_LEN + 1))
    with pytest.raises(CriteriaError, match="at most"):
        load_criteria(_doc_with_persona(tmp_path, f'{{"display_name": {long_name}}}'))


# --- write_persona (Phase 3) --------------------------------------------


def test_write_persona_replaces_block_and_keeps_the_rest(tmp_path):
    path = _doc_with_persona(tmp_path, '{"display_name": "Old Name", "domain_label": "old-label"}')
    c = write_persona("New Name", "new-label", path)
    assert c.display_name == "New Name"
    assert c.domain_label == "new-label"
    text = path.read_text(encoding="utf-8")
    # Everything outside the block survives; the old values are gone.
    assert "Prose before." in text and "Prose after." in text
    assert "Old Name" not in text and "old-label" not in text
    # And the tier1_params block is untouched.
    assert load_criteria(path).params["comp_floor"] == VALID_PARAMS["comp_floor"]


def test_write_persona_null_name_round_trips(tmp_path):
    path = _doc_with_persona(tmp_path, '{"display_name": "Someone", "domain_label": "x"}')
    c = write_persona(None, "x", path)
    assert c.display_name == NEUTRAL_DISPLAY_NAME  # anonymous → "the candidate"
    assert '"display_name": null' in path.read_text(encoding="utf-8")


def test_write_persona_inserts_block_when_absent(tmp_path):
    path = write_doc(tmp_path)  # a params-only doc, no persona block
    assert "```json persona" not in path.read_text(encoding="utf-8")
    c = write_persona("Fresh Name", "fresh-label", path)
    assert c.display_name == "Fresh Name"
    text = path.read_text(encoding="utf-8")
    assert "```json persona" in text
    # Inserted above the params fence, and the params still parse.
    assert text.index("```json persona") < text.index("```json tier1_params")
    assert load_criteria(path).params["comp_floor"] == VALID_PARAMS["comp_floor"]


def test_write_persona_rejects_invalid_and_leaves_doc_untouched(tmp_path):
    path = _doc_with_persona(tmp_path, '{"display_name": "Keep Me", "domain_label": "keep"}')
    before = path.read_text(encoding="utf-8")
    with pytest.raises(CriteriaError, match="double quote"):
        write_persona('Bo " Vance', "role", path)
    with pytest.raises(CriteriaError, match="at most"):
        write_persona("S" * (PERSONA_MAX_LEN + 1), "role", path)
    assert path.read_text(encoding="utf-8") == before  # atomic: nothing changed
    assert not path.with_name(path.name + ".tmp").exists()  # temp cleaned up


def test_html_comments_never_reach_the_prose(tmp_path):
    # HTML comments are author guidance, addressed to the USER. The prose goes
    # to the model verbatim, so a note next to the persona block explaining that
    # a null name "keeps the prompts anonymous" would otherwise tell the scorer
    # the name it was just handed is a placeholder.
    path = tmp_path / "fit_criteria.md"
    path.write_text(
        "# Criteria\n\n"
        "<!--\nSet display_name to null to stay anonymous.\n-->\n\n"
        f"```json tier1_params\n{json.dumps(VALID_PARAMS)}\n```\n\n"
        "Real prose the scorer should read.\n\n"
        "<!-- tier2:start -->\n1. **First criterion** — text.\n<!-- tier2:end -->\n",
        encoding="utf-8",  # the em-dash must not become cp1252 on Windows
    )
    c = load_criteria(path)
    assert "stay anonymous" not in c.prose
    assert "<!--" not in c.prose
    # The tier2 markers are comments too, but the criteria BETWEEN them survive.
    assert "First criterion" in c.prose
    assert "Real prose the scorer should read." in c.prose


# --- taxonomy block (Phase 2) -------------------------------------------


def _doc_with_taxonomy(tmp_path, table_json):
    path = tmp_path / "fit_criteria.md"
    path.write_text(
        "# Criteria\n\nProse before.\n\n"
        f"```json tier1_params\n{json.dumps(VALID_PARAMS)}\n```\n\n"
        f"```json taxonomy\n{table_json}\n```\n\n"
        # a rubric with no criteria cannot build a prompt, so give it one
        + _wrap_tier2("1. Only criterion")
        + "\n\nProse after.\n"
    )
    return path


def test_taxonomy_absent_reproduces_the_hardcoded_vocabulary(tmp_path):
    # BACK-COMPAT: every value here was a code constant before Phase 2, so a doc
    # without the block must classify exactly as the old build did.
    from jshq.scoring.criteria import DEFAULT_DISCIPLINES, DEFAULT_FUNCTIONS

    c = load_criteria(write_doc(tmp_path))
    assert c.taxonomy["disciplines"] == DEFAULT_DISCIPLINES
    assert c.taxonomy["functions"] == DEFAULT_FUNCTIONS
    assert c.taxonomy["in_band_disciplines"] == ["design"]


def test_taxonomy_retargets_the_search_without_touching_code(tmp_path):
    """The whole point of the block. Before Phase 2 the in-band discipline was
    the literal string "design" in function_check_flag, so a non-design user had
    every job they saw hard-capped as wrong_function."""
    from jshq.scoring import function_check_flag, haiku, _in_band

    c = load_criteria(
        _doc_with_taxonomy(
            tmp_path,
            json.dumps(
                {
                    "disciplines": {
                        "engineering": "roles leading engineers",
                        "design": "roles leading designers",
                        "unclear": "thin evidence",
                    },
                    "in_band_disciplines": ["engineering"],
                    "functions": {"backend": "server systems", "frontend": ""},
                }
            ),
        )
    )
    assert function_check_flag("engineering", "people_leader", _in_band(c)) is None
    assert function_check_flag("design", "people_leader", _in_band(c)) == "wrong_function"
    # the schema the model is constrained by follows the doc too
    schema = haiku.build_schema(c)
    assert schema["properties"]["leads_discipline"]["enum"] == [
        "engineering",
        "design",
        "unclear",
    ]
    assert schema["properties"]["function"]["enum"] == ["backend", "frontend"]
    # and so does the prompt
    assert "This search is for 'engineering'" in haiku.build_system_prompt(c, "")


def test_taxonomy_requires_unclear(tmp_path):
    # function_unclear keys on it, and a thin read must always have somewhere
    # honest to land.
    with pytest.raises(CriteriaError, match="unclear"):
        load_criteria(
            _doc_with_taxonomy(
                tmp_path, json.dumps({"disciplines": {"design": "a", "other": "b"}})
            )
        )


def test_in_band_must_name_real_disciplines(tmp_path):
    with pytest.raises(CriteriaError, match="in_band_disciplines"):
        load_criteria(
            _doc_with_taxonomy(
                tmp_path,
                json.dumps(
                    {
                        "disciplines": {"design": "a", "unclear": "b"},
                        "in_band_disciplines": ["engineering"],
                    }
                ),
            )
        )


def test_in_band_may_not_be_unclear(tmp_path):
    # An unclear read is flagged for manual review; it must never pass.
    with pytest.raises(CriteriaError, match="unclear"):
        load_criteria(
            _doc_with_taxonomy(
                tmp_path,
                json.dumps(
                    {
                        "disciplines": {"design": "a", "unclear": "b"},
                        "in_band_disciplines": ["unclear"],
                    }
                ),
            )
        )


def test_stored_label_keys_are_fixed(tmp_path):
    # quadrant/tension KEYS are written to the DB and parsed back out; renaming
    # one would orphan every existing row, so only the labels are editable.
    with pytest.raises(CriteriaError, match="quadrant_labels"):
        load_criteria(
            _doc_with_taxonomy(
                tmp_path, json.dumps({"quadrant_labels": {"made_up": "x"}})
            )
        )
    c = load_criteria(
        _doc_with_taxonomy(
            tmp_path,
            json.dumps(
                {
                    "tension_labels": {
                        "teach_craft": "grow the craft",
                        "convert_sell": "sell the craft",
                        "mixed": "both",
                    }
                }
            ),
        )
    )
    assert c.taxonomy["tension_labels"]["teach_craft"] == "grow the craft"


def test_taxonomy_tokens_must_be_snake_case(tmp_path):
    # They are sent to the model as enum values and stored in the DB.
    with pytest.raises(CriteriaError, match="snake_case"):
        load_criteria(
            _doc_with_taxonomy(
                tmp_path,
                json.dumps({"disciplines": {"Design Leadership": "a", "unclear": "b"}}),
            )
        )


def test_taxonomy_block_stripped_from_prose(tmp_path):
    c = load_criteria(
        _doc_with_taxonomy(tmp_path, json.dumps({"in_band_disciplines": ["design"]}))
    )
    assert "in_band_disciplines" not in c.prose
    assert "```json taxonomy" not in c.prose
    assert "Prose before." in c.prose and "Prose after." in c.prose


# --- level_bands block (Phase 2) ----------------------------------------


def _doc_with_bands(tmp_path, bands_json, params=None):
    path = tmp_path / "fit_criteria.md"
    path.write_text(
        f"```json tier1_params\n{json.dumps(params or VALID_PARAMS)}\n```\n\n"
        f"```json level_bands\n{bands_json}\n```\n\n" + _wrap_tier2("1. Only criterion")
    )
    return path


def test_level_bands_absent_reproduces_the_shipped_ladder(tmp_path):
    from jshq.ats.normalize import derive_level_band

    c = load_criteria(write_doc(tmp_path))
    for title, band in [
        ("Product Design Director (Individual Contributor)", "ic"),
        ("Associate Creative Director", "director"),
        ("Junior Product Designer", "junior"),
        ("Head of Design", "director"),
        ("Sr. Director of UX", "senior_director"),
        ("Design Lead", "manager"),
        ("Product Designer", "ic"),
    ]:
        assert derive_level_band(title, c.level_bands, c.level_band_fallback) == band


def test_level_bands_can_add_a_band_the_shipped_ladder_lacks(tmp_path):
    """The gap this closes: band NAMES were config but the patterns were not, so
    adding "principal" to target_title_bands produced a key nothing could emit."""
    from jshq.ats.normalize import derive_level_band

    bands = {
        "bands": [
            {"band": "principal", "phrases": ["principal", "distinguished"]},
            {"band": "staff", "phrases": ["staff"]},
        ],
        "fallback": "ic",
    }
    params = dict(VALID_PARAMS, target_title_bands=["principal", "staff"], flag_title_bands={})
    c = load_criteria(_doc_with_bands(tmp_path, json.dumps(bands), params))
    assert derive_level_band("Principal Designer", c.level_bands, c.level_band_fallback) == "principal"
    assert derive_level_band("Staff Designer", c.level_bands, c.level_band_fallback) == "staff"
    assert derive_level_band("Product Designer", c.level_bands, c.level_band_fallback) == "ic"
    # a custom band becomes cappable without a code change
    assert "principal" in c.cappable_bands


def test_custom_band_cap_actually_caps_at_derive_time(tmp_path):
    """cappable_bands was parser-only: a doc could declare a custom band AND a
    cap for it, load cleanly, and the appliers still gated on the legacy
    {"junior"} constant — so every job in that band scored uncapped forever,
    the exact silently-matching-nothing class the parser generalization was
    built to eliminate."""
    from types import SimpleNamespace

    from jshq.scoring import derive

    bands = {"bands": [{"band": "staff", "phrases": ["staff"]}], "fallback": "ic"}
    params = dict(VALID_PARAMS, target_title_bands=["staff"], flag_title_bands={})
    path = tmp_path / "fit_criteria.md"
    path.write_text(
        f"```json tier1_params\n{json.dumps(params)}\n```\n\n"
        f"```json level_bands\n{json.dumps(bands)}\n```\n\n"
        '```json score_caps\n{"staff": 30}\n```\n\n'
        + _wrap_tier2("1. Only criterion")
    )
    c = load_criteria(path)
    assert c.caps["staff"] == 30 and "staff" in c.cappable_bands
    d = derive(
        {"title": "Staff Designer", "level_band": "staff"},
        SimpleNamespace(near_miss_flags=[]),
        {
            "tier2": {1: 2},
            "management_type": "people_leader",
            "leads_discipline": "unclear",
            "near_miss_flags": [],
            "confidence": "high",
        },
        c,
    )
    assert d["band_cap"] == 30
    assert d["cap"] == 30
    assert d["final"] <= 30
    # And the one-item rubric's proportional thin flag never fires on full
    # coverage: threshold(1)=1 equals the whole rubric, so without the
    # full-coverage guard every job this install ever scored carried it.
    assert "thin_posting" not in d["flags"]


def test_a_title_band_no_level_band_can_emit_fails_loud(tmp_path):
    # Previously silent: the filter simply never matched. This is the shipped
    # doc's own historical "head" typo, caught at load.
    bands = json.dumps({"bands": [{"band": "director", "phrases": ["director"]}]})
    params = dict(VALID_PARAMS, target_title_bands=["director", "head"], flag_title_bands={})
    with pytest.raises(CriteriaError, match="head"):
        load_criteria(_doc_with_bands(tmp_path, bands, params))


def test_ic_band_never_reads_the_ic_management_cap(tmp_path):
    # The caps table shares one namespace: "ic" there means the MANAGEMENT type,
    # so the level band of the same name must not claim it.
    c = load_criteria(write_doc(tmp_path))
    assert "ic" not in c.cappable_bands


def test_level_bands_reject_a_non_snake_case_band(tmp_path):
    with pytest.raises(CriteriaError, match="snake_case"):
        load_criteria(
            _doc_with_bands(
                tmp_path, json.dumps({"bands": [{"band": "Sr Director", "phrases": ["x"]}]})
            )
        )


def test_level_bands_reject_an_empty_phrase_list(tmp_path):
    with pytest.raises(CriteriaError, match="phrases"):
        load_criteria(
            _doc_with_bands(tmp_path, json.dumps({"bands": [{"band": "director", "phrases": []}]}))
        )


def test_level_bands_phrase_matching_is_whole_word_and_hyphen_tolerant():
    from jshq.ats.normalize import compile_level_bands

    bands, fallback = compile_level_bands(
        {"bands": [{"band": "director", "phrases": ["head of", "sr. director"]}], "fallback": "ic"}
    )
    from jshq.ats.normalize import derive_level_band

    assert derive_level_band("Head of Design", bands, fallback) == "director"
    assert derive_level_band("Head-of Design", bands, fallback) == "director"
    assert derive_level_band("Sr. Director, UX", bands, fallback) == "director"
    # whole-word: "overhead" must not match "head of"
    assert derive_level_band("Overhead Analyst", bands, fallback) == "ic"
