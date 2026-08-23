"""The Settings API-key surface is wired to the endpoints (Phase 3, C3).

No JS runtime in this repo — UI behavior is pinned by asserting on the shipped
source and the API contract it consumes (the same approach as
test_frontend_offline / test_static_serving). This guards the wiring, not the
rendering: that the client calls the four api-key routes and the key input is a
password field the value of which is never logged.
"""

from jshq import paths

FRONTEND = paths.FRONTEND_DIR


def _read(rel):
    return (FRONTEND / rel).read_text(encoding="utf-8")


def test_api_js_defines_the_four_key_methods():
    api = _read("js/api.js")
    assert 'request("GET", "/api/settings/api-key")' in api
    assert 'request("PUT", "/api/settings/api-key", { key })' in api
    assert 'request("DELETE", "/api/settings/api-key")' in api
    assert 'request("POST", "/api/settings/api-key/test")' in api


def test_settings_view_wires_the_key_actions():
    js = _read("js/views/settings.js")
    # The System tab loads status and dispatches the three user actions.
    assert "api.getApiKeyStatus()" in js
    for action in ("save-api-key", "test-api-key", "remove-api-key"):
        assert f'action === "{action}"' in js, f"missing handler for {action}"
        assert f'data-action="{action}"' in js, f"missing button for {action}"


def test_key_input_is_a_password_field():
    js = _read("js/views/settings.js")
    # The raw key must never render as visible text or be echoed from status.
    assert 'type="password"' in js
    assert "data-api-key-input" in js


def test_key_section_names_where_the_key_lives():
    """The copy must tell the user the key stays on their machine and goes only
    to Anthropic — the zero-phone-home promise, made visible."""
    js = _read("js/views/settings.js")
    assert "api.anthropic.com" in js
    assert "data directory" in js


def test_linkedin_title_defaults_are_editable_in_sourcing():
    """The central default list behind every new company's LinkedIn role checks
    (seed-on-create, main.py) was API-only — the Settings UI is the fix for
    'there is no central place to edit the defaults'."""
    js = _read("js/views/settings.js")
    assert "LinkedIn role checks" in js
    assert 'tagsHtml("set", "linkedin_title_defaults"' in js
    # Hydrated everywhere the sibling list settings are, so the section never
    # paints stale after a failed write's reload.
    assert js.count('api.getSetting("linkedin_title_defaults")') >= 2


def test_linkedin_suggest_button_is_key_gated_and_review_first():
    """Suggest-with-AI renders ONLY when a key is configured (owner call), and
    suggestions are accept/ignore review cards — nothing joins the list
    without an explicit Add."""
    api = _read("js/api.js")
    assert 'request("POST", "/api/settings/linkedin-titles/suggest")' in api
    js = _read("js/views/settings.js")
    gate = js.split("function linkedinSuggestHtml()")[1][:120]
    assert 'if (!state.apiKey?.configured) return ""' in gate
    for action in ("linkedin-suggest", "linkedin-suggest-add", "linkedin-suggest-ignore"):
        assert f'action === "{action}"' in js, f"missing handler for {action}"
        assert f'data-action="{action}"' in js, f"missing control for {action}"


def test_persona_editor_is_wired():
    api = _read("js/api.js")
    assert 'request("GET", "/api/scoring/persona")' in api
    assert 'request("PUT", "/api/scoring/persona", body)' in api
    js = _read("js/views/settings.js")
    assert "api.getPersona()" in js
    assert 'action === "save-persona"' in js
    assert 'data-action="save-persona"' in js
    assert "data-persona-name" in js and "data-persona-label" in js
    # The two fields sit side by side via the settings-native control-row/field
    # pattern — NOT the map-editor's .map-row (its fixed key/value columns left
    # the labels and inputs misaligned and overlapping). Guard the regression.
    persona = js.split('data-persona-name')[0].rsplit("Persona", 1)[1]
    assert "control-row" in persona and "map-row" not in persona


def test_voice_guide_editor_is_wired():
    api = _read("js/api.js")
    assert 'request("GET", "/api/docs/voice-guide")' in api
    assert 'request("PUT", "/api/docs/voice-guide", { markdown })' in api
    js = _read("js/views/settings.js")
    assert "api.getVoiceGuide()" in js
    assert 'action === "save-voice-guide"' in js
    assert 'data-action="save-voice-guide"' in js
    assert "data-voice-guide" in js


