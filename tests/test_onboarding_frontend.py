"""Onboarding frontend wiring (Phase 4): the first-run wizard, the router-gate
takeover, and the persistent completeness tracker.

No JS runtime in the suite, so — like test_settings_frontend — the UI behavior
is pinned by asserting on the shipped source plus the API contract it consumes.
The backend halves of these contracts are covered in test_onboarding_api."""

import re

from jshq import paths
from jshq.onboarding import build_readiness

FRONTEND = paths.FRONTEND_DIR


def _read(rel):
    return (FRONTEND / rel).read_text(encoding="utf-8")


def test_api_js_defines_the_onboarding_methods():
    api = _read("js/api.js")
    assert 'request("GET", "/api/onboarding")' in api
    assert 'request("PUT", "/api/onboarding", body)' in api
    assert 'request("GET", "/api/onboarding/roadmap")' in api
    assert 'request("PUT", "/api/onboarding/roadmap", body)' in api
    assert 'request("PUT", "/api/scoring/discipline", { field })' in api
    assert 'request("GET", "/api/scoring/criteria-example")' in api


def test_index_ships_the_tracker_node_hidden_left_of_stats():
    html = _read("index.html")
    # hidden at boot: an empty visible box would join .topbar-row's baseline
    # alignment with a synthesized (wrong) baseline and shift the nav.
    assert '<div id="onboarding-tracker" hidden></div>' in html
    assert html.index('id="onboarding-tracker"') < html.index('id="topbar-stats"')


def test_app_js_gates_first_run_and_wires_the_tracker():
    app = _read("js/app.js")
    # The wizard IS the first-run UI: every route resolves to welcome while
    # first_run holds, and the welcome route hides the chrome.
    assert 'route = "welcome"' in app
    assert '"wizard-active"' in app
    # Tracker: seeded once from boot's fetch, refreshed (unawaited) per render.
    assert 'from "./lib/onboardingTracker.js"' in app
    assert "onboardingTracker.seed(" in app
    assert "onboardingTracker.refresh(" in app


def test_tracker_counts_the_backend_and_links_to_the_wizard():
    js = _read("js/lib/onboardingTracker.js")
    assert "api.getOnboarding()" in js  # the aggregate GET — no client recount
    assert 'href="#/welcome"' in js  # the way back into setup
    assert "complete_count" in js and "total" in js
    # Visibility guards: broken doc, first-run (the wizard IS the screen), 8/8.
    assert "criteria_error" in js and "first_run" in js
    assert "hidden = true" in js  # baseline-safe hide


def test_tracker_carries_a_dismiss_control():
    """FLOW-02: the pill's own "I'm set — hide this" ✕. Optional steps are
    content-derived, so a deliberately-blank one never flips done — the ✕ lets a
    user acknowledge the nudge without changing readiness semantics."""
    js = _read("js/lib/onboardingTracker.js")
    # shouldShow gates on the persisted flag (payload key tracker_dismissed).
    assert "tracker_dismissed" in js
    assert "!p.tracker_dismissed" in js
    # The ✕ is a real button with an accessible name, NOT nested in the pill's
    # anchor (a control inside <a> is invalid and would also follow the link) —
    # it rides as a sibling inside a wrapper.
    assert 'data-action="dismiss-tracker"' in js
    assert 'aria-label="Hide setup tracker"' in js
    assert "ob-tracker-wrap" in js
    # Reuses the existing close-button class from the a11y pass (24px tap target);
    # no new CSS class is invented here.
    assert "banner-dismiss" in js
    # The click persists the acknowledgement and hides optimistically, best-effort.
    assert 'api.putSetting("onboarding_tracker_dismissed", true)' in js
    assert ".catch(() => {})" in js  # a failed save never throws uncaught


def test_dismiss_reuses_an_existing_close_button_class():
    # The reused class must actually exist in the shipped stylesheet (the a11y
    # 24px tap-target button), so the ✕ is styled without touching app.css.
    css = _read("css/app.css")
    assert ".banner-dismiss {" in css


