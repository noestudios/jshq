"""The starter-doc prompt guard (Phase 5b).

The suite's default fixture deliberately overwrites the seeded neutral starter
with the Alex Rivera example (tests/conftest.py — the live-doc anchor tests
need his values), which is exactly why the Tier-1 genericization defects were
invisible to 1000+ green tests: no test ever assembled a prompt from the
document a real fresh install receives. This file is that test. It reads the
starter template straight from DEFAULTS_DIR into a tmp copy (never DATA_DIR),
so the conftest overwrite cannot reach it.

The denylist is word-boundary design vocabulary. If a hit ever appears here,
some default is leaking another person's field into a blank install's paid
scoring calls — fix the default, never this list.
"""

import json
import re
import shutil

import pytest

from jshq import paths
from jshq.scoring import haiku, learned
from jshq.scoring.criteria import load_criteria, write_field

DENYLIST = [
    r"\bdesign(?:er|ers|s)?\b",
    r"\bux\b",
    r"\buser experience\b",
    r"\buser research\b",
    r"\bportfolio\b",
    r"\bcritique\b",
    r"\bcraft\b",
    r"\bdesignops\b",
    r"\bcreative director\b",
    r"\bbrand\b",
]


def _hits(text: str) -> list[str]:
    lowered = text.lower()
    return [p for p in DENYLIST if re.search(p, lowered)]


def _starter_copy(tmp_path, tier2_item="1. Compensation meets my stated target."):
    """The starter doc as a loadable rubric: the template ships with an EMPTY
    tier2 region and build_system_prompt refuses an empty rubric (loud beats a
    silent money leak), so splice in one neutral criterion — the smallest
    thing a wizard user saves."""
    text = (paths.DEFAULTS_DIR / "fit_criteria.starter.md").read_text(encoding="utf-8")
    marked = text.replace(
        "<!-- tier2:start -->", f"<!-- tier2:start -->\n{tier2_item}", 1
    )
    assert marked != text, "tier2:start marker missing from the starter template"
    doc = tmp_path / "fit_criteria.md"
    doc.write_text(marked, encoding="utf-8")
    return doc


def test_starter_prompt_carries_no_design_vocabulary(tmp_path):
    criteria = load_criteria(_starter_copy(tmp_path))
    prompt = haiku.build_system_prompt(criteria, "", [])
    assert _hits(prompt) == []


def test_starter_schema_omits_undeclared_classifications(tmp_path):
    # A blank-slate doc declares no functions map, no quadrant labels, and no
    # in-band discipline — the model must not be forced to classify a nurse's
    # posting into design sub-functions it inherited from the code default.
    criteria = load_criteria(_starter_copy(tmp_path))
    schema = haiku.build_schema(criteria)
    for key in ("function", "fit_quadrant", "leads_discipline"):
        assert key not in schema["properties"], key
        assert key not in schema["required"], key
    assert _hits(json.dumps(schema)) == []


def test_wizard_field_write_stays_neutral(tmp_path):
    # The wizard-shaped path: write_field on the starter copy re-arms the
    # discipline check with the USER'S field and nothing else.
    doc = _starter_copy(tmp_path)
    write_field("nursing", path=doc)
    criteria = load_criteria(doc)
    prompt = haiku.build_system_prompt(criteria, "", [])
    assert _hits(prompt) == []
    assert "nursing" in prompt
    schema = haiku.build_schema(criteria)
    assert "leads_discipline" in schema["properties"]  # re-armed by the field
    assert "function" not in schema["properties"]  # still undeclared
    assert "fit_quadrant" not in schema["properties"]
    assert _hits(json.dumps(schema)) == []


def test_starter_learned_prompt_carries_no_design_vocabulary(tmp_path):
    criteria = load_criteria(_starter_copy(tmp_path))
    prompt = learned.build_proposal_prompt(criteria, "", [])
    assert _hits(prompt) == []


def test_starter_ships_the_senior_ic_engineering_ladder(tmp_path):
    # #59: a fresh install shipped no level_bands block, so the title-band picker
    # fell back to the management-only ladder and omitted the senior-IC engineer
    # rungs the v1 audience actually uses. The starter now ships them, offered by
    # both the wizard and Settings pickers, and they band real titles correctly.
    from jshq.ats.normalize import derive_level_band

    criteria = load_criteria(_starter_copy(tmp_path))
    bands = {b for b, _ in criteria.level_bands}
    for rung in ("staff", "senior_staff", "principal", "distinguished"):
        assert rung in bands, rung

    def band(title):
        return derive_level_band(title, criteria.level_bands, criteria.level_band_fallback)

    assert band("Staff Software Engineer") == "staff"
    assert band("Senior Staff Engineer") == "senior_staff"  # sr-staff wins over staff
    assert band("Principal Engineer") == "principal"
    assert band("Distinguished Engineer") == "distinguished"
    # The management ladder still bands as before, and "Chief of Staff" is an
    # exec, not a staff IC — vp_plus must win the collision on "staff".
    assert band("Chief of Staff") == "vp_plus"
    assert band("Head of Design") == "director"
    assert band("Software Engineer") == "ic"


def test_denylist_actually_detects(tmp_path):
    # Guard the guard: the Alex example must trip it, or a regression in the
    # denylist (or the loader) would let everything above pass vacuously.
    criteria = load_criteria(_starter_copy(tmp_path))
    shipped = (paths.DEFAULTS_DIR / "fit_criteria.md").read_text(encoding="utf-8")
    assert _hits(shipped), "the Alex example should trip the denylist"
    assert _hits(haiku.build_system_prompt(criteria, shipped[:2000], [])), (
        "a design digest must be detectable through the assembled prompt"
    )
