"""The Help manual must not name fit-chip colours that the UI doesn't render
(UX panel Pass B, #63).

The chips were deliberately moved cool (teal mid, desaturated-green high) in
tokens.css, but the manual still taught "Amber (60-69)" / "Green (70 and up)".
The one decision built on measurement can't afford a stale colour word — describe
the bands by score + behaviour instead.
"""

from jshq import paths


def _manual():
    return (paths.DEFAULTS_DIR / "user-manual.md").read_text(encoding="utf-8")


def test_manual_describes_bands_by_score_not_stale_colour():
    md = _manual()
    assert "Amber (60" not in md
    assert "Green (70" not in md
    assert "Grey (under" not in md
    # the score-led wording is present
    assert "**70 and up**" in md
    assert "**60–69**" in md
    assert "**Under 60**" in md