def test_companies_add_refreshes_the_tracker():
    js = _read("js/views/companies.js")
    # setDetailHash uses pushState — no hashchange, no render(), so the required
    # step's flip must nudge the pill explicitly.
    assert 'from "../lib/onboardingTracker.js"' in js
    assert "onboardingTracker.refresh(" in js


def test_wizard_wires_every_step_action():
    js = _read("js/views/welcome.js")
    for action in (
        "next", "back", "skip", "skip-all", "finish", "save-test-key",
        "wish-add", "wish-up", "wish-down", "wish-remove", "wish-edit",
        "jump", "toggle-example",
    ):
        assert f'data-action="{action}"' in js, f"missing control for {action}"
        assert f'action === "{action}"' in js or f'"{action}"' in js


def test_wizard_saves_a_typed_key_on_continue():
    js = _read("js/views/welcome.js")
    # The review's top finding: the primary button silently discarded a pasted
    # key (next() had no "key" branch).
    assert 'if (id === "key")' in js
    assert "storeAndTestKey(" in js


def test_wizard_hydrates_and_round_trips_the_filters():
    js = _read("js/views/welcome.js")
    # The data-loss fix: an untouched Continue must round-trip, never zero.
    assert "s.compFloor = t1.comp_floor" in js
    assert "(t1.location_allowlist || []).join" in js  # degraded flat fallback
    assert "REMOTE_US_TOKENS" in js
    assert "regions.delete(r)" in js  # unchecking removes; not a one-way ratchet


def test_wizard_towns_field_edits_only_the_manual_allowlist():
    """The collision fix: location_allowlist is co-owned by the inclusion-rules
    compiler. The wizard must hydrate only the manual extras into its editable
    field, show rule-owned towns read-only, and echo them (refreshed at save
    time) back into every criteria write — else deleting a rule town here
    silently diverges the doc and the next rules save resurrects the town."""
    js = _read("js/views/welcome.js")
    assert "api.getInclusionRules().catch" in js
    assert 'filter((e) => e.source === "rule")' in js
    assert 'filter((e) => e.source === "manual")' in js
    assert "new Set([...s.ruleTowns, ...manualTowns])" in js
    assert "from your\n      inclusion rules" in js or "from your inclusion rules" in js


def test_wizard_home_base_writes_the_drive_time_circle():
    js = _read("js/views/welcome.js")
    # Home base + minutes → tier1's location_radius via the offline geocoder;
    # an unknown town blocks with an inline error, an unchanged label keeps the
    # resolved center, and blank turns the circle off (Settings semantics).
    assert "api.geocode(home)" in js
    assert "location_radius" in js
    assert "radius_minutes" in js
    assert 'data-field="homeTown"' in js
    assert 'data-field="driveMins"' in js


def test_wizard_filters_capture_excluded_sectors_and_target_bands():
    """#32: the "hard limits" step must be able to set the exclusions that
    matter most — sectors (a hard filter) and the target seniority ladder — not
    only comp + location. Both ride the same putCriteria the step already
    sends; band choices come from levelBands() so every value is emittable."""
    js = _read("js/views/welcome.js")
    # The picker reads the seniority vocabulary, loaded at mount.
    assert 'from "../lib/vocab.js"' in js
    assert "loadVocab()" in js
    assert "levelBands()" in js
    # Sectors: a comma text field hydrated from + written to excluded_sectors.
    assert 'data-field="sectors"' in js
    assert "s.sectors = (t1.excluded_sectors" in js
    assert "params.excluded_sectors = s.sectors" in js
    # Target bands: checkboxes driving target_title_bands via a toggle handler.
    assert 'data-action="toggle-band"' in js
    assert "s.titleBands = Array.isArray(t1.target_title_bands)" in js
    assert "params.target_title_bands = [...s.titleBands]" in js


def test_wizard_rank_drives_weight_and_preserves_markers():
    js = _read("js/views/welcome.js")
    assert "rampedTier2" in js  # the reorder ceremony has a scoring effect
    assert "bonus_only" in js  # Tier2 items round-trip whole, markers intact


