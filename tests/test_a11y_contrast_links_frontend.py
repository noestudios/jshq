"""Amber-on-amber contrast (#35 / A11Y-02) and use-of-colour for inline links
(#36 / A11Y-03), UX panel Pass A.

#35: .wizard-required / .wish-rank / .wb-mark-done drew --t-accent on
--t-accent-soft — 3.59:1 dark, 3.68:1 light, both under the 4.5:1 body floor. A
dedicated --t-on-accent-soft token (solved per theme) fixes all three at once.

#36: in-sentence a.wizard-link relied on colour alone (~1.5:1 vs prose, underline
only on hover) — a persistent underline inside running text, and on focus.

WCAG 2.2 SC 1.4.3 (contrast, AA), SC 1.4.1 (use of colour, A).
"""

import re

from jshq import paths

FRONTEND = paths.FRONTEND_DIR


def _read(rel):
    return (FRONTEND / rel).read_text(encoding="utf-8")


def _lin(c):
    c /= 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def _luminance(hexs):
    r, g, b = (int(hexs[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def _ratio(a, b):
    la, lb = _luminance(a), _luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _token(css, name, *, near):
    """Value of `name` in the theme block containing the marker `near`."""
    block = css[css.index(near) :]
    m = re.search(rf"{re.escape(name)}:\s*(#[0-9a-fA-F]{{6}})", block)
    assert m, f"{name} not found near {near}"
    return m.group(1)


# ---- #35 amber-on-amber contrast -----------------------------------------

def test_on_accent_soft_token_clears_4_5_to_1_in_both_themes():
    css = _read("css/tokens.css")
    # dark block is the first accent-soft; light block sits after the light marker
    dark_soft = _token(css, "--t-accent-soft", near="--t-accent: #c8984e;")
    dark_fg = _token(css, "--t-on-accent-soft", near="--t-accent: #c8984e;")
    light_soft = _token(css, "--t-accent-soft", near="--t-accent: #8e5b00;")
    light_fg = _token(css, "--t-on-accent-soft", near="--t-accent: #8e5b00;")
    assert _ratio(dark_fg, dark_soft) >= 4.5
    assert _ratio(light_fg, light_soft) >= 4.5


def test_amber_pills_point_at_the_on_accent_token_not_plain_accent():
    css = _read("css/app.css")
    # Exactly the three components (.wizard-required / .wish-rank / .wb-mark-done)
    # now pair --t-accent-soft with the readable ink.
    assert css.count("color: var(--t-on-accent-soft);") == 3
    # None of the three keeps the old low-contrast accent-soft + accent pairing.
    assert "background: var(--t-accent-soft);\n  color: var(--t-accent);" not in css


# ---- #36 inline-link underline -------------------------------------------

def test_in_sentence_links_carry_a_persistent_underline():
    css = _read("css/app.css")
    assert ":where(p, li) a.wizard-link {" in css
    assert "a.wizard-link:focus-visible" in css
