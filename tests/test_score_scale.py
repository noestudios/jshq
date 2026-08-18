"""Per-install score scale (Phase 5b).

The shipped 1.6/55 constants were calibrated to the example doc's
eleven-criterion rubric: on a three-item wizard list, a job scoring +2 on
every single criterion aggregated to 66 — below the 70 positive-fit line the
UI is built around, so the headline output was mathematically unreachable.
derive_scale generalizes the constants (20/Σw over intercept 50) and
sync_scale writes the block on ranked-list saves, guarded by an ownership
fingerprint so hand-authored blocks — the example doc's included — are never
touched.
"""

import math
import os
import shutil
from pathlib import Path

import pytest

from jshq import paths
from jshq.scoring import POSITIVE_FIT, aggregate, thin_threshold
from jshq.scoring import criteria as criteria_mod
from jshq.scoring.criteria import derive_scale, read_editable


def items(*weights):
    return [
        {"text": f"criterion {i}", "weight": w, "craft": False, "bonus_only": False}
        for i, w in enumerate(weights, 1)
    ]


def ramp(n):
    """The wizard's rank→weight ramp (welcome.js rampedTier2), 1.5 → 0.75."""
    if n < 2:
        return items(*([1.0] * n))
    return items(*(round(1.5 - (0.75 * i) / (n - 1), 2) for i in range(n)))


def scored(value, tier2):
    return {i: value for i in range(1, len(tier2) + 1)}


# --- derive_scale arithmetic, through the real aggregate() -------------------


@pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 11, 20])
def test_all_plus_two_lands_on_90_at_every_size(n):
    tier2 = ramp(n)
    scale = derive_scale(tier2)
    score, evidenced = aggregate(scored(2, tier2), tier2, scale)
    assert score == 90
    assert evidenced == n


@pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 11, 20])
def test_average_plus_one_is_exactly_the_positive_fit_line(n):
    tier2 = ramp(n)
    score, _ = aggregate(scored(1, tier2), tier2, derive_scale(tier2))
    assert score == POSITIVE_FIT == 70


def test_three_item_wizard_list_can_reach_positive_fit():
    # The defect this replaces: with the shipped 1.6/55, this exact payload
    # aggregated to 66 — "positive fit" was unreachable for a short list.
    tier2 = ramp(3)
    old_score, _ = aggregate(scored(2, tier2), tier2, {"slope": 1.6, "intercept": 55.0})
    new_score, _ = aggregate(scored(2, tier2), tier2, derive_scale(tier2))
    assert old_score < POSITIVE_FIT <= new_score


def test_eleven_ramped_items_recover_the_shipped_constants():
    # Sanity anchor: the derivation is a faithful generalization of 1.6/55,
    # not a new scoring philosophy.
    scale = derive_scale(ramp(11))
    assert scale["intercept"] == 50.0
    assert abs(scale["slope"] - 1.6) < 0.02


def test_slope_stays_inside_the_parser_rail():
    assert derive_scale(items(0.25))["slope"] == 50.0  # clamped
    assert derive_scale(items(*([4.0] * 40)))["slope"] >= 0.01


# --- thin-posting proportionality --------------------------------------------


def test_thin_threshold_table():
    # 11 ⇒ 4 keeps the example doc's behavior (and every existing thin test)
    # unchanged; short rubrics stop flagging every job ever scored.
    assert [thin_threshold(n) for n in (1, 2, 3, 4, 6, 11, 12)] == [1, 1, 1, 2, 2, 4, 4]
    assert thin_threshold(11) == math.ceil(11 / 3)


# --- sync_scale ownership + emission -----------------------------------------


@pytest.fixture
def starter_doc(tmp_path, monkeypatch):
    """The neutral starter as the live doc (same shape as a fresh install),
    with one criterion spliced so the doc loads."""
    doc = tmp_path / "fit_criteria.md"
    text = (paths.DEFAULTS_DIR / "fit_criteria.starter.md").read_text(encoding="utf-8")
    doc.write_text(
        text.replace("<!-- tier2:start -->", "<!-- tier2:start -->\n1. Starter criterion.", 1),
        encoding="utf-8",
    )
    monkeypatch.setattr(criteria_mod, "CRITERIA_PATH", doc)
    monkeypatch.setattr(criteria_mod, "_cache", None)
    return doc


def put_list(doc, tier2):
    """A ranked-list save as the endpoints perform it (size_scale=True)."""
    criteria_mod.write_criteria(read_editable(doc)[0], tier2, path=doc, size_scale=True)


def test_first_wishlist_save_emits_a_sized_scale(starter_doc):
    assert "score_scale" not in starter_doc.read_text(encoding="utf-8")
    put_list(starter_doc, ramp(3))
    assert "```json score_scale" in starter_doc.read_text(encoding="utf-8")
    c = criteria_mod.load_criteria(starter_doc)
    assert c.scale["slope"] == derive_scale(ramp(3))["slope"]
    assert c.scale["intercept"] == 50.0


def test_unchanged_list_is_a_byte_identical_no_op(starter_doc):
    put_list(starter_doc, ramp(3))
    before = starter_doc.read_text(encoding="utf-8")
    put_list(starter_doc, ramp(3))  # same list again
    assert starter_doc.read_text(encoding="utf-8") == before