def test_model_spend_labels_cover_sonnet_5():
    """The compose/tailor tier is claude-sonnet-5; without a label the spend line
    printed the raw model id."""
    js = _read("js/views/settings.js")
    assert '"claude-sonnet-5":' in js


def test_vocab_carries_in_band_disciplines():
    """The backend serves in_band_disciplines; merge() used to drop it. The whole
    served taxonomy must survive the fold."""
    js = _read("js/lib/vocab.js")
    assert "in_band_disciplines: list(p.in_band_disciplines" in js
    assert "export function inBandDisciplines()" in js


def test_notifications_toggle_is_mac_gated_and_copy_is_neutral():
    """The popups backend is osascript-only, so off-macOS the toggle would be
    a dead control: settings gates it on isMac (the app is localhost-only, so
    the browser's platform IS the server's) and no shipped string names the
    user's computer 'the Mac'."""
    js = _read("js/views/settings.js")
    assert "${isMac ?" in js and "toggle-notify-popups" in js
    assert "macOS notifications" not in js
    ui = _read("js/lib/ui.js")
    assert "export const isMac" in ui
    assert "the Mac was" not in _read("js/views/today.js")
    assert "Calendar.app" not in _read("js/views/calendar.js")
    # the clipboard fallback names the right key per platform
    assert 'isMac ? "⌘C" : "Ctrl+C"' in _read("js/lib/composeModal.js")


def test_settings_criteria_save_echoes_the_rule_owned_allowlist():
    """location_allowlist is compiled from inclusion rules; the Scoring tab's
    criteria save must echo the current compiled value so a criteria write
    can't clobber it with a stale array. The wizard mirrors this guard
    (test_onboarding_frontend pins that side)."""
    js = _read("js/views/settings.js")
    assert "tier1_params.location_allowlist = (state.compiled.location_allowlist" in js


def test_synthesis_flow_is_wired():
    """Scoring tab's synthesis section: keyed draft, keyless copy+paste-back,
    preview with explicit Apply/Discard — the criteria doc is never written
    without the Apply click."""
    api = _read("js/api.js")
    for method in (
        'request("GET", "/api/scoring/synthesis")',
        'request("GET", "/api/scoring/synthesis/prompt")',
        'request("POST", "/api/scoring/synthesis")',
        'request("POST", "/api/scoring/synthesis/reply", { reply })',
        'request("POST", "/api/scoring/synthesis/apply", body)',
        'request("DELETE", "/api/scoring/synthesis")',
    ):
        assert method in api, method
    js = _read("js/views/settings.js")
    for marker in (
        'data-action="synthesis-draft"',
        'data-action="synthesis-copy-prompt"',
        "data-synthesis-reply",
        'data-action="synthesis-check"',
        'data-action="synthesis-apply"',
        'data-action="synthesis-discard"',
        'data-action="synthesis-toggle-tier2"',
    ):
        assert marker in js, marker
    # every proposal-data interpolation goes through esc()
    assert "esc(d.central_tension.one_liner)" in js
    assert "esc(cell.signal_verbs.join" in js
    assert "esc(cell.activities" in js or "`<li>${esc(a)}</li>`" in js


def test_synthesis_discard_confirms_before_dropping_a_paid_draft():
    """#19 (FLOW-04): the draft can be a paid AI call; discarding it must confirm
    first, symmetric with the compose modal. Guard that synthesisDiscard() gates
    the DELETE behind a confirmModal and bails when the user cancels."""
    js = _read("js/views/settings.js")
    body = js.split("async function synthesisDiscard()")[1].split("\n}")[0]
    assert "confirmModal(" in body, "discard must confirm before dropping the draft"
    # the confirm gates the destructive call — an early return on cancel
    assert "if (!ok) return" in body
    assert body.index("confirmModal(") < body.index("api.discardSynthesis()")


def test_scoring_savebar_is_the_tabs_last_element():
    js = _read("js/views/settings.js")
    # Sticky bottom:0 detaches the moment content follows the bar in the flow,
    # riding up mid-page at the end of the scroll (owner-flagged twice) — the
    # synthesis section must render ABOVE the save bar, never after it.
    assert js.index("${synthesisSection()}") < js.index(
        'class="settings-savebar scoring-savebar'
    )


