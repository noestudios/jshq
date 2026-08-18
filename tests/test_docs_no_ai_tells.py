"""The shipped prose docs must stay clear of the AI tells the owner's voice
guide bans (UX panel #54): no em dashes, and US spelling.

Scope is exactly the four public-facing prose docs #54 covers. The fictional
example docs (`fit_criteria.md`, `voice_guide.md`), `AI-TELLS.md` (deliberately
tell-laden), code comments, and CSS/JS strings are out of scope and are NOT
scanned here.

Note: this guard is intentionally mechanical (em dash + British spelling), the
objective half of #54. The subjective voice work (no "X, not Y", banned vocab,
significance inflation) is done by hand against `docs/voice_guide-personalized.md`
and not asserted here, so a legitimate future edit isn't blocked by a brittle
heuristic.
"""

import re
from pathlib import Path

from jshq import paths

REPO_ROOT = Path(__file__).resolve().parents[1]

# The four in-scope docs, resolved from their real homes.
IN_SCOPE = {
    "README.md": REPO_ROOT / "README.md",
    "PRIVACY.md": REPO_ROOT / "PRIVACY.md",
    "CONTRIBUTING.md": REPO_ROOT / "CONTRIBUTING.md",
    "SECURITY.md": REPO_ROOT / "SECURITY.md",
    "docs/CASE-STUDY.md": REPO_ROOT / "docs" / "CASE-STUDY.md",
    "user-manual.md": paths.DEFAULTS_DIR / "user-manual.md",
}

EM_DASH = "—"  # — ; en dash (–, U+2013) in ranges like 60–69 stays allowed.
BRITISH = re.compile(r"colour|behaviour", re.IGNORECASE)


def _read(path):
    return path.read_text(encoding="utf-8")


def test_no_em_dashes_in_shipped_prose_docs():
    for label, path in IN_SCOPE.items():
        text = _read(path)
        assert EM_DASH not in text, f"{label} still contains an em dash (U+2014)"


def test_us_spelling_in_shipped_prose_docs():
    for label, path in IN_SCOPE.items():
        text = _read(path)
        hits = BRITISH.findall(text)
        assert not hits, f"{label} uses British spelling: {sorted(set(hits))}"


def test_manual_band_tokens_survived_the_sweep():
    # The em-dash sweep must not have disturbed the score-led band wording that
    # test_manual_fit_bands_doc.py and the Help view depend on.
    md = _read(IN_SCOPE["user-manual.md"])
    assert "**70 and up**" in md
    assert "**60–69**" in md  # en dash, deliberately kept
    assert "**Under 60**" in md