def test_wishlist_flashes_arrivals_and_edits_in_place():
    ui = _read("js/lib/ui.js")
    assert "export function flashRow" in ui  # the jobs-list arrival cue, shared
    js = _read("js/views/welcome.js")
    assert "flashRow(" in js  # added + reordered items get the cue
    css = _read("css/app.css")
    assert ".wish-item.row-flash::after" in css
    # In-place editing: click the text, Enter/blur commits, Escape cancels,
    # an emptied editor cancels rather than silently deleting.
    assert 'data-field="editDraft"' in js
    assert 'e.key === "Escape"' in js
    assert "commitEdit" in js


def test_wizard_consumes_the_example_and_polls_the_created_company():
    js = _read("js/views/welcome.js")
    assert "api.getCriteriaExample()" in js  # the built affordance, finally used
    assert "api.getCompany(" in js  # done-step live status
    assert "ats_last_status" in js


def test_wizard_treats_a_duplicate_company_as_satisfied():
    js = _read("js/views/welcome.js")
    # POST /api/companies 409s on a duplicate with the existing row's id in the
    # structured detail; the wizard reuses that row instead of failing the
    # required step.
    assert "err.status !== 409" in js
    assert "err.info?.company_id" in js


def test_company_step_shows_the_board_and_enforces_required():
    js = _read("js/views/welcome.js")
    # The required step shows its evidence (existing companies as ruled rows)
    # and means it: while the board is empty there is no FIRST-RUN exit
    # ANYWHERE in the flow (owner call, 2026-08-16 — exiting earlier landed on
    # an empty dashboard wearing an error face) and no company-step skip; once
    # companies exist both return. A RETURNING user's exit is plain navigation
    # back to their dashboard and always renders — hiding it trapped a user
    # who later deleted every company in the hub with the app chrome hidden.
    assert "api.listCompanies()" in js
    assert "companyBoardHtml" in js
    assert "function companyRequired" in js
    assert '!== "done" && (returning() || !companyRequired())' in js
    assert 'id === "company" && !companyRequired()' in js  # skippable once met


def test_today_keeps_day_one_calm():
    js = _read("js/views/today.js")
    # A fresh install's landing must not stack "stale · never" + "no backup yet"
    # warnings over a page of zeros.
    assert "const dayOne = !state.lastRefresh" in js
    assert "Connecting your first job board" in js
    # #34: the backup-missing banner now gates on real data to protect (hasJobs),
    # not on last_refresh — a no-op refresh can't unmask it seconds in — and its
    # copy no longer implies a scheduled nightly job the app never sets up.
    assert "if (hasJobs) out.push({ key: \"backup-missing\"" in js
    assert "the nightly job hasn't run" not in js
    app = _read("js/app.js")
    assert "if (!status.connectable) return;" in app  # skip the no-op day-one refresh


def test_takeover_css_hides_the_chrome():
    css = _read("css/app.css")
    assert "body.wizard-active #nav-tabs" in css
    assert "body.wizard-active #onboarding-tracker" in css
    assert ".ob-tracker" in css


def test_wizard_offers_the_synthesis_handoff():
    """Done step + welcome-back hub point at Settings → Scoring when raw words
    exist; the link parks a one-shot tab handoff that settings.js honors
    (hash routing carries no params)."""
    js = _read("js/views/welcome.js")
    assert 'data-action="go-synthesis"' in js
    assert 'sessionStorage.setItem("jshq-settings-tab", "scoring")' in js
    assert "synthesisLineHtml" in js
    settings = _read("js/views/settings.js")
    assert 'sessionStorage.getItem("jshq-settings-tab")' in settings


def test_wishlist_weights_are_editable_and_survive_continue():
    """Owner decision: hand-set weights always survive; the rank ramp
    re-derives only after an actual reorder and only for never-touched items.
    An untouched Continue sends the list verbatim (the old unconditional
    rampedTier2() quietly clobbered Settings-set weights)."""
    js = _read("js/views/welcome.js")
    assert 'data-wish-weight data-i="${i}" min="0.25" max="4" step="0.25"' in js
    assert "function finalTier2()" in js
    assert "if (!s.orderChanged) return s.wishlist" in js  # the verbatim path
    assert "s.touchedWeights.has(item) ? item : ramped[i]" in js
    assert "tier2_criteria: finalTier2()" in js
    assert "tier2_criteria: rampedTier2()" not in js  # the clobber path is gone


