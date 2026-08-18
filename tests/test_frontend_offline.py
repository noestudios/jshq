"""Zero-phone-home invariant, frontend edition (Phase 1).

The shipped frontend must reference no external origin at all: no CDN fonts,
no remote scripts/styles/images. (The one disclosed network exception,
icons.duckduckgo.com, lives in the backend logo pipeline — logos.py — not
in the frontend, so the assertion here can be absolute.)
"""

import re

from jshq import paths

_TEXT_SUFFIXES = {".html", ".css", ".js", ".svg", ".json", ".txt", ".md"}
# http(s):// followed by anything that isn't a quote/paren/whitespace.
_EXTERNAL = re.compile(r"https?://[^\s\"'()<>]+")

# Prose mentions of URLs (comments, license texts) and user-clicked <a href>
# navigation links are fine; what must not exist is an *auto-fetched* resource
# reference — script/img/link src|href, CSS url(), @import — pointing
# off-origin: those fire on page load with zero user action.
_FETCH_CONTEXT = re.compile(
    r"""src\s*=\s*["']https?://|<link[^>]{0,200}href\s*=\s*["']https?://"""
    r"""|url\(\s*["']?https?://|@import\s+["']https?://|fetch\(\s*["']https?://""",
    re.IGNORECASE,
)


def test_frontend_has_no_external_fetch_references():
    offenders = []
    for path in paths.FRONTEND_DIR.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8")
        for match in _FETCH_CONTEXT.finditer(text):
            offenders.append(f"{path.relative_to(paths.FRONTEND_DIR)}: {match.group(0)!r}")
    assert not offenders, "external fetch references in shipped frontend:\n" + "\n".join(offenders)


def test_frontend_never_mentions_font_cdns():
    for path in paths.FRONTEND_DIR.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8")
        assert "fonts.googleapis.com" not in text, path
        assert "fonts.gstatic.com" not in text, path


def test_vendored_fonts_ship_with_licenses():
    fonts = paths.FRONTEND_DIR / "fonts"
    woff2 = sorted(p.name for p in fonts.glob("*.woff2"))
    assert woff2, "vendored woff2 files missing"
    assert (fonts / "OFL-Geist.txt").is_file()
    assert (fonts / "OFL-InstrumentSans.txt").is_file()