def test_rescore_is_reachable_from_the_scoring_savebar():
    js = _read("js/views/settings.js")
    # The "rescore to apply" prompt and the Rescore action must not live on
    # separate tabs (it was buried in System): the sticky scoring save bar
    # carries the rescore control right next to Save.
    savebar = js.split('class="settings-savebar scoring-savebar')[1].split("</div>")[0]
    assert "rescoreControlsHtml({ requireSaved: true })" in savebar
    # The shared control the System tab also uses renders a rescore button.
    ctrl = js.split("function rescoreControlsHtml")[1].split("\nfunction ")[0]
    assert 'data-action="rescore"' in ctrl


def test_api_key_decline_checkbox_completes_the_step_and_disables_inputs():
    """Keyless is a first-class mode: a 'I don't want a key' checkbox (shown only
    with no key) completes the api_key setup step. Checking it disables the field
    + Save and shows a warning; the choice persists and refreshes the tracker."""
    js = _read("js/views/settings.js")
    assert 'data-action="toggle-decline-key"' in js
    assert "const inputsOff = shadowed || declined;" in js  # field + Save disabled when declined
    assert 'api.putSetting("api_key_declined", declined)' in js
    assert "refreshOnboardingTracker()" in js  # the setup count changes
    # only offered with no key configured
    assert "const declineRow = k.configured" in js


def test_persona_edits_survive_a_tab_switch():
    """Typed-but-unsaved persona name/label must survive a Settings tab switch,
    the way the sibling voice-guide field does. onInput syncs both to
    state.persona (from which personaSection re-renders), so a structural repaint
    no longer drops the edit."""
    js = _read("js/views/settings.js")
    assert 't.matches("[data-persona-name]")' in js
    assert "state.persona.display_name = t.value" in js
    assert 't.matches("[data-persona-label]")' in js
    assert "state.persona.domain_label = t.value" in js


def test_rescore_poll_never_repaints_over_an_edited_field():
    """A rescore finishing (or a status poll erroring) mid-edit must not wipe an
    unsaved persona name or a pasted API key. The mid-edit guard was on the
    progress tick only; the completion and error branches called paint()
    unconditionally. All three poll paint sites now route through the one guard."""
    js = _read("js/views/settings.js")
    poll = js.split("rescorePoll = setInterval")[1].split("}, 4000);")[0]
    # no raw paint() survives in the poll body -- every path defers to the guard
    assert "paint();" not in poll
    assert poll.count("settleRescorePaint();") == 3  # catch, completion, progress
    # the guard skips a full paint while any control in the panel is focused
    guard = js.split("function settleRescorePaint()")[1].split("\nfunction ")[0]
    assert 'active.matches?.("input, textarea, select")' in guard
    assert "if (editing) updateRescoreProgress();" in guard
    assert "else paint();" in guard


def test_api_js_defines_the_schedule_methods():
    api = _read("js/api.js")
    assert 'request("GET", "/api/schedule")' in api
    assert 'request("PUT", "/api/schedule", { refresh, backup })' in api
    assert 'request("POST", "/api/schedule/install")' in api
    assert 'request("POST", "/api/schedule/uninstall")' in api


def test_settings_view_wires_the_schedule_actions():
    js = _read("js/views/settings.js")
    assert "api.getSchedule()" in js  # joins the mount Promise.all
    for action in ("install-schedule", "remove-schedule"):
        assert f'action === "{action}"' in js, f"missing handler for {action}"
        assert f'data-action="{action}"' in js, f"missing button for {action}"
    # Install is one verb: save the times, then make the OS match — a
    # "saved but not applied" drift state must not exist.
    body = js.split("async function installSchedule()")[1].split("\nasync function ")[0]
    assert "api.putSchedule(refresh, backup)" in body
    assert "api.installSchedule()" in body
    # Remove is destructive — gated on the confirm modal like the endpoint's.
    remove = js.split("async function removeSchedule()")[1].split("\nasync function ")[0]
    assert "confirmModal({" in remove
    assert "api.uninstallSchedule()" in remove


def test_schedule_section_renders_and_degrades():
    js = _read("js/views/settings.js")
    section = js.split("function scheduleSection()")[1].split("\nfunction ")[0]
    # rendered on the System tab
    assert "${scheduleSection()}" in js
    # times render as house time-picker slots (one per time, add/remove), not
    # a comma-separated text field — the same control the reminder modal uses
    assert "timeFieldHtml(" in section
    for action in ("schedule-slot-add", "schedule-slot-remove"):
        assert f'data-action="{action}"' in js, f"missing control for {action}"
        assert f'action === "{action}"' in js, f"missing handler for {action}"
    # Apply reads the pickers' canonical hidden inputs; empties drop, an
    # all-empty job blocks with a toast
    body = js.split("async function installSchedule()")[1].split("\nasync function ")[0]
    assert "readScheduleSlots(" in body and "toast(" in body
    # slot edits survive the add/remove repaint via view state
    assert "function syncScheduleSlots()" in js and "scheduleSlots" in js
    # unsupported platform: no dead buttons, an honest manual pointer instead
    assert "isn't supported on this system" in section
    unsupported = section.split("if (!s.supported)")[1].split("}")[0]
    assert "data-action" not in unsupported


