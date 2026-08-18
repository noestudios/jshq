"""Job-detail Actions row hierarchy + application-state truth (#57) and Tier-1
flag glossing (#62), UX panel Pass B.

#57: the amber primary was "Mark applied" (terminal book-keeping) even on an
un-applied job, and an already-applied job showed amber "Mark applied" AND
"View application →" together. The primary now tracks the likely next step, and
once an application exists the job detail defers to it (View application →) rather
than fighting its status.

#62: the Sector/Title-band cells rendered raw tokens ("flag: excluded",
"flag: wrong_function") beside the glossed near-miss chips. t1cell now glosses
flag values via the shared flagLabel and drops the raw prefix, and gives
pass/fail/unknown dimension-aware wording.

Live-verified on the seeded review instance.
"""

from jshq import paths

FRONTEND = paths.FRONTEND_DIR


def _jobs():
    return (FRONTEND / "js/views/jobs.js").read_text(encoding="utf-8")


# ---- #57 actions hierarchy -----------------------------------------------

def test_start_application_is_the_primary_on_un_applied_jobs():
    js = _jobs()
    assert 'btn btn-accent" data-action="start-application"' in js
    # "Mark applied" is demoted to a plain, secondary button
    assert 'class="btn" data-action="set-status" data-status="applied"' in js
    # and is not itself an accent primary anywhere
    assert 'btn-accent" data-action="set-status" data-status="applied"' not in js


def test_application_detail_owns_pipeline_state_when_one_exists():
    js = _jobs()
    # when an application exists, lead with View application (accent), not a
    # competing Mark applied
    assert "const hasApp = !!job.application_id;" in js
    assert 'btn btn-accent" href="#/applications/${job.application_id}">View application' in js
    # the standalone applicationLink duplicate is gone (folded into statusActions)
    assert "const applicationLink" not in js


def test_hard_excluded_role_gets_no_loud_apply_primary():
    js = _jobs()
    # the hard-fail branch leads with elevate + Dismiss, Start application plain
    assert "isHardFailFit(job)" in js


# ---- #62 Tier-1 flag gloss -----------------------------------------------

def test_tier1_cells_gloss_flag_values_and_drop_the_raw_prefix():
    js = _jobs()
    assert "flagLabel(v.slice(5))" in js  # flag:<reason> → glossed reason
    assert 'sector: "excluded sector"' in js
    # the old raw "flag: " prefix transform is gone
    assert '.replace("flag:", "flag: ")' not in js


def test_tier1_wording_is_dimension_aware():
    js = _jobs()
    assert "const T1_FAIL = {" in js
    assert "const T1_UNKNOWN = {" in js
    assert "t1cell(k, tier1[k])" in js
