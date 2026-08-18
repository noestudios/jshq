"""Structure & screen-reader a11y (UX panel review 2026-08-17): a real heading
hierarchy (#3 / A11Y-02), async status announced to AT (#12 / A11Y-04), and a
skip-to-content link (#23 / A11Y-09). No JS runtime here (see
test_settings_frontend), so the behavior is pinned against the shipped source.

WCAG 2.2: SC 1.3.1 / 2.4.6 (headings), SC 4.1.3 (status messages), SC 2.4.1
(bypass blocks).
"""

from jshq import paths

FRONTEND = paths.FRONTEND_DIR


def _read(rel):
    return (FRONTEND / rel).read_text(encoding="utf-8")


# ---- #3 heading hierarchy -------------------------------------------------

def test_section_helper_and_inline_headers_are_h2_not_span():
    today = _read("js/views/today.js")
    # the shared Today section() helper emits a heading
    assert '<h2 class="section-title">${esc(title)}</h2>' in today
    # a representative inline section header in each list view is now a heading
    for rel in ("js/views/jobs.js", "js/views/companies.js",
                "js/views/applications.js", "js/views/contacts.js"):
        assert '<h2 class="section-title">' in _read(rel), rel
    # and no section title is left as a styled <span> anywhere it was a real header
    # (the two disclosure-BUTTON titles are the deliberate exception below)


def test_disclosure_button_titles_stay_spans_not_headings():
    # A heading inside a <button> is invalid; these two are click targets, not
    # section headers, and must remain <span>.
    assert '<span class="section-title">Company settings</span>' in _read("js/views/companies.js")
    assert '<span class="section-title">Advanced — compiled from rules</span>' in _read("js/views/settings.js")


def test_detail_titles_are_h2_under_the_view_h1():
    # The selected-item title in each list view is demoted from <h1> to <h2> so
    # the single per-view <h1> (below) owns the top level.
    for rel in ("js/views/jobs.js", "js/views/companies.js", "js/views/contacts.js",
                "js/views/applications.js", "js/views/calendar.js"):
        src = _read(rel)
        assert '<h2 class="detail-title">' in src, rel
        assert "<h1" not in src, f"{rel} should have no <h1> (the shared #view-heading owns it)"


def test_shared_view_heading_h1_is_managed_inside_the_main_landmark():
    # #46: the single <h1> is created/retitled by app.js and kept as the first
    # child of <main id="view"> (a landmark) rather than orphaned in index.html
    # between </header> and <main>.
    html = _read("index.html")
    assert '<h1 id="view-heading"' not in html  # no longer static/orphaned
    app = _read("js/app.js")
    assert "function ensureViewHeading(route)" in app
    assert 'h.id = "view-heading";' in app
    assert "const VIEW_HEADINGS = {" in app
    for label in ("today:", "jobs:", "companies:", "contacts:", "applications:", "calendar:"):
        assert label in app
    # retitled + hidden (never an empty heading in the a11y tree), inserted first
    assert "h.textContent = label;" in app
    assert "h.hidden = !label;" in app
    assert "view.insertBefore(h, view.firstChild" in app


# ---- #12 announce async status -------------------------------------------

def test_today_banners_are_a_live_region():
    today = _read("js/views/today.js")
    assert '<div class="today-banners" role="status" aria-live="polite">' in today
    # error/offline banners escalate to assertive
    assert '<div role="alert" class="stale-banner banner-error">' in today


# ---- #23 skip link --------------------------------------------------------

def test_skip_link_present_and_focuses_view_without_touching_the_hash():
    html = _read("index.html")
    assert '<a class="skip-link" href="#view">Skip to main content</a>' in html
    # #view must be focusable as the target
    assert '<main id="view" tabindex="-1">' in html
    app = _read("js/app.js")
    # preventDefault + focus, so a bare "#view" never resolves to a route
    assert 'document.querySelector(".skip-link")?.addEventListener("click"' in app
    assert "event.preventDefault();" in app
    assert "view?.focus();" in app


def test_sr_only_utility_exists():
    css = _read("css/app.css")
    assert ".sr-only {" in css
    assert "clip-path: inset(50%);" in css
    assert ".skip-link {" in css
    assert ".skip-link:focus {" in css