def test_wishlist_hand_weights_survive_a_save_then_reorder():
    """The save must not defeat the weight protection. saveWishlist used to
    replace s.wishlist with the fresh response objects AND clear touchedWeights,
    so after any save a later reorder re-ramped every hand-set weight (the marks
    were tracked by object identity, which the replacement broke). It must merge
    the response into the existing objects (like saveFilters) and keep the set."""
    js = _read("js/views/welcome.js")
    # saveWishlist merges by identity instead of adopting fresh objects
    assert "resp.tier2_criteria.forEach((item, i) => {" in js
    assert "if (s.wishlist[i]) Object.assign(s.wishlist[i], item);" in js
    # and never blows the touched set away (that was the double-defeat)
    assert "s.touchedWeights = new Set()" not in js
    # a returning user's persisted hand weights are seeded as touched at mount,
    # so their first reorder/add doesn't re-ramp them either
    assert "function seedTouchedWeights()" in js
    assert "seedTouchedWeights();" in js
    assert "if (item.weight !== ramped[i].weight) s.touchedWeights.add(item);" in js


def test_wishlist_shows_the_effective_weight_and_one_remove_glyph():
    """#31/#43/#38: the weight shown must equal the weight saved (an eager ramp
    folds rank into item.weight on every structural change), the row must carry
    one × (the remove control, not a second × prefix on the weight), and the
    deferred craft/bonus axes are pointed at Settings."""
    js = _read("js/views/welcome.js")
    css = _read("css/app.css")
    # #31 eager ramp: applied at add, remove, and move so the field never shows
    # a placeholder 1 while a descending ramp is what persists.
    assert "function applyRamp()" in js
    assert js.count("applyRamp();") >= 3
    # any edit to the weight field is a deliberate pin (the equality guard is
    # gone from the live handler — re-affirming the shown value now pins too).
    assert "deliberate pin — even re-affirming" in js
    # #43: the "×" weight prefix is gone; a text label replaces it, remove ✕ stays.
    assert "wish-weight-mul" not in js
    assert '<span class="wish-weight-label">Weight</span>' in js
    assert 'data-action="wish-remove"' in js and "✕" in js
    assert ".wish-weight-label" in css
    # #38: the craft/bonus axes are deferred to Settings with a visible note.
    assert "central trade-off" in js and "upside-only" in js


def test_build_readiness_demotes_a_rejected_key(monkeypatch):
    """#33: a configured key that last tested 401 must not complete the step,
    but an explicit decline still wins over a stale rejection."""
    from jshq import apikey

    monkeypatch.setattr(apikey, "is_configured", lambda: True)
    ok = build_readiness(1)["steps"]["api_key"]
    assert ok["done"] is True and ok["rejected"] is False
    bad = build_readiness(1, api_key_rejected=True)["steps"]["api_key"]
    assert bad["done"] is False and bad["rejected"] is True
    declined = build_readiness(1, api_key_declined=True, api_key_rejected=True)["steps"]["api_key"]
    assert declined["done"] is True


def test_wizard_and_board_read_a_rejected_key_as_unusable():
    """#33: the "scoring/AI is on" copy keys on keyUsable (configured AND not
    rejected), not raw configured; the key step carries a standing 401 warning;
    Continue refreshes readiness; and the board demotes a rejected key too."""
    js = _read("js/views/welcome.js")
    assert "function keyUsable()" in js
    assert "!s.onboarding?.api_key_rejected" in js
    assert "const hasKey = keyUsable();" in js
    assert "const keyless = !keyUsable();" in js
    # standing (not transient) 401 note on the key step
    assert "was rejected (401) when last tested" in js
    # Continue now refreshes the tracker/badge after saving the key
    key_next = js.split('if (id === "key") {')[1].split("} else if")[0]
    assert "await syncOnboarding();" in key_next
    today = _read("js/views/today.js")
    assert "!keyStatus.rejected" in today


