"""Tier-1 exclusions get an affirmative "N hidden by your filters" signal
(UX panel Pass B, #58 — the highest-leverage trust fix).

Before: the default "hide 0-fit jobs" switch dropped every hard-excluded role
with no count, badge, or line — the exclusion engine did the user's most-wanted
work and showed no evidence. Confirming it required the undiscoverable Fit
dropdown toggle.

No JS runtime here; the behavior is pinned against the shipped source and was
live-verified on the seeded review instance (1 role hidden → Show reveals it,
6→7 rows, switch flips off).
"""

from jshq import paths

FRONTEND = paths.FRONTEND_DIR


def _jobs():
    return (FRONTEND / "js/views/jobs.js").read_text(encoding="utf-8")


def test_hidden_count_helper_reuses_the_filter_predicate():
    js = _jobs()
    # The predicate is extracted so the count re-runs it with hideZeroFit off
    # rather than duplicating the filter rules (which would drift).
    assert "function matchesFilters(j, f, groups)" in js
    assert "function hiddenByHardFilters()" in js
    assert "const shown = { ...f, hideZeroFit: false };" in js
    assert "isHardFailFit(j) && matchesFilters(j, shown, groups)" in js


def test_notice_renders_in_the_list_body_so_it_repaints_with_the_switch():
    js = _jobs()
    assert "function hiddenNoticeHtml()" in js
    assert "function listBodyHtml()" in js
    # both render paths go through the shared body so the toggle path updates it
    assert js.count("listBodyHtml()") >= 2
    assert "hidden by your hard filters" in js


def test_show_flips_the_existing_reveal():
    js = _jobs()
    assert 'data-action="reveal-hidden"' in js
    assert 'case "reveal-hidden":' in js
    assert "state.filters.hideZeroFit = false;" in js
