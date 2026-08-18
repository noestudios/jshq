"""A11y polish batch (UX panel review 2026-08-17): light-theme muted contrast
(#4 / A11Y-03), non-hue banner distinction (#9 / A11Y-07/UI-05), associated
form labels (#11 / ORCH-01), 24px tap targets (#13 / A11Y-06), and a completed
(here: removed) partial ARIA tabs pattern (#21 / A11Y-08). No JS runtime here
(see test_settings_frontend), so the behavior is pinned against the shipped
source.

WCAG 2.2: SC 1.4.3 (contrast), SC 1.4.1 (use of colour), SC 1.3.1 / 4.1.2
(name/role/value), SC 2.5.8 (target size).
"""

from jshq import paths

FRONTEND = paths.FRONTEND_DIR


def _read(rel):
    return (FRONTEND / rel).read_text(encoding="utf-8")


# ---- #4 light-theme muted contrast ---------------------------------------

def test_light_muted_ink_darkened_off_the_failing_value():
    css = _read("css/tokens.css")
    # The old #73787c measured 3.69:1 on --t-bg and only 3.01:1 as muted text on
    # the selected-card fill --t-rule — both under the 4.5:1 body floor. (The
    # value survives only in the explanatory comment, never as the assignment.)
    assert "--t-ink-mute: #73787c;" not in css
    assert "--t-ink-mute: #565a5e;" in css


# ---- #9 non-hue banner distinction ---------------------------------------

def test_banners_carry_a_tone_shape_icon():
    today = _read("js/views/today.js")
    # a shape cue (not hue) distinguishes the green info bar from the amber warning
    assert "function bannerIcon(html)" in today
    assert "function withIcon(html)" in today
    # icon inherits the banner ink and is hidden from AT (the text carries meaning)
    assert 'class="banner-icon"' in today
    assert 'aria-hidden="true"' in today
    # wired into all three banner assembly paths
    assert 'withIcon(`<div class="stale-banner banner-progress">' in today
    assert "completion = withIcon(" in today
    assert today.count("withIcon(") >= 4  # helper def + 3 assembly paths
    css = _read("css/app.css")
    assert ".banner-icon {" in css


# ---- #11 associated labels -----------------------------------------------

def test_search_box_has_an_accessible_name():
    ui = _read("js/lib/ui.js")
    assert 'aria-label="${esc(placeholder)}" placeholder="${esc(placeholder)}"' in ui


def test_company_settings_inputs_have_aria_labels():
    comp = _read("js/views/companies.js")
    for name in ("Status", "Priority", "Values fit", "Location", "Website",
                 "Careers URL", "LinkedIn IDs (comma-sep)"):
        assert f'aria-label="{name}"' in comp, name
    assert 'aria-label="Notes"' in comp
    assert 'aria-label="LinkedIn role titles"' in comp


def test_settings_comp_inputs_have_aria_labels():
    s = _read("js/views/settings.js")
    assert 'data-param="comp_floor" aria-label="Comp floor ($)"' in s
    assert 'data-param="comp_target" aria-label="Comp target ($)"' in s


# ---- #13 24px tap targets ------------------------------------------------

def test_small_icon_buttons_meet_24px_target():
    css = _read("css/app.css")
    # banner dismiss grew 22 -> 24
    assert "width: 24px;\n  height: 24px;" in css
    # tag remove + wizard reorder arrows get an explicit 24px floor
    tagx = css[css.index(".settings-tag-x {"):css.index(".settings-tag-x:hover")]
    assert "min-width: 24px;" in tagx and "min-height: 24px;" in tagx
    wish = css[css.index(".wish-btn {"):css.index(".wish-btn:hover")]
    assert "min-width: 24px;" in wish and "min-height: 24px;" in wish


# ---- #21 partial ARIA tabs removed ---------------------------------------

def test_settings_tabs_are_plain_nav_not_a_partial_tablist():
    s = _read("js/views/settings.js")
    # the incomplete ARIA tab roles are gone (no aria-controls / tabpanel / roving
    # tabindex / arrow-key nav ever existed, so the roles were lying)
    assert 'role="tablist"' not in s
    assert 'role="tab"' not in s
    # the aria-selected wiring is gone (the surviving mention is the explanatory
    # comment in switchTab, not live markup or a setAttribute call)
    assert 'aria-selected="' not in s
    assert 'setAttribute("aria-selected"' not in s
    # labelled navigation with the current section marked
    assert '<nav class="settings-tabs" aria-label="Settings sections">' in s
    assert 'aria-current="page"' in s
    assert 'b.setAttribute("aria-current", "page")' in s