def test_wizard_resumes_the_step_on_reload():
    """#39: render() re-mounts at step 0, so a mid-wizard reload dropped a
    first-run user to the cold Welcome pitch though the config was all saved.
    Persist the step per-tab and resume it (first-run only — a returning user
    gets the hub), clearing the breadcrumb on every exit."""
    js = _read("js/views/welcome.js")
    assert 'const WIZARD_STEP_KEY = "jshq-wizard-step"' in js
    assert "sessionStorage.setItem(WIZARD_STEP_KEY" in js
    assert "sessionStorage.getItem(WIZARD_STEP_KEY)" in js
    assert "if (s.onboarding?.first_run) {" in js  # restore only during first-run
    assert "saved >= 1 && saved <= SETUP_TOTAL" in js  # only a real setup step
    assert js.count("clearResumeStep();") >= 2  # cleared on dismiss + finish


def test_wizard_key_step_offers_declining_a_key():
    """The setup tracker's api_key step is only reachable through the wizard key
    step (the tracker links to the welcome hub, which jumps here), so a keyless
    user must be able to opt out here: a checkbox that completes the step,
    disables the field + Save & test, and refreshes the badge/tracker."""
    js = _read("js/views/welcome.js")
    assert 'data-action="toggle-decline-key"' in js
    assert "function toggleDeclineKey(declined)" in js
    assert 'api.putSetting("api_key_declined", declined)' in js
    # checking it disables the key field and Save & test
    assert "const fieldOff = shadowed || declined;" in js
    assert "const saveOff = shadowed || testing || declined;" in js
    # only offered when there is no key at all
    assert "const noKey = !st?.configured;" in js


def test_profile_step_shows_the_criteria_load_banner():
    """The profile step writes name + field through the criteria doc, so it must
    surface the same load-failure banner the filters/wishlist steps show — but
    without the 'words are kept safe' line, since name/field have no raw store."""
    js = _read("js/views/welcome.js")
    profile = js.split("function profileStep()")[1].split("\nfunction ")[0]
    assert "criteriaBanner({ wordsSafe: false })" in profile
    assert "const safe = wordsSafe ?" in js  # the reassurance is now conditional


def test_wizard_field_step_writes_the_first_sourcing_rule():
    # Phase 5b: the ingestion gate ships empty, so the field answer must feed
    # it or a Workday-only board pulls nothing and every other board pulls
    # everything, unnarrowed. The write uses the reserved "wizard-field" rule
    # id (replace-on-rerun, other rules and manual chips echoed verbatim) and
    # runs LAST in saveProfile so a failure re-runs cleanly on the next
    # Continue.
    js = _read("js/views/welcome.js")
    assert 'r.id !== "wizard-field"' in js
    assert '{ id: "wizard-field", verb: "include", target: "title", terms }' in js
    assert js.index("putDiscipline(field)") < js.index("putInclusionRules")
    # the hint that makes the coupling visible at the point of decision
    assert "matches these words against job titles" in js


def test_wizard_company_step_asks_the_add_modal_fields():
    js = _read("js/views/welcome.js")
    # Owner-flagged: the one-field company step under-specified new companies.
    # The step now asks what the Companies add modal asks, plus the careers
    # URL detection feeds on (its own field — the most reliable input).
    for field in (
        "companyName", "companyUrl", "companyCareers",
        "companyLocation", "companyPriority", "companyStatus", "companyValues",
    ):
        assert f'data-field="{field}"' in js, f"missing wizard input for {field}"
    assert "careers_url" in js  # saveCompany forwards it


def test_companies_view_wires_url_edit_reprobe_and_check_again():
    api = _read("js/api.js")
    assert "/detect`)" in api  # api.detectCompanyBoard
    js = _read("js/views/companies.js")
    # The manual-tracking state still has a way out: Check again re-runs detection.
    assert 'data-action="detect-board"' in js
    assert 'case "detect-board":' in js
    # URL edits commit on blur/change only — the quiet mid-typing autosave
    # would PUT half-typed URLs, each now firing the backend re-probe.
    assert 'field === "careers_url" || field === "website"' in js
    # A PUT that comes back 'checking' is watched to a settled outcome toast.
    assert "onDetectSettled" in js


