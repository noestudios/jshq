"""tier1.py — pure filter evaluation against fixed params."""

import json

from jshq.scoring.tier1 import evaluate_tier1

PARAMS = {
    "comp_floor": 180000,
    "comp_target": 200000,
    "location_allowlist": ["evanston", "skokie", "oak park", "st. charles"],
    "company_location_overrides": {"Exampleco Group": ["copenhagen", "london"]},
    "remote_regions": ["united states", "us", "usa", "anywhere", "global", "california", "illinois"],
    "excluded_sectors": ["healthcare", "defense"],
    "target_title_bands": ["director", "senior_director"],
    "flag_title_bands": {
        "manager": "below_band",
        "senior_manager": "below_band",
        "ic": "below_band",
        "vp_plus": "scope_gap",
    },
}


def job(**overrides):
    base = {
        "company_name": "TestCo",
        "salary_stated": 1,
        "salary_min": 200000,
        "salary_max": 240000,
        "remote_type": "remote",
        "location": None,
        "level_band": "director",
    }
    base.update(overrides)
    return base


# --- comp ---------------------------------------------------------------


def test_comp_stated_below_floor_fails():
    r = evaluate_tier1(job(salary_max=175000), [], PARAMS)
    assert r.comp == "fail"
    assert r.hard_fail


def test_comp_unstated_is_unknown_never_fail():
    r = evaluate_tier1(job(salary_stated=0, salary_min=None, salary_max=None), [], PARAMS)
    assert r.comp == "unknown"
    assert "comp_unknown" in r.near_miss_flags
    assert not r.hard_fail


def test_comp_between_floor_and_target_flags():
    r = evaluate_tier1(job(salary_max=185000), [], PARAMS)
    assert r.comp == "pass"
    assert "comp_below_target" in r.near_miss_flags
    assert not r.hard_fail


def test_comp_above_target_clean():
    r = evaluate_tier1(job(salary_max=210000), [], PARAMS)
    assert r.comp == "pass"
    assert "comp_below_target" not in r.near_miss_flags


# --- location -----------------------------------------------------------


def test_bare_remote_passes_location():
    assert evaluate_tier1(job(remote_type="remote", location="Remote"), [], PARAMS).location == "pass"
    assert evaluate_tier1(job(remote_type="remote", location=None), [], PARAMS).location == "pass"


def test_us_scoped_remote_passes():
    for loc in ("Remote - US", "Remote, United States", "Remote California", "Remote - Anywhere"):
        assert evaluate_tier1(job(remote_type="remote", location=loc), [], PARAMS).location == "pass", loc


def test_us_state_abbreviation_remote_passes():
    # USPS codes and common spelled-short (AP) forms count as US-located, so the
    # 50 states needn't be listed in remote_regions (design decision, 2026-06-13).
    for loc in ("Remote, CA", "Remote - NY", "Remote (TX)", "Remote, Calif.", "Remote Miss.", "Remote, Wash."):
        assert evaluate_tier1(job(remote_type="remote", location=loc), [], PARAMS).location == "pass", loc


def test_no_space_paren_scope_remote_passes():
    # Lever writes "Remote(US)" with no space — punctuation must become a
    # space, not vanish (norm() fused it to "remoteus", which matched nothing:
    # caught live, 2026-08).
    for loc in ("Remote(US)", "Remote(USA)", "Remote(CA)"):
        assert evaluate_tier1(job(remote_type="remote", location=loc), [], PARAMS).location == "pass", loc


def test_dotted_acronym_scope_remote_passes():
    # Dotted forms collapse before punctuation-to-space, so "U.S." stays "us".
    for loc in ("Remote - U.S.", "Remote, N.Y."):
        assert evaluate_tier1(job(remote_type="remote", location=loc), [], PARAMS).location == "pass", loc


def test_us_state_recognition_gated_on_us_acceptance():
    # With no US/broader marker in remote_regions, a state scope is NOT widened.
    no_us = {**PARAMS, "remote_regions": ["evanston"]}
    r = evaluate_tier1(job(remote_type="remote", location="Remote, CA"), [], no_us)
    assert r.location == "fail"


def test_state_abbrev_does_not_match_foreign_country():
    # "ca" must not fire inside "canada"; non-US remote still fails.
    r = evaluate_tier1(job(remote_type="remote", location="Remote, Canada"), [], PARAMS)
    assert r.location == "fail"


