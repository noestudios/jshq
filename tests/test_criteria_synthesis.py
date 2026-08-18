"""write_synthesis_prose — the fenced splice for synthesized reflection prose."""

import json

import pytest

from jshq.scoring.criteria import (
    CriteriaError,
    load_criteria,
    write_synthesis_prose,
)
from test_criteria import VALID_PARAMS

BODY = (
    "## Fulfillment matrix — quadrants\n\n"
    "### energizing_strength — energizing · strength (best fit)\n"
    "- coaching designers one-on-one\n\n"
    "**Signal verbs:** mentor, coach, critique."
)


def make_doc(tmp_path, *, rubric=True):
    path = tmp_path / "fit_criteria.md"
    parts = [
        "# Criteria\n\nIntro prose.\n",
        f"```json tier1_params\n{json.dumps(VALID_PARAMS)}\n```\n",
        "<!-- tier2:start -->\n1. **Craft bar** — high.\n2. **Team** — strong.\n<!-- tier2:end -->\n",
    ]
    if rubric:
        parts.append("## Scoring rubric\n\nRubric prose.\n")
    path.write_text("\n".join(parts), encoding="utf-8")
    return path


def test_inserts_above_the_rubric_heading(tmp_path):
    path = make_doc(tmp_path)
    c = write_synthesis_prose(BODY, path=path)
    text = path.read_text(encoding="utf-8")
    assert text.index("<!-- synthesis:start -->") < text.index("## Scoring rubric")
    # fences are comments: stripped from prose, the body ships to the scorer
    assert "Signal verbs" in c.prose
    assert "synthesis:start" not in c.prose


def test_appends_when_no_rubric_heading(tmp_path):
    path = make_doc(tmp_path, rubric=False)
    write_synthesis_prose(BODY, path=path)
    text = path.read_text(encoding="utf-8")
    assert text.rstrip("\n").endswith("<!-- synthesis:end -->")


def test_rerun_replaces_only_the_fenced_region(tmp_path):
    path = make_doc(tmp_path)
    write_synthesis_prose(BODY, path=path)
    # a hand edit outside the fences must survive the re-run byte-for-byte
    hand_edited = path.read_text(encoding="utf-8").replace(
        "Intro prose.", "Intro prose, hand-tuned."
    )
    path.write_text(hand_edited, encoding="utf-8")
    write_synthesis_prose("## Rewritten section\n\nSecond draft.", path=path)
    text = path.read_text(encoding="utf-8")
    assert "Second draft." in text
    assert "Signal verbs" not in text  # first body fully replaced
    assert "Intro prose, hand-tuned." in text
    assert text.count("<!-- synthesis:start -->") == 1


def test_rejects_comments_openers_and_empty(tmp_path):
    path = make_doc(tmp_path)
    before = path.read_bytes()
    for bad in (
        "",
        "prose with a <!-- comment -->",
        "sneaky\n<!-- synthesis:end -->",
        "a fake\n```json tier1_params\n{}\n``` block",
        "case games\n```JSON TIER1_PARAMS".lower() + " x",
    ):
        with pytest.raises(CriteriaError):
            write_synthesis_prose(bad, path=path)
    assert path.read_bytes() == before  # nothing landed


def test_tier2_co_splice_is_one_atomic_swap(tmp_path):
    path = make_doc(tmp_path)
    items = [
        {"text": "Coach a senior team", "weight": 2.0, "craft": True, "bonus_only": False},
        {"text": "Ship real product", "weight": 1.0, "craft": False, "bonus_only": False},
    ]
    c = write_synthesis_prose(BODY, tier2_criteria=items, path=path)
    assert [i["text"] for i in c.tier2] == ["Coach a senior team", "Ship real product"]
    assert c.craft_criterion == 1

    # an invalid refinement must leave BOTH prose and tier2 untouched
    before = path.read_bytes()
    broken = [{"text": "X", "weight": 1.0, "craft": True, "bonus_only": True}]
    with pytest.raises(CriteriaError):
        write_synthesis_prose("## Replacement\n\nNever lands.", tier2_criteria=broken, path=path)
    assert path.read_bytes() == before


def test_stray_hand_added_fence_fails_loud(tmp_path):
    path = make_doc(tmp_path)
    write_synthesis_prose(BODY, path=path)
    path.write_text(
        path.read_text(encoding="utf-8") + "\n<!-- synthesis:end -->\n", encoding="utf-8"
    )
    with pytest.raises(CriteriaError, match="exactly one synthesis fence pair"):
        write_synthesis_prose("## Again", path=path)