def test_blur_saves_ride_addeventlistener_not_the_onfocusout_property():
    # The onfocusout PROPERTY is not a GlobalEventHandler in every engine —
    # older WebKit fires the focusout EVENT but has no property slot, so a
    # property-wired blur-save silently never fires there (live-found: the
    # careers-URL edit never saved in an embedded WebKit pane). All views go
    # through ui.setFocusOut (addEventListener under the hood).
    assert "export function setFocusOut" in _read("js/lib/ui.js")
    for rel in ("companies", "welcome", "contacts", "applications", "calendar", "jobs"):
        js = _read(f"js/views/{rel}.js")
        assert ".onfocusout" not in js, f"{rel}.js still assigns the onfocusout property"


# --- Wizard-review polish (pre-Phase-6): stateful key step, careers
# auto-population, tip-icon hints. Backend halves live in test_companies. ---


def test_key_step_is_stateful_when_a_key_is_already_set():
    js = _read("js/views/welcome.js")
    # A configured key hides the raw field behind Test / Change affordances,
    # with a Cancel to back out of an in-progress replacement.
    for action in ("test-key", "change-key", "cancel-change-key"):
        assert f'data-action="{action}"' in js, f"missing key-step action {action}"
    assert "s.changingKey" in js  # the editing/not-editing toggle
    # The editing view is gated on "no key yet, or the user chose to change it".
    assert "s.changingKey || !st?.configured" in js


def test_api_js_defines_the_careers_preview_method():
    api = _read("js/api.js")
    assert 'request("POST", "/api/companies/careers-preview", body)' in api


def test_wizard_company_step_auto_populates_the_careers_field():
    js = _read("js/views/welcome.js")
    # The website blur runs the no-write probe and fills the careers field.
    assert "api.previewCareers(" in js
    assert 'field === "companyUrl"' in js  # the blur trigger
    assert "s.careersSearch" in js  # the Finding…/Found…/Didn't-find line
    # A hand-typed careers URL is authoritative — the probe never overwrites it.
    assert "s.careersAutofilled" in js


def test_wizard_hints_carry_the_inline_svg_tip_icon():
    js = _read("js/views/welcome.js")
    # The tip marker is an inline SVG in the app's icon set (stroke, currentColor),
    # not a 💡 emoji.
    assert "function hintIcon()" in js
    assert 'stroke="currentColor"' in js
    assert "💡" not in js, "use the inline-SVG icon set, not the lightbulb emoji"
    assert "${hintIcon()}" in js  # actually prepended to hint copy


# --- Second wizard-review round (owner live-walkthrough): careers fill fix,
# stateful-key polish, welcome-back voice-guide row, LinkedIn seeding. ---


def test_careers_autofill_reaches_a_focused_empty_field():
    # The bug: paintCareers skipped filling while the careers input was focused,
    # so tabbing into it left it blank while the status claimed it was filled.
    # The fill must only be skipped for a focused field the user is TYPING in.
    js = _read("js/views/welcome.js")
    assert 'input.value !== ""' in js  # the "still empty" guard
    assert "const typing =" in js


def test_change_api_key_is_never_a_dead_disabled_button():
    # Env-shadowed used to render Change disabled (read as broken). It stays
    # enabled and reveals the edit view, whose field/Save carry the disable.
    js = _read("js/views/welcome.js")
    change = js.split('data-action="change-key"')[1][:80]
    assert "shadowed" not in change, "Change API key must not be disabled when shadowed"


def test_welcome_back_hub_points_at_the_uncounted_voice_guide():
    js = _read("js/views/welcome.js")
    # The voice guide is uncounted and lives in Settings, so it is a non-counted
    # pointer (links out), never a counted row with a jump into the flow.
    assert '"go-voice"' in js
    assert '"jshq-settings-tab", "system"' in js
    # It must not be one of the counted welcome-back rows.
    assert "st.voice_guide" not in js
    # The profile row no longer waits on the optional name (persona).
    assert "st.persona" not in js


def test_wizard_seeds_linkedin_defaults_from_the_field():
    js = _read("js/views/welcome.js")
    assert 'putSetting("linkedin_title_defaults"' in js


