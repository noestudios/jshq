"""Tier 1 deterministic fit filters. Pure functions, no I/O.

Parameters come from the tier1_params block in DATA_DIR/fit_criteria.md via
criteria.load_criteria() — never hardcode criteria values here. Rules:
"unknown" is never a fail, title band never fails (flag only), and a hard
fail is any of comp/location/sector failing outright.
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

from jshq.ats.normalize import norm
from jshq.scoring import geo


@dataclass(frozen=True)
class Tier1Result:
    comp: str        # pass / fail / unknown
    location: str    # pass / fail / unknown
    sector: str      # pass / fail
    title_band: str  # pass / flag:below_band / flag:scope_gap
    hard_fail: bool
    near_miss_flags: list[str]

    def as_json(self) -> str:
        """Serialized for the jobs.tier1_results column."""
        return json.dumps(
            {
                "comp": self.comp,
                "location": self.location,
                "sector": self.sector,
                "title_band": self.title_band,
                "hard_fail": self.hard_fail,
            }
        )


_DOTTED_ACRONYM = re.compile(r"\b(?:[a-z]\.){1,}[a-z]\.?", re.I)


def _norm_loc(text: str | None) -> str:
    """Location normalization: dotted acronyms collapse ("U.S." -> "us",
    "N.Y." -> "ny"), then punctuation becomes a SPACE and whitespace
    collapses. normalize.norm() deletes punctuation outright, which fused
    Lever's "Remote(US)" into the unmatchable token "remoteus" (caught
    live, 2026-08) — but norm() also builds dedupe keys, where changing
    delete->space would shift existing keys, so this stays local to tier1."""
    if not text:
        return ""
    text = _DOTTED_ACRONYM.sub(lambda m: m.group(0).replace(".", ""), text.lower())
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text)).strip()


def _matches_any(loc: str, terms: list[str]) -> bool:
    """Word-boundary match of each normalized term against a normalized
    location. Substring matching would be wrong here: "us" must match
    "remote us" but never "australia"."""
    return any(
        re.search(rf"\b{re.escape(_norm_loc(term))}\b", loc)
        for term in terms
        if _norm_loc(term)
    )


# US geography lexicon (owner review, 2026-06-13): a remote role scoped to any
# US state — by full name, USPS code, or common spelled-short (AP) form — counts as
# US-located, so fit_criteria.md's remote_regions need not enumerate all 50.
# Erring toward inclusion. Gated by _us_remote() on remote_regions actually
# accepting US/broader remote, so clearing those markers there turns this off.
_US_BROAD_MARKERS = {
    "united states", "usa", "us", "america",
    "north america", "americas", "anywhere", "global", "worldwide",
}
_US_STATES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "west virginia", "wisconsin", "wyoming",
    "district of columbia", "washington dc",
}
# Official USPS two-letter codes + common spelled-short (AP-style) forms.
_US_STATE_ABBR = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id",
    "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms",
    "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok",
    "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv",
    "wi", "wy", "dc",
    "calif", "cali", "ariz", "ark", "colo", "conn", "fla", "ill", "ind",
    "kans", "mass", "mich", "minn", "miss", "mont", "nebr", "nev", "okla",
    "ore", "oreg", "penn", "penna", "tenn", "tex", "wash", "wis", "wisc", "wyo",
}
_US_GEO_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(t) for t in (_US_STATES | _US_STATE_ABBR)) + r")\b"
)


def _us_remote(loc: str, remote_regions: list[str]) -> bool:
    """A US-state-scoped remote role passes when remote_regions accepts US (or
    broader) remote — so the 50 states needn't be listed there explicitly."""
    if not any(norm(r) in _US_BROAD_MARKERS for r in remote_regions):
        return False
    return bool(_US_GEO_RE.search(loc))