def test_region_restricted_remote_fails():
    for loc in ("Remote Spain", "Sydney, , Australia", "Remote, Canada"):
        r = evaluate_tier1(job(remote_type="remote", location=loc), [], PARAMS)
        assert r.location == "fail", loc
        assert r.hard_fail


def test_us_token_never_matches_australia():
    r = evaluate_tier1(job(remote_type="remote", location="Remote Australia"), [], PARAMS)
    assert r.location == "fail"


def test_multi_region_remote_passes_when_us_listed():
    loc = "Remote, Canada; Remote, France; Remote, US"
    assert evaluate_tier1(job(remote_type="remote", location=loc), [], PARAMS).location == "pass"


def test_company_override_passes_onsite():
    r = evaluate_tier1(
        job(company_name="Exampleco Group", remote_type="onsite", location="Copenhagen"), [], PARAMS
    )
    assert r.location == "pass"


def test_company_override_is_per_company():
    r = evaluate_tier1(
        job(company_name="TestCo", remote_type="onsite", location="Copenhagen"), [], PARAMS
    )
    assert r.location == "fail"


def test_company_override_does_not_widen_other_locations():
    r = evaluate_tier1(
        job(company_name="Exampleco Group", remote_type="onsite", location="Odense"), [], PARAMS
    )
    assert r.location == "fail"


def test_allowlisted_town_passes():
    r = evaluate_tier1(job(remote_type="hybrid", location="Skokie, IL"), [], PARAMS)
    assert r.location == "pass"


def test_punctuation_insensitive_allowlist_match():
    r = evaluate_tier1(job(remote_type="onsite", location="St Charles, IL"), [], PARAMS)
    assert r.location == "pass"


def test_onsite_elsewhere_fails():
    r = evaluate_tier1(job(remote_type="onsite", location="Austin, TX"), [], PARAMS)
    assert r.location == "fail"
    assert r.hard_fail


def test_missing_location_is_unknown():
    r = evaluate_tier1(job(remote_type="unknown", location=None), [], PARAMS)
    assert r.location == "unknown"
    assert "location_unknown" in r.near_miss_flags
    assert not r.hard_fail


# --- location: blank slate (nothing configured, Phase 4) ----------------

BLANK_LOC = {
    **PARAMS,
    "location_allowlist": [],
    "company_location_overrides": {},
    "remote_regions": [],
    "location_radius": None,
}


def test_unconfigured_location_never_fails_onsite():
    # A blank-slate install must not reject a city-bearing onsite posting — with
    # no location filter set at all it reads "unknown", not "fail".
    r = evaluate_tier1(job(remote_type="onsite", location="Austin, TX"), [], BLANK_LOC)
    assert r.location == "unknown"
    assert not r.hard_fail
    assert "location_unknown" not in r.near_miss_flags  # nothing to warn about


def test_unconfigured_location_never_fails_scoped_remote():
    for loc in ("Remote Spain", "Remote, Canada", "Remote - Sydney"):
        r = evaluate_tier1(job(remote_type="remote", location=loc), [], BLANK_LOC)
        assert r.location == "unknown", loc
        assert not r.hard_fail, loc


def test_configuring_any_location_key_reactivates_filtering():
    # Adding just a remote_regions entry (allowlist still empty) re-arms the
    # filter: an out-of-scope onsite city fails again, as before Phase 4.
    only_regions = {**BLANK_LOC, "remote_regions": ["united states"]}
    r = evaluate_tier1(job(remote_type="onsite", location="Austin, TX"), [], only_regions)
    assert r.location == "fail"
    assert r.hard_fail


# --- location radius: commute minutes (Phase 7i) ------------------------

# Arlington Heights estimates ~39 drive-minutes from Evanston (15 straight-mi × 1.4 ÷ 33mph).
RADIUS_PARAMS = {
    **PARAMS,
    "location_radius": {
        "center": {"lat": 42.046391, "lng": -87.694352, "label": "Evanston, IL"},
        "radius_minutes": 45,
        "estimate": {"detour_factor": 1.4, "avg_mph": 33},
    },
}
TIGHT = {  # the example persona's ~30-min tolerance
    **PARAMS,
    "location_radius": {**RADIUS_PARAMS["location_radius"], "radius_minutes": 30},
}


def test_estimate_passes_town_under_threshold():
    # Arlington Heights (~39 est-min, not allowlisted) is within a 45-min radius.
    r = evaluate_tier1(job(remote_type="onsite", location="Arlington Heights, IL"), [], RADIUS_PARAMS)
    assert r.location == "pass"
    assert not r.hard_fail