def test_wishlist_continue_commits_a_typed_but_unadded_draft():
    # The empty-ranking bug: a criterion typed into the add box but not Added was
    # dropped on Continue (saveWishlist read s.wishlist, never the draft). Continue
    # must flush the draft through the same commit path Enter/Add use.
    js = _read("js/views/welcome.js")
    assert "function commitWishDraft()" in js
    save = js.split("async function saveWishlist()")[1].split("async function")[0]
    assert "commitWishDraft()" in save, "saveWishlist must flush the typed draft"


def test_today_day_one_banner_yields_to_actual_jobs():
    # Add-time onboarding pulls jobs without stamping last_refresh, so day-one
    # must not claim "Nothing on the board yet" over a board that has jobs.
    js = _read("js/views/today.js")
    assert "const hasJobs =" in js
    assert "!(dayOne && hasJobs)" in js


def test_company_detail_explains_the_linkedin_role_checks():
    js = _read("js/views/companies.js")
    assert "linkedin-purpose" in js


# --- First-run value story (Issue #2 / PEER-01): done-step CTAs + a day-one
# scored-posting teaching preview; and the stale manual counter (Issue #20). ---


def test_wizard_setup_total_matches_the_backend_step_count():
    # Issue #20 (PEER-06/UI-09): the manual said "N/8" / "eight steps" but the
    # real total is 6. Pin the two sources of truth together so they can't drift:
    # the wizard's SETUP_TOTAL constant and the backend readiness step dict.
    js = _read("js/views/welcome.js")
    m = re.search(r"const SETUP_TOTAL = (\d+);", js)
    assert m, "welcome.js must declare SETUP_TOTAL"
    assert int(m.group(1)) == 6
    readiness = build_readiness(company_count=0)
    assert len(readiness["steps"]) == 6
    assert readiness["total"] == 6
    assert int(m.group(1)) == readiness["total"]


def test_user_manual_setup_counter_is_not_stale():
    # The manual must no longer promise eight steps (there are six).
    manual = (paths.DEFAULTS_DIR / "user-manual.md").read_text(encoding="utf-8")
    assert "N/8" not in manual
    assert "/8" not in manual
    assert "eight steps" not in manual
    assert "Setup N/6" in manual
    assert "six steps" in manual


def test_done_step_offers_first_class_value_ctas():
    # Issue #2 (PEER-01): the keyless / no-ATS persona finished onboarding to an
    # empty board with only prose. The done step now carries tappable CTAs.
    js = _read("js/views/welcome.js")
    assert "function doneCtas()" in js
    assert "${doneCtas()}" in js  # actually rendered on the done step
    # A one-tap "add a job you found" and the LinkedIn role checks both deep-link
    # to the created company's own detail page (the real add-job + role-check home).
    assert "Add a job you found" in js
    assert "LinkedIn role checks" in js
    assert "`#/companies/${c.id}`" in js
    # A keyless user gets a first-class "Add an API key" jump to Settings → System
    # (reusing the settings-tab handoff pattern), not just a line in "Still open".
    assert "Add an API key" in js
    assert 'data-action="go-key"' in js
    # #42: the three peer CTAs are ONE component (.btn), not two buttons + a stray
    # text link — with a single accent primary and the row given breathing room.
    ctas = js.split("function doneCtas()")[1].split("\nfunction ")[0]
    assert "wizard-link" not in ctas  # no bare text link amongst the buttons
    assert "btn-accent" in ctas  # exactly one promoted primary
    assert "wizard-cta-row" in ctas
    assert ".wizard-cta-row" in _read("css/app.css")
    assert 'action === "go-key"' in js
    assert '"jshq-settings-tab", "system"' in js
    # The nudge is scoped: the auto-pull + keyed happy path needs no CTA block.
    assert "if (!noAuto && !keyless) return" in js


