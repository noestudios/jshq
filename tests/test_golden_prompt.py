"""Golden snapshot of the assembled scoring prompt and its output schema.

The prompt is the product. Nearly all of its bytes come from the criteria doc's
prose, which no substring assertion covers: an edit could reorder, reword or
drop whole sections of the rubric the model actually reads and every other test
in the suite would stay green. Phase 2 moves taxonomy out of code and into that
doc, so the blast radius of a bad edit is exactly this text.

This test freezes it. A mismatch is not automatically a failure of the code --
it is a prompt change that a human must look at, because the calibration
baseline (scripts/calibrate_scoring.py, a paid live run) was blessed against
the previous wording. Re-bless deliberately, with the diff in front of you:

    JSHQ_UPDATE_GOLDEN=1 .venv/bin/pytest tests/test_golden_prompt.py

then commit the regenerated fixture alongside the change that caused it.

Snapshotting the SHIPPED doc (src/jshq/defaults/) rather than the seeded live
copy keeps this deterministic: the fixture tracks what the package ships, not
whatever a developer's DATA_DIR happens to contain.
"""

import difflib
import json
import os
from pathlib import Path

import pytest

from jshq import paths
from jshq.scoring import haiku
from jshq.scoring.criteria import NEUTRAL_DISPLAY_NAME, load_criteria

SHIPPED_DOC = paths.DEFAULTS_DIR / "fit_criteria.md"
GOLDEN_DIR = Path(__file__).parent / "fixtures" / "golden"
PROMPT_FIXTURE = GOLDEN_DIR / "system_prompt.txt"
SCHEMA_FIXTURE = GOLDEN_DIR / "schema.json"

UPDATE = bool(os.environ.get("JSHQ_UPDATE_GOLDEN"))


def _shipped_criteria():
    # Explicit path: load_criteria only memoizes CRITERIA_PATH, so this never
    # reads or populates the live-doc cache.
    return load_criteria(SHIPPED_DOC)


def _built_prompt() -> str:
    # No digest and no learned rules: both are DB state, and a golden fixture
    # must depend on the doc alone. scripts/calibrate_scoring.py builds the
    # prompt the same way and for the same reason.
    return haiku.build_system_prompt(_shipped_criteria(), "", [])


def _compare(actual: str, fixture: Path, label: str) -> None:
    if UPDATE:
        fixture.parent.mkdir(parents=True, exist_ok=True)
        fixture.write_text(actual, encoding="utf-8")
        return
    if not fixture.exists():
        pytest.fail(
            f"missing golden fixture {fixture.name}. Generate it with:\n"
            f"    JSHQ_UPDATE_GOLDEN=1 .venv/bin/pytest {__file__}"
        )
    expected = fixture.read_text(encoding="utf-8")
    if actual == expected:
        return
    diff = "\n".join(
        difflib.unified_diff(
            expected.splitlines(),
            actual.splitlines(),
            fromfile=f"golden/{fixture.name}",
            tofile=f"built {label}",
            lineterm="",
        )
    )
    pytest.fail(
        f"the {label} changed against its golden fixture.\n\n{diff}\n\n"
        "If this change is intended, review the diff above, then re-bless:\n"
        f"    JSHQ_UPDATE_GOLDEN=1 .venv/bin/pytest {__file__}\n"
        "and re-run the calibration harness before trusting scores."
    )


def test_system_prompt_matches_golden():
    _compare(_built_prompt(), PROMPT_FIXTURE, "system prompt")


def test_output_schema_matches_golden():
    # Built from the shipped doc, not the code defaults: the schema's enums are
    # the doc's taxonomy now, so snapshotting the defaults would miss a drift
    # between what the prompt describes and what the grammar allows.
    schema = (
        json.dumps(haiku.build_schema(_shipped_criteria()), indent=2, sort_keys=True) + "\n"
    )
    _compare(schema, SCHEMA_FIXTURE, "output schema")


# --- guards that hold whatever the fixture says --------------------------
#
# The snapshot pins the prompt to its current wording; these pin properties it
# must never lose, so a careless re-bless cannot quietly enshrine a regression.


def test_golden_prompt_carries_no_personal_data():
    # The prompt is the one artifact that leaves the machine on every scored
    # job (CLAUDE.md: no personal data, zero phone-home beyond the user's key).
    prompt = _built_prompt()
    assert "Chris" not in prompt
    assert "noestudios" not in prompt


def test_golden_prompt_names_the_persona_from_the_doc():
    criteria = _shipped_criteria()
    prompt = _built_prompt()
    assert criteria.display_name != NEUTRAL_DISPLAY_NAME, (
        "the shipped doc should name its example persona"
    )
    assert criteria.display_name in prompt
    assert f"{criteria.domain_label} job postings" in prompt


def test_golden_prompt_asks_for_every_criterion_in_the_doc():
    # The doc's Tier 2 list and the count quoted at the model must agree, or the
    # criteria past that count sit permanently unevidenced and silently take
    # their silence value on every job.
    criteria = _shipped_criteria()
    assert str(len(criteria.tier2)) in _built_prompt()


def test_golden_prompt_leaks_no_machine_block_values():
    # Caps, deductions and the score scale are stripped from the prose on
    # purpose: shown the numbers, the model pre-applies them instead of
    # reporting honestly.
    prompt = _built_prompt()
    for machine_key in ('"slope"', '"intercept"', '"silence"', '"scope_gap":', '"ic":'):
        assert machine_key not in prompt, f"{machine_key} reached the model"