def test_unified_ai_section_composes_the_three_surfaces():
    """One AI section (owner direction 2026-08-22): the provider picker, the
    picked provider's credential pane, and the per-task split as an Advanced
    disclosure — no more three sibling sections."""
    js = _read("js/views/settings.js")
    assert "${aiSection()}" in js
    for gone in ("${apiKeySection()}", "${aiProvidersSection()}", "${aiModelsSection()}"):
        assert gone not in js, f"stale standalone section render: {gone}"
    section = js.split("function aiSection()")[1].split("\nfunction ")[0]
    assert "apiKeyPane()" in section and "endpointPane()" in section
    assert 'data-action="pick-provider"' in section
    assert "<details" in section and "axisControls(" in section
    # The derived state is never stored — it reads the axes every paint.
    derived = js.split("function derivedProvider()")[1].split("\nfunction ")[0]
    assert "state.aiModels" in derived and "per_task" in derived


def test_provider_picker_switches_tasks_and_keeps_credentials():
    js = _read("js/views/settings.js")
    assert 'action === "pick-provider"' in js
    body = js.split("async function pickProvider(")[1].split("\nasync function ")[0]
    # Ready → both axes PUT with the remembered model for that provider.
    assert "remembered" in body and "putAxes(" in body
    # Not ready → intent + reveal, never a credential write from the picker.
    assert "pickerIntent" in body
    for forbidden in ("putApiKey", "putAiProviders", "deleteApiKey", "deleteAiProviders"):
        assert forbidden not in body, f"picker must not touch credentials: {forbidden}"
    # The panes' Saves complete a pending switch.
    save_key = js.split("async function saveApiKey(")[1].split("\nasync function ")[0]
    assert 'pickProvider("anthropic")' in save_key
    save_ep = js.split("async function saveAiProviders()")[1].split("\nasync function ")[0]
    assert "data-compat-active-model" in save_ep and "putAxes(" in save_ep


def test_simple_compat_model_field_commits_on_change():
    js = _read("js/views/settings.js")
    assert "data-compat-active-model" in js
    # Commit on blur/Enter like the per-axis field — never per keystroke.
    assert 't.matches("[data-compat-active-model]")' in js
    body = js.split("async function saveActiveCompatModel(")[1].split("\nasync function ")[0]
    assert '"openai_compat"' in body and "putAxes(" in body


def test_declining_ai_greys_out_the_endpoint_path_too():
    """"I don't want to use AI" (renamed from "…an API key", 2026-08-22)
    declines the whole feature: offered only when NEITHER provider is set up,
    and while checked the picker's endpoint segment disables alongside the
    key form."""
    js = _read("js/views/settings.js")
    assert "I don't want to use AI" in js
    assert "I don't want to use an API key" not in js
    # Offered only with no key AND no endpoint.
    assert "k.configured || state.aiProviders?.configured" in js
    # The endpoint segment carries the declined disable...
    assert 'segBtn("openai_compat", "Your endpoint", declinedAll)' in js
    # ...and the handler backstops it.
    picker = js.split("async function pickProvider(")[1].split("\nasync function ")[0]
    assert "state.apiKeyDeclined === true" in picker


def test_credential_inputs_are_field_sized_not_tag_chips():
    """The key/URL inputs reuse the savebar's .settings-add-input, whose 130px
    tag-adder sizing truncated long secrets and URLs into guesswork (owner
    report 2026-08-22). The cred modifier upgrades them to field scale."""
    js = _read("js/views/settings.js")
    for attr in ("data-api-key-input", "data-compat-url-input", "data-compat-key-input"):
        assert f'settings-add-input settings-cred-input" {attr}' in js, attr
    css = _read("css/app.css")
    block = css.split(".settings-savebar .settings-cred-input {")[1].split("}")[0]
    assert "min-width" in block and "flex" in block