def _within_commute(location: str | None, params: dict, drive_times: dict) -> bool:
    """Augments the allowlist (Phase 7i): a hybrid/onsite job whose town is within
    radius_minutes' drive of center passes even when it isn't allowlisted. A
    measured per-town drive-time (drive_times, keyed by geo.town_key) wins;
    otherwise an offline estimate. Off (a no-op) until a location_radius block
    exists. Operates on the raw location string — geo.py parses 'City, ST'."""
    cfg = params.get("location_radius")
    if not cfg:
        return False
    threshold = cfg.get("radius_minutes")
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool) or threshold <= 0:
        return False
    minutes = None
    if drive_times:
        key = geo.town_key(location)
        if key is not None:
            minutes = drive_times.get(key)
    if minutes is None:
        est = cfg.get("estimate") or {}
        minutes = geo.estimate_minutes(
            location, cfg.get("center"), est.get("detour_factor", 1.4), est.get("avg_mph", 33)
        )
    return minutes is not None and minutes <= threshold


def evaluate_tier1(
    job: Mapping, sector_flags: list[str], params: dict, drive_times: dict | None = None
) -> Tier1Result:
    flags: list[str] = []
    drive_times = drive_times or {}

    # Comp: stated max below floor fails; unstated is unknown, never fail;
    # stated in [floor, target) passes with a near-miss flag.
    if job["salary_stated"] and job["salary_max"] is not None:
        if job["salary_max"] < params["comp_floor"]:
            comp = "fail"
        elif job["salary_max"] < params["comp_target"]:
            comp = "pass"
            flags.append("comp_below_target")
        else:
            comp = "pass"
    else:
        comp = "unknown"
        flags.append("comp_unknown")

    # Location (owner decision, 2026-06-11). Hybrid/onsite must match the
    # location allowlist (Evanston-area in the shipped example) or this
    # company's overrides (e.g. Toronto/London for Meridian Loom).
    # Remote passes only when US-scoped or unscoped: region-restricted remote
    # elsewhere ("Remote Spain", a Sydney-based remote role) fails — it isn't
    # workable from the configured home area. Missing location is unknown,
    # never a fail.
    allowed_towns = list(params["location_allowlist"]) + list(
        params["company_location_overrides"].get(job["company_name"], [])
    )
    loc = _norm_loc(job["location"])
    # Blank slate (Phase 4): a fresh install configures no location filter at all
    # — empty allowlist, no company overrides, no remote_regions, no radius. An
    # unconfigured filter can neither pass nor fail a posting, so it reads
    # "unknown" and never hard-fails, and adds no near-miss flag (nothing was
    # asked to be warned about). Configuring ANY one of them activates the real
    # checks below, which reject an unlisted onsite/scoped-remote job as before.
    location_configured = bool(
        params["location_allowlist"]
        or params["company_location_overrides"]
        or params["remote_regions"]
        or params.get("location_radius")
    )
    if not location_configured:
        location = "unknown"
    elif job["remote_type"] == "remote":
        # Strip the word "remote" itself; whatever remains is the stated scope.
        scope = re.sub(r"\bremote\b", " ", loc).strip()
        if not scope:
            location = "pass"  # bare "Remote" — unscoped
        elif (
            _matches_any(loc, allowed_towns)
            or _within_commute(job["location"], params, drive_times)
            or _matches_any(loc, params["remote_regions"])
            or _us_remote(loc, params["remote_regions"])
        ):
            location = "pass"
        else:
            location = "fail"
    elif not loc:
        location = "unknown"
        flags.append("location_unknown")
    elif _matches_any(loc, allowed_towns) or _within_commute(job["location"], params, drive_times):
        location = "pass"
    else:
        location = "fail"

    # Sector: inherited from the company record.
    excluded = set(params["excluded_sectors"]) & {norm(s) for s in sector_flags or []}
    sector = "fail" if excluded else "pass"

    # Title band: flag-not-fail.
    band = job["level_band"]
    if band in params["target_title_bands"]:
        title_band = "pass"
    elif band in params["flag_title_bands"]:
        flag = params["flag_title_bands"][band]
        title_band = f"flag:{flag}"
        flags.append(flag)
    else:
        title_band = "pass"

    return Tier1Result(
        comp=comp,
        location=location,
        sector=sector,
        title_band=title_band,
        hard_fail="fail" in (comp, location, sector),
        near_miss_flags=flags,
    )
