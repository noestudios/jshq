"""Wizard + shell accessibility batch (UX panel Pass A):
#41 announce step transitions, #45 matrix cell prompt/axis association, #46 the
page <h1> inside the <main> landmark, #47 the inert masthead is inert to the
keyboard too, #48 unified focus rings, #49 icon-button names, #51 weight-stepper
target consistency.

WCAG 2.2: SC 4.1.3 (status messages), 2.4.3 (focus order), 1.3.1 / 3.3.2 (name /
relationships), 2.4.7 (focus visible), 4.1.2 (name/role/value), 2.5.8 (target
size). No JS runtime here; pinned against the shipped source.
"""

from jshq import paths

FRONTEND = paths.FRONTEND_DIR


def _read(rel):
    return (FRONTEND / rel).read_text(encoding="utf-8")


# ---- #41 announce step transitions ---------------------------------------

def test_a_persistent_polite_region_announces_the_step():
    html = _read("index.html")
    assert '<div id="wizard-live" class="sr-only" aria-live="polite"></div>' in html
    js = _read("js/views/welcome.js")
    assert 'document.getElementById("wizard-live")' in js
    assert "Step ${s.step} of ${SETUP_TOTAL}: ${title}" in js


# ---- #45 matrix cell prompt + axis ---------------------------------------

def test_matrix_textarea_is_described_by_its_prompt_and_names_its_axes():
    js = _read("js/views/welcome.js")
    assert 'id="matrix-prompt-${key}"' in js
    assert 'aria-describedby="matrix-prompt-${key}"' in js
    assert 'aria-label="${heading} — ${axis}"' in js


# ---- #46 <h1> inside the main landmark -----------------------------------

def test_view_heading_lives_in_the_main_landmark():
    assert '<h1 id="view-heading"' not in _read("index.html")
    app = _read("js/app.js")
    assert "function ensureViewHeading(route)" in app
    assert "view.insertBefore(h, view.firstChild" in app


# ---- #47 inert masthead ---------------------------------------------------

def test_brand_mark_is_made_inert_during_the_wizard():
    app = _read("js/app.js")
    # both the keyboard (tabindex) and AT (aria-hidden) are disabled on welcome
    assert 'brand.setAttribute("tabindex", "-1")' in app
    assert 'brand.setAttribute("aria-hidden", "true")' in app
    assert 'brand.removeAttribute("tabindex")' in app


# ---- #48 unified focus rings ---------------------------------------------

def test_wizard_controls_share_the_focus_ring():
    css = _read("css/app.css")
    ring = css[css.index(".btn:focus-visible,") : css.index(".btn:focus-visible,") + 400]
    for sel in (".wish-btn:focus-visible", ".wish-text:focus-visible",
                ".wizard-skip-all:focus-visible", "a.wizard-link:focus-visible"):
        assert sel in ring, sel


# ---- #49 icon-button names -----------------------------------------------

def test_wish_row_icon_buttons_are_named():
    js = _read("js/views/welcome.js")
    assert 'aria-label="Move criterion ${i + 1} up"' in js
    assert 'aria-label="Move criterion ${i + 1} down"' in js
    assert 'aria-label="Remove criterion ${i + 1}"' in js
    assert '<span aria-hidden="true">✕</span>' in js


# ---- #51 weight stepper target -------------------------------------------

def test_wish_weight_input_matches_its_settings_sibling():
    css = _read("css/app.css")
    block = css[css.index(".wish-weight input {") : css.index(".wish-weight input {") + 500]
    assert "border: 1px solid var(--t-rule);" in block
    assert "padding: var(--t-space-3) var(--t-space-3);" in block