def test_machine_owned_block_rederives_on_reorder(starter_doc):
    put_list(starter_doc, ramp(3))
    put_list(starter_doc, ramp(5))
    c = criteria_mod.load_criteria(starter_doc)
    assert c.scale["slope"] == derive_scale(ramp(5))["slope"]


def test_hand_edited_weight_token_does_not_orphan_the_machine_block(starter_doc):
    # A hand edit to a LIST weight is not a hand edit to the BLOCK, so sizing
    # must still follow the next save. The old ownership fingerprint compared
    # the block against derive_scale(pre-save list) — which the weight edit
    # had already changed — so the machine's own block read as hand-authored
    # and the scale never re-sized again (the all-+2 ⇒ 90 contract silently
    # broken on every later save). The self-fingerprint (derived_from_total)
    # keeps the block machine-owned regardless of list-side edits.
    put_list(starter_doc, ramp(3))
    text = starter_doc.read_text(encoding="utf-8")
    edited = text.replace("[w: 1.5]", "[w: 4]", 1)
    assert edited != text
    starter_doc.write_text(edited, encoding="utf-8")
    put_list(starter_doc, ramp(5))
    assert criteria_mod.load_criteria(starter_doc).scale["slope"] == (
        derive_scale(ramp(5))["slope"]
    )


def test_hand_authored_block_is_never_touched(starter_doc):
    # Simulate a user hand-tuning the block: its fingerprint no longer matches
    # derive_scale of the pre-save list, so a later list edit leaves it alone.
    put_list(starter_doc, ramp(3))
    text = starter_doc.read_text(encoding="utf-8")
    hand = text.replace(f'"slope": {derive_scale(ramp(3))["slope"]}', '"slope": 2.2')
    assert hand != text
    starter_doc.write_text(hand, encoding="utf-8")
    put_list(starter_doc, ramp(5))
    assert criteria_mod.load_criteria(starter_doc).scale["slope"] == 2.2


def test_shipped_example_block_reads_as_hand_authored(tmp_path, monkeypatch):
    # The Alex doc's 1.6/55 + silence map must never fingerprint as machine-
    # owned, or live-doc round-trip tests would clobber his silence values.
    doc = tmp_path / "fit_criteria.md"
    shutil.copy(paths.DEFAULTS_DIR / "fit_criteria.md", doc)
    monkeypatch.setattr(criteria_mod, "CRITERIA_PATH", doc)
    monkeypatch.setattr(criteria_mod, "_cache", None)
    _, tier2 = read_editable(doc)
    reordered = list(reversed(tier2))
    criteria_mod.write_criteria(read_editable(doc)[0], reordered, path=doc, size_scale=True)
    assert "```json score_scale" in doc.read_text(encoding="utf-8")
    assert criteria_mod.load_criteria(doc).scale["slope"] == 1.6


def graft_silence(doc, silence):
    """Add silence entries to the machine block, keeping its fingerprint."""
    import json as jsonlib

    text = doc.read_text(encoding="utf-8")
    block = criteria_mod._SCALE_BLOCK.search(text)
    payload = jsonlib.loads(block.group(1))
    payload["silence"] = silence
    doc.write_text(
        text.replace(
            block.group(0),
            "```json score_scale\n" + jsonlib.dumps(payload, indent=2) + "\n```",
        ),
        encoding="utf-8",
    )


def test_silence_keys_survive_within_range_and_prune_beyond(starter_doc):
    put_list(starter_doc, ramp(4))
    graft_silence(starter_doc, {"2": -0.5, "4": -1.0})
    put_list(starter_doc, ramp(3))  # shrink: key "4" is out of range, pruned
    scale = criteria_mod.load_criteria(starter_doc).scale
    assert scale["silence"] == {2: -0.5}


def test_writers_drop_the_mtime_cache(starter_doc, monkeypatch):
    """Regression (CI, windows-latest): Windows updates file times on a coarse
    timer tick, so a writer's replace can land on the SAME st_mtime as a write
    the cache parsed moments earlier — and load_criteria served the stale
    parse (the grafted 4-item doc, silence unpruned). The writers must drop
    the cache themselves. Simulated by pinning the doc's reported mtime so
    the cache can never see any write."""
    real_stat = Path.stat

    def pinned(self, *args, **kwargs):
        st = real_stat(self, *args, **kwargs)
        if self == starter_doc:
            values = list(st)
            values[8] = 1_000_000_000  # st_mtime slot
            return os.stat_result(tuple(values))
        return st

    monkeypatch.setattr(Path, "stat", pinned)
    put_list(starter_doc, ramp(4))
    graft_silence(starter_doc, {"2": -0.5, "4": -1.0})
    criteria_mod._cache = None
    criteria_mod.load_criteria(starter_doc)  # cache the grafted, pre-shrink parse
    put_list(starter_doc, ramp(3))  # replace lands on the pinned mtime
    scale = criteria_mod.load_criteria(starter_doc).scale
    assert scale["silence"] == {2: -0.5}
