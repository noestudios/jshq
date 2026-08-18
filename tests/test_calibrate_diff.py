"""calibrate_scoring.diff_baseline — the drift sentinel's pure comparison."""

import importlib.util
from pathlib import Path

# The script is not a package module; load it by path (it inserts backend/ on
# sys.path itself and touches no network or DB at import time).
_SPEC = importlib.util.spec_from_file_location(
    "calibrate_scoring",
    Path(__file__).resolve().parents[1] / "scripts" / "calibrate_scoring.py",
)
calibrate = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(calibrate)


def _result(**overrides):
    base = {
        "label": "good", "final": 85, "model_score": 85, "mgmt": "people_leader",
        "leads": "design", "func_flag": None, "flags": [], "evidenced": 11,
        "confidence": "high",
    }
    base.update(overrides)
    return base


def _snapshot(**results):
    return {"model": "claude-haiku-4-5", "results": results}


def test_identical_runs_have_no_drift_and_no_info():
    a = _snapshot(**{"good_pl.txt": _result()})
    assert calibrate.diff_baseline(a, a) == ([], [])


def test_small_score_movement_is_variance_not_drift():
    old = _snapshot(**{"good_pl.txt": _result()})
    new = _snapshot(**{"good_pl.txt": _result(model_score=85 + calibrate.DRIFT_SCORE_TOLERANCE)})
    drift, info = calibrate.diff_baseline(old, new)
    assert drift == []
    assert len(info) == 1 and "temp-0 variance" in info[0]


def test_score_movement_beyond_tolerance_is_drift():
    old = _snapshot(**{"good_pl.txt": _result()})
    new = _snapshot(**{"good_pl.txt": _result(model_score=85 + calibrate.DRIFT_SCORE_TOLERANCE + 1)})
    drift, info = calibrate.diff_baseline(old, new)
    assert len(drift) == 1 and "model_score 85 → 94" in drift[0]
    assert info == []


def test_definite_categorical_flip_is_drift_but_flags_churn_informs():
    old = _snapshot(**{"wrong_function.txt": _result(leads="product", flags=["wrong_function"])})
    new = _snapshot(**{"wrong_function.txt": _result(leads="design", flags=[])})
    drift, info = calibrate.diff_baseline(old, new)
    assert any("leads product → design" in line for line in drift)
    assert not any("flags" in line for line in drift)
    assert any("flags" in line for line in info)


def test_unclear_flips_and_func_flag_churn_are_soft_not_drift():
    old = _snapshot(**{"bad.txt": _result(
        label="bad", leads="other", func_flag="wrong_function", flags=["wrong_function"]
    )})
    new = _snapshot(**{"bad.txt": _result(
        label="bad", leads="unclear", func_flag=None, flags=[]
    )})
    drift, info = calibrate.diff_baseline(old, new)
    assert drift == []
    assert any("leads other → unclear" in line and "soft flip" in line for line in info)
    assert any("func_flag" in line for line in info)


def test_mgmt_flip_between_definites_is_drift():
    old = _snapshot(**{"good_pl.txt": _result(mgmt="people_leader")})
    new = _snapshot(**{"good_pl.txt": _result(mgmt="ic")})
    drift, _ = calibrate.diff_baseline(old, new)
    assert any("mgmt people_leader → ic" in line for line in drift)
    unclear = _snapshot(**{"good_pl.txt": _result(mgmt="unclear")})
    drift2, info2 = calibrate.diff_baseline(old, unclear)
    assert drift2 == []
    assert any("mgmt people_leader → unclear" in line for line in info2)


def test_model_change_is_drift_by_itself():
    a = _snapshot(**{"good_pl.txt": _result()})
    b = dict(a, model="claude-haiku-5")
    drift, _ = calibrate.diff_baseline(a, b)
    assert len(drift) == 1 and "model changed" in drift[0]


def test_fixture_set_changes_are_reported():
    old = _snapshot(**{"gone.txt": _result(), "kept.txt": _result()})
    new = _snapshot(**{"kept.txt": _result(), "added.txt": _result()})
    drift, info = calibrate.diff_baseline(old, new)
    assert any("gone.txt" in line for line in drift)  # silently unscored would hide drift
    assert any("added.txt" in line for line in info)


