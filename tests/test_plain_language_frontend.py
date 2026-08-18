"""Plain-language content cluster (2026-08-17 UX panel review): the scoring
surface should speak the user's language, not internal enums. #6 (PEER-02)
glosses the near-miss flag pills; #18 (PEER-05) drops the "Tier 1/Tier 2"
jargon from Settings → Scoring. Source-scan style (no JS runtime).

(#16/#17 from the same cluster were closed won't-fix — the audience is
tech/design professionals, so the wizard's defaults are on-target.)
"""

from jshq import paths

FRONTEND = paths.FRONTEND_DIR


def _read(rel):
    return (FRONTEND / rel).read_text(encoding="utf-8")


# ---- #6 PEER-02: plain-language flag pills ---------------------------------

GLOSSES = {
    "wrong_function": "role mismatch",
    "function_unclear": "role unclear",
    "scope_gap": "seniority gap",
    "below_band": "below your level",
    "comp_below_target": "below target pay",
    "comp_unknown": "no salary listed",
    "location_unknown": "location unclear",
    "thin_posting": "light on detail",
}


def test_flag_gloss_map_covers_every_structured_flag():
    jobs = _read("js/views/jobs.js")
    assert "const FLAG_GLOSSES = {" in jobs
    for token, gloss in GLOSSES.items():
        assert f'{token}: "{gloss}"' in jobs, token


def test_flag_label_glosses_with_a_snake_case_fallback():
    jobs = _read("js/views/jobs.js")
    # display-only: the map is consulted, then the old snake→space fallback so a
    # free-form model flag never renders blank; the internal token is untouched
    assert 'return FLAG_GLOSSES[flag] || flag.replaceAll("_", " ");' in jobs
    # the red-fail styling still keys on the raw token, not the glossed label
    assert 'f === "wrong_function"' in jobs


def test_sibling_override_is_hidden_from_pills_and_rollup():
    jobs = _read("js/views/jobs.js")
    assert 'const HIDDEN_FLAGS = new Set(["sibling_override"]);' in jobs
    # filtered out of the per-job Scoring chips …
    assert "near_miss_flags.filter(" in jobs
    assert "!HIDDEN_FLAGS.has(f)" in jobs
    # … and skipped in the flag-rollup counts
    assert "if (HIDDEN_FLAGS.has(flag)) continue;" in jobs


# ---- #18 PEER-05: no Tier jargon in the scoring UI -------------------------

def test_tier_chips_are_gone_from_settings():
    settings = _read("js/views/settings.js")
    assert '<span class="section-count">Tier 1</span>' not in settings
    assert '<span class="section-count">Tier 2</span>' not in settings
    # the plain section titles remain
    assert "Hard rules — auto-rejected if they fail" in settings
    assert "What I'm looking for — ranked" in settings


def test_scoring_hint_teaches_with_the_section_names_not_tiers():
    hints = _read("js/lib/helpHint.js")
    assert 'title: "Tier 1 vs Tier 2"' not in hints
    assert 'title: "How scoring works"' in hints
    # the rewritten body speaks in the section names the user actually sees
    assert "“Hard rules” are pass/fail gates" in hints
    assert "“What I'm looking for”" in hints
    # the fit-score hint no longer leaks "(Tier 1)"
    assert "failed a hard rule (Tier 1)" not in hints
    assert "0 means it failed a hard rule." in hints


def test_no_user_facing_tier_labels_survive_in_the_hint_copy():
    # every HINTS title/body string is user-visible; none should say "Tier N"
    hints = _read("js/lib/helpHint.js")
    for line in hints.splitlines():
        stripped = line.strip()
        if stripped.startswith(("title:", "body:")):
            assert "Tier 1" not in stripped and "Tier 2" not in stripped, stripped
