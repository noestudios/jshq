"""Companies "Top jobs" must not present a delisted listing as a live role
(UX panel Pass B, #56).

Before: topJobsSection filtered out resolved applications but never closed /
delisted jobs, so a decay-closed req sat in "Top jobs" as a solid green fit chip
with no marker — while the Jobs view correctly showed it "NO LONGER LISTED" and
the header read "1 active job" against a count of 2.

Live-verified on the seeded review instance: Meridian's Top jobs dropped from 2
(incl. the closed 74) to 1 (the live 86), reconciling with "1 active job".
"""

from jshq import paths

FRONTEND = paths.FRONTEND_DIR


def _companies():
    return (FRONTEND / "js/views/companies.js").read_text(encoding="utf-8")


def test_top_jobs_excludes_delisted_listings():
    js = _companies()
    assert "isDelisted," in js  # imported from ui.js
    # the Top-jobs filter drops both resolved applications and delisted listings
    assert "!isResolvedApplication(j) && !isDelisted(j)" in js


def test_isdelisted_is_the_shared_ui_helper():
    # sanity: the helper the Jobs view uses for its "no longer listed" marker
    ui = (FRONTEND / "js/lib/ui.js").read_text(encoding="utf-8")
    assert "export function isDelisted(job)" in ui