# --- aggregate_collision: the habitual-number guard, one 2-way tie tolerated --


def test_all_distinct_aggregates_pass():
    assert calibrate.aggregate_collision([95, 96, 31, 25, 24, 64, 89, 63, 88, 68]) is None


def test_one_two_way_tie_is_tolerated_as_jitter():
    assert calibrate.aggregate_collision([95, 96, 31, 25, 25, 64, 89, 63, 88, 68]) is None


def test_three_fixtures_on_one_value_fail():
    msg = calibrate.aggregate_collision([95, 96, 25, 25, 25, 64, 89, 63, 88, 68])
    assert msg is not None and "[25]" in msg


def test_two_tied_pairs_fail():
    msg = calibrate.aggregate_collision([95, 95, 31, 25, 25, 64, 89, 63, 88, 68])
    assert msg is not None and "[25, 95]" in msg


# --- read_failures: the per-fixture flake surface ----------------------------


def _read_result(label="bad", *, tier2=None, quotes=None, notes='"quoted evidence" here',
                 mgmt="ic", leads="other", flags=None, func_flag=None, entry_extra=None):
    entry = {"file": "fixture.txt", "label": label}
    entry.update(entry_extra or {})
    return {
        "entry": entry,
        "data": {
            "tier2": tier2 or {1: 1},
            "tier2_quotes": quotes or {},
            "management_type": mgmt,
            "leads_discipline": leads,
            "scoring_notes": notes,
        },
        "flags": flags if flags is not None else [],
        "func_flag": func_flag,
    }


def test_clean_read_has_no_failures():
    assert calibrate.read_failures(_read_result()) == []


def test_plus_two_without_quote_fails():
    r = _read_result(tier2={3: 2}, quotes={})
    assert any("±2 sub-scores without quoted evidence" in f for f in calibrate.read_failures(r))


def test_plus_two_with_quote_passes():
    r = _read_result(tier2={3: 2}, quotes={3: "a quoted phrase"})
    assert calibrate.read_failures(r) == []


def test_notes_without_quotes_fail_unless_both_reads_unclear():
    r = _read_result(notes="no quotes here")
    assert any("notes lack quoted evidence" in f for f in calibrate.read_failures(r))
    both_unclear = _read_result(mgmt="unclear", leads="unclear", notes="no quotes here")
    assert calibrate.read_failures(both_unclear) == []


def test_bad_fixture_read_as_people_leader_fails():
    r = _read_result(mgmt="people_leader")
    assert any("read as people_leader" in f for f in calibrate.read_failures(r))


def test_wrong_function_expectations_come_from_manifest():
    r = _read_result(
        label="wrong_function", leads="content", flags=["wrong_function"],
        entry_extra={"expect_leads": "content"},
    )
    assert calibrate.read_failures(r) == []
    r2 = _read_result(label="wrong_function", leads="design", flags=["wrong_function"],
                      entry_extra={"expect_leads": "content"})
    assert any("expected content" in f for f in calibrate.read_failures(r2))


# --- drifted_fixture_files: which drift lines a re-read can arbitrate --------


def test_drift_lines_map_back_to_fixture_files():
    drift = [
        "bad_convert_sell_vp.txt: leads other → unclear",
        "bad_convert_sell_vp.txt: func_flag wrong_function → None",
        "bad_ic_director.txt: flags ['a'] → ['b']",
    ]
    known = ["bad_convert_sell_vp.txt", "bad_ic_director.txt", "good_head_of_design.txt"]
    assert calibrate.drifted_fixture_files(drift, known) == [
        "bad_convert_sell_vp.txt", "bad_ic_director.txt",
    ]


def test_model_change_line_is_not_a_fixture():
    drift = ["model changed: claude-haiku-4-5 → claude-haiku-5 — review the per-fixture diff, then --save-baseline"]
    assert calibrate.drifted_fixture_files(drift, ["good_head_of_design.txt"]) == []
