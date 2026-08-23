"""Onboarding primitives (Phase 4): the raw-exercise "roadmap" store and the
readiness signal behind the /api/onboarding routes.

The roadmap holds the user's RAW wishlist + fulfillment-matrix inputs verbatim —
their own words, kept so a later pass can synthesize them into criteria (in-app
with a key, or via a copy-paste prompt without one). Readiness reports which
setup steps are done so the first-run wizard and the always-visible completeness
tracker bind to one signal, derived structurally with no extra bookkeeping.

File I/O + pure logic only; the persisted onboarding *state* (a settings row) is
owned by the routes in main.py, which have the DB connection.
"""

import json

from jshq import apikey, paths
from jshq.scoring import criteria as criteria_mod

ROADMAP_MAX_BYTES = 256 * 1024


def roadmap_path():
    """DATA_DIR/roadmap.json, resolved at call time so first-run seeding and a
    test's monkeypatched DATA_DIR are both followed — same idiom as the voice
    guide."""
    return paths.DATA_DIR / "roadmap.json"


def read_roadmap() -> dict:
    """The saved raw exercise inputs, or {} when none exist / it is unreadable."""
    p = roadmap_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def write_roadmap(data: dict) -> dict:
    """Persist the raw exercise inputs atomically (temp + replace). Raises
    ValueError when the payload exceeds ROADMAP_MAX_BYTES (the endpoint 422s),
    writing nothing."""
    text = json.dumps(data, indent=2, ensure_ascii=False)
    if len(text.encode("utf-8")) > ROADMAP_MAX_BYTES:
        raise ValueError(f"roadmap is too large (max {ROADMAP_MAX_BYTES} bytes)")
    p = roadmap_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(p)
    return data


def build_readiness(
    company_count: int,
    api_key_declined: bool = False,
    api_key_rejected: bool = False,
    compat_configured: bool = False,
) -> dict:
    """Which setup steps are done, derived from the live config (no separate
    'completed' bookkeeping): anything a blank-slate install has NOT yet touched
    reads not-done. A broken criteria doc degrades every criteria-derived flag to
    not-done and surfaces the error rather than raising — this is labeling, and it
    must never take the app down (scoring still fails loud on the same doc).

    api_key_declined is the user's explicit "I don't want to use AI"
    choice: keyless is a first-class supported mode, so declining completes the
    api_key step the same as configuring one would. Without it a keyless-by-choice
    user is stranded below 100% forever, with no reachable control to finish.

    api_key_rejected is set when the currently-configured key last tested 401: a
    saved-but-rejected key is present but useless, so it must NOT complete the
    step or imply scoring is live (#33). A decline still wins — keyless is a
    valid finished state regardless of any stale verdict.

    compat_configured widens the step to "AI is decided": a configured
    OpenAI-compatible endpoint turns AI on without any Anthropic key, so it
    completes the step the same way a working key does — including when a
    stale rejected key is also lying around."""
    roadmap = read_roadmap()
    criteria_error = None
    field_done = hard_filters_done = wishlist_done = False
    try:
        c = criteria_mod.load_criteria()
        p = c.params
        field_done = not c.taxonomy_is_default
        hard_filters_done = bool(
            p["comp_floor"] or p["location_allowlist"] or p["company_location_overrides"]
            or p["remote_regions"] or p.get("location_radius") or p["excluded_sectors"]
            or p["target_title_bands"] or p["flag_title_bands"]
        )
        wishlist_done = bool(c.tier2)
    except criteria_mod.CriteriaError as exc:
        criteria_error = str(exc)

    # The matrix counts only when some cell has real content: the wizard co-writes
    # the matrix dict alongside the wishlist, so a dict of empty strings just means
    # "saved the other exercise", not "did this one".
    matrix = roadmap.get("matrix")
    matrix_done = isinstance(matrix, dict) and any(
        str(v or "").strip() for v in matrix.values()
    )

    # Note: neither the display name (persona) nor the voice guide is a counted
    # step. Both are optional and AI-only (a blank persona = anonymous, a valid
    # choice; the voice guide only shapes drafts and degrades gracefully), and
    # neither has a wizard step to satisfy — counting them would strand a user
    # below 100% with no way to finish from the flow. The field step covers the
    # profile; the voice guide stays discoverable in Settings → System.
    api_key_done = (
        api_key_declined
        or compat_configured
        or (apikey.is_configured() and not api_key_rejected)
    )
    steps = {
        "company": {"done": company_count > 0, "required": True},
        "api_key": {"done": api_key_done, "rejected": api_key_rejected},
        "field": {"done": field_done},
        "hard_filters": {"done": hard_filters_done},
        "wishlist": {"done": wishlist_done},
        "matrix": {"done": matrix_done},
    }
    return {
        "company_count": company_count,
        "steps": steps,
        "complete_count": sum(1 for s in steps.values() if s["done"]),
        "total": len(steps),
        "criteria_error": criteria_error,
    }