def test_today_seeds_a_day_one_scored_posting_example():
    # Part 2: the day-one empty board seeds a clearly-labelled EXAMPLE scored
    # posting, built from the real row parts (fitChip + company-row markup).
    js = _read("js/views/today.js")
    assert "function exampleCard()" in js
    assert "EXAMPLE_JOB" in js
    assert "exampleCard()" in js  # rendered into the New jobs empty state
    # Gated to an empty board (job count), not last_refresh: a no-ATS user's
    # first fruitless refresh stamps last_refresh but leaves the board empty, so
    # the payoff preview must persist until a real posting lands, and vanish the
    # moment any job arrives.
    assert "!state.jobs.length" in js
    assert "!state.lastRefresh && !active.length" not in js
    # Unmistakably an example and non-interactive: a caption, and NO data-action /
    # data-id / role on the preview row (so nothing opens or navigates).
    assert "Example — not a real posting" in js
    card = js.split("function exampleCard()")[1].split("\nconst COLLAPSE_AT")[0]
    assert "today-example-row" in card
    assert "data-action" not in card, "the example row must not be clickable"
    assert 'data-id="' not in card
    assert 'role="button"' not in card
    # It reads the key status (fetched in load) to caption the keyless case, and
    # the no-ATS case off state.noAts — without a new external network call.
    assert "state.keyConfigured" in js
    assert "getApiKeyStatus()" in js
    assert "state.noAts.length" in card
    # #37: the card is a fixed off-domain example, so its "why" bullets must not
    # claim personalization ("your salary floor" / "your top-ranked criteria")
    # the user never set for this static role.
    why = card.split('today-example-why')[1].split("</ul>")[0]
    assert "your" not in why.lower()


def test_privacy_and_honesty_copy_is_preserved():
    """#27 (PEER-08): the privacy/honesty copy is the product's biggest asset for
    a protective, tired user — a promise that data stays local, an honest outbound
    inventory, "AI is optional and everything else still works", and a first-class
    decline option. Pin the load-bearing phrases so a future copy edit can't
    quietly hollow them out. (The exact prose can change; these commitments can't
    vanish without this test failing and forcing a deliberate re-bless.)"""
    js = _read("js/views/welcome.js")
    # The welcome step's data-stays-local promise + honest outbound inventory.
    assert "it stays yours" in js
    assert "No accounts, no tracking, no cloud" in js
    assert "The only things that ever leave are" in js
    # The key step is honest that AI is optional and the rest works without it.
    assert "everything else still works" in js
    # A real, first-class decline option (not a dark-pattern nag to add a key).
    assert 'data-action="toggle-decline-key"' in js
    assert "I don't want to use an API key" in js
    # The schoolmarm "highly encouraged to spend the time" line was warmed (#27
    # nit) — guard against a regression back to it.
    assert "highly encouraged" not in js


def test_wizard_css_block_uses_scale_tokens_not_raw_literals():
    """UI-02 (#14): the wizard block was the sole holdout using raw decimal-rem
    font-size literals — a tokens.css scale change never reached the first screen
    every user sees. Guard that the whole onboarding-wizard region (from `.wizard`
    to end of file: .wizard-*, .wish-*, .matrix-*, .ob-tracker, .wb-*) carries no
    raw font-size, and no hardcoded pill/px radius."""
    css = _read("css/app.css")
    block = css[css.index(".wizard {") :]  # the wizard region is the file tail
    # Every font-size must be a token reference, never a bare rem/px number.
    assert re.search(r"font-size:\s*var\(--t-", block), "sanity: block has font-sizes"
    assert not re.search(
        r"font-size:\s*[0-9]", block
    ), "wizard font-sizes must use var(--t-size-*), not raw rem/px literals"
    # Radii: no literal pill (999px) or px corner — the radius scale must reach here.
    assert not re.search(
        r"border-radius:\s*(?:999px|[0-9]+px)", block
    ), "wizard radii must use var(--t-radius-*), not 999px/Npx literals"


def test_today_example_card_css_is_theme_token_based():
    css = _read("css/app.css")
    # New rules exist and live in the Today region, not the wizard block.
    assert ".today-example {" in css
    assert ".today-example-cap {" in css
    # No raw colour literals — every colour is an existing theme-aware token, so
    # light + dark come for free (the convention: colour changes land in tokens).
    block = css.split(".today-example {")[1].split(".suggestion-card {")[0]
    assert "#" not in block, "use theme tokens, not colour literals"
    assert "var(--t-bg-card)" in block