def test_estimate_fails_town_over_threshold():
    # The same Arlington Heights falls outside a tighter 30-min radius.
    r = evaluate_tier1(job(remote_type="onsite", location="Arlington Heights, IL"), [], TIGHT)
    assert r.location == "fail"
    assert r.hard_fail


def test_far_town_fails():
    r = evaluate_tier1(job(remote_type="onsite", location="Milwaukee, WI"), [], RADIUS_PARAMS)
    assert r.location == "fail"


def test_measured_override_beats_estimate_both_ways():
    # A measured time wins over the estimate — rescues a close town the estimate
    # would reject, and rejects a far town the estimate would pass.
    close = {"arlington heights, il": 28}
    assert evaluate_tier1(job(remote_type="onsite", location="Arlington Heights, IL"), [], TIGHT, close).location == "pass"
    far = {"arlington heights, il": 55}
    assert evaluate_tier1(job(remote_type="onsite", location="Arlington Heights, IL"), [], RADIUS_PARAMS, far).location == "fail"


def test_without_radius_block_nearby_town_fails():
    # Regression: no location_radius key ⇒ pre-7i behavior (allowlist only).
    r = evaluate_tier1(job(remote_type="onsite", location="Arlington Heights, IL"), [], PARAMS)
    assert r.location == "fail"


def test_radius_does_not_rescue_unresolvable_location():
    r = evaluate_tier1(job(remote_type="onsite", location="Somewhere weird"), [], RADIUS_PARAMS)
    assert r.location == "fail"


def test_allowlisted_town_still_passes_with_radius():
    r = evaluate_tier1(job(remote_type="onsite", location="Evanston, IL"), [], TIGHT)
    assert r.location == "pass"


def test_malformed_radius_center_does_not_pass():
    bad = {**PARAMS, "location_radius": {"center": {"lat": 42.0}, "radius_minutes": 45}}  # no lng
    r = evaluate_tier1(job(remote_type="onsite", location="Arlington Heights, IL"), [], bad)
    assert r.location == "fail"


def test_nonpositive_threshold_does_not_pass():
    bad = {
        **PARAMS,
        "location_radius": {"center": {"lat": 42.046391, "lng": -87.694352}, "radius_minutes": 0},
    }
    assert evaluate_tier1(job(remote_type="onsite", location="Arlington Heights, IL"), [], bad).location == "fail"


def test_radius_augments_remote_branch():
    # Remote with no US marker normally fails a state scope; an in-range town
    # passes via the commute radius (it augments the remote OR-chain too).
    no_us = {**RADIUS_PARAMS, "remote_regions": ["evanston"]}
    r = evaluate_tier1(job(remote_type="remote", location="Remote - Arlington Heights, IL"), [], no_us)
    assert r.location == "pass"


# --- sector -------------------------------------------------------------


def test_excluded_sector_fails():
    r = evaluate_tier1(job(), ["healthcare"], PARAMS)
    assert r.sector == "fail"
    assert r.hard_fail


def test_no_sector_flags_passes():
    assert evaluate_tier1(job(), [], PARAMS).sector == "pass"
    assert evaluate_tier1(job(), None, PARAMS).sector == "pass"


def test_unrelated_sector_passes():
    assert evaluate_tier1(job(), ["fintech"], PARAMS).sector == "pass"


# --- title band ---------------------------------------------------------


def test_director_band_passes():
    assert evaluate_tier1(job(level_band="director"), [], PARAMS).title_band == "pass"


def test_manager_flags_below_band():
    r = evaluate_tier1(job(level_band="manager"), [], PARAMS)
    assert r.title_band == "flag:below_band"
    assert "below_band" in r.near_miss_flags
    assert not r.hard_fail


def test_vp_flags_scope_gap_never_fails():
    r = evaluate_tier1(job(level_band="vp_plus"), [], PARAMS)
    assert r.title_band == "flag:scope_gap"
    assert "scope_gap" in r.near_miss_flags
    assert not r.hard_fail


# --- composition --------------------------------------------------------


def test_hard_fail_is_any_single_fail():
    clean = evaluate_tier1(job(), [], PARAMS)
    assert not clean.hard_fail
    assert clean.near_miss_flags == []
    assert evaluate_tier1(job(salary_max=100000), [], PARAMS).hard_fail


def test_as_json_round_trips():
    data = json.loads(evaluate_tier1(job(), [], PARAMS).as_json())
    assert data == {
        "comp": "pass",
        "location": "pass",
        "sector": "pass",
        "title_band": "pass",
        "hard_fail": False,
    }
