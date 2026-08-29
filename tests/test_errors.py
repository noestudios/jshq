"""The [JSHQ-###] error-code scheme (error-audit Waves 1-2).

Backend half: the registry's invariants, the code-suffixed wire form, and the
RequestValidationError handler that replaces FastAPI's Pydantic-array 422s
with one human sentence per field (string detail — the api.js contract).

Frontend half: source-scan pins (test_settings_frontend style — no JS
runtime) that the shared humanizer exists and the applications-view file
toasts render failures AS failures through it.
"""

import re

from jshq import errors, paths

FRONTEND = paths.FRONTEND_DIR


def _read(rel):
    return (FRONTEND / rel).read_text(encoding="utf-8")


# --- registry ----------------------------------------------------------------


def test_registry_entries_are_wellformed():
    assert errors.REGISTRY, "registry must not be empty"
    for code, entry in errors.REGISTRY.items():
        assert entry.code == code
        assert 0 < code < 1000  # three digits, zero-padded on the wire
        assert entry.message.strip() and not entry.message.rstrip().endswith("]"), (
            f"JSHQ-{code}: default message must not embed its own code"
        )
        assert entry.note.strip(), f"JSHQ-{code}: note is the manual copy — required"


def test_registry_notes_are_em_dash_free():
    # Notes become user-manual appendix copy, which is under the docs lint
    # (test_docs_no_ai_tells) — keep them clean at the source.
    for code, entry in errors.REGISTRY.items():
        assert "—" not in entry.note, f"JSHQ-{code}: em dash in note"


def test_fmt_appends_the_padded_code():
    assert errors.fmt(errors.VALIDATION).endswith("[JSHQ-001]")
    assert errors.fmt(errors.COMPOSE_FAILED).endswith("[JSHQ-501]")
    # An overriding message keeps the entry's code.
    assert errors.fmt(errors.VALIDATION, "Name is required.") == "Name is required. [JSHQ-001]"


def test_http_error_carries_status_and_code():
    exc = errors.http_error(502, errors.SYNTHESIS_FAILED)
    assert exc.status_code == 502
    assert "[JSHQ-401]" in exc.detail


# --- provider-status descriptions (key/endpoint test surfaces) ----------------


def test_out_of_credit_400_reads_as_billing_not_broken_key():
    """The reported trap: a valid key on an empty balance answers 400. The
    message must say 'out of credits', not parrot the status code."""
    msg = errors.describe_provider_status(
        400,
        "Your credit balance is too low to access the Anthropic API.",
        subject="api.anthropic.com",
        billing="Add credit at console.anthropic.com (Plans & Billing)",
    )
    assert "out of credits" in msg
    assert "console.anthropic.com" in msg
    assert "400" not in msg  # the code alone would mislead


def test_out_of_credit_without_billing_surfaces_provider_words():
    msg = errors.describe_provider_status(
        400, "Your credit balance is too low.", subject="The endpoint"
    )
    assert "credit balance is too low" in msg


def test_rate_limit_and_server_errors_are_plain_language():
    assert "rate-limiting" in errors.describe_provider_status(429, subject="api.anthropic.com")
    assert "their end" in errors.describe_provider_status(503, subject="api.anthropic.com")


def test_unknown_status_surfaces_provider_message_then_falls_back():
    with_msg = errors.describe_provider_status(
        400, "model: bad-id not found", subject="The endpoint"
    )
    assert "bad-id not found" in with_msg and "(400)" in with_msg
    bare = errors.describe_provider_status(418, subject="api.anthropic.com")
    assert bare == "api.anthropic.com returned status 418."


# --- the validation handler ---------------------------------------------------


def test_missing_field_is_a_labeled_sentence(client):
    r = client.post("/api/contacts", json={})
    assert r.status_code == 422
    body = r.json()
    assert isinstance(body["detail"], str)  # never the Pydantic array
    assert body["detail"] == "Name is required. [JSHQ-001]"
    # The machine-shaped view still rides alongside for API callers.
    assert body["errors"][0]["loc"] == ["body", "name"]
    assert body["errors"][0]["type"] == "missing"


def test_pattern_mismatch_names_the_field_with_a_hint(client):
    # The reminder time regex used to surface AS a regex in the toast.
    r = client.post(
        "/api/reminders", json={"title": "x", "due_date": "2026-06-20", "due_time": "9am"}
    )
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "Due time" in detail
    assert "09:30" in detail  # the concrete example, not the pattern
    assert "^" not in detail and "\\d" not in detail  # no regex leakage
    assert detail.endswith("[JSHQ-001]")


def test_model_validator_prose_passes_through(client, criteria_doc):
    # Our own @model_validator messages are authored prose — the handler must
    # not rewrite them. The location-exclude rule carries its own code
    # (audit P2: the frontend matches [JSHQ-202], never the wording), and the
    # handler must not stack [JSHQ-001] on an already-coded sentence.
    r = client.put(
        "/api/inclusion-rules",
        json={
            "rules": [{"id": "x", "verb": "exclude", "target": "location", "terms": ["boston"]}],
            "manual": {},
        },
    )
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "[JSHQ-202]" in detail
    assert "location" in detail  # still names the concept for API callers
    assert "Value error" not in detail  # the Pydantic prefix is stripped
    assert "[JSHQ-001]" not in detail  # no stacked generic code


def test_type_errors_read_as_plain_language(client):
    r = client.post(
        "/api/reminders",
        json={"title": "x", "due_date": "2026-06-20", "entity_type": "job", "entity_id": "abc"},
    )
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail == "Entity id must be a whole number. [JSHQ-001]"
    assert "body." not in detail  # no Pydantic paths at the user


def test_literal_choice_error_is_labeled(client, seed_job):
    job_id = seed_job()
    r = client.patch(f"/api/jobs/{job_id}", json={"status": "nonsense"})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail == "Status isn't one of the allowed options. [JSHQ-001]"


# --- the seven AI 502s carry codes (spot checks live in test_compose /
# --- test_synthesis_api; here we pin that every AI-failure entry exists) ------


def test_ai_failure_codes_are_registered():
    for entry, code in [
        (errors.COMPOSE_FAILED, 501),
        (errors.REFINE_FAILED, 502),
        (errors.TAILOR_FAILED, 503),
        (errors.TAILOR_CHAT_FAILED, 504),
        (errors.RULE_PROPOSAL_FAILED, 301),
        (errors.SYNTHESIS_FAILED, 401),
        (errors.TITLE_SUGGEST_FAILED, 201),
    ]:
        assert entry.code == code
        assert "Try again" in entry.message  # actionable, not just a diagnosis


def test_no_502_site_leaks_the_raw_exception():
    # The seven AI catch-alls must log the exception and raise a coded detail;
    # a reintroduced f"...: {exc}" detail is the regression this pins against.
    main_src = (paths.PACKAGE_DIR / "main.py").read_text(encoding="utf-8")
    leaks = re.findall(r'status_code=502, detail=f"[^"]*\{exc\}', main_src)
    assert leaks == [
        # The two PDF-render 502s are F3/Wave-2 scope (ResumeError corpus) —
        # they are the only sanctioned survivors. Shrink, never grow.
        'status_code=502, detail=f"PDF render failed: {exc}',
        'status_code=502, detail=f"PDF render failed: {exc}',
    ]


# --- frontend: the shared humanizer and its first adopters --------------------


def test_lib_errors_exports_the_humanizer():
    js = _read("js/lib/errors.js")
    assert "export function humanizeApiError(" in js
    assert "export function errorCode(" in js
    assert "JSHQ-" in js  # the code extractor matches the wire format


def test_applications_file_toasts_render_failures_as_failures():
    js = _read("js/views/applications.js")
    assert 'from "../lib/errors.js"' in js
    # Upload + delete failures: humanized text, error styling (they used to
    # render success-styled with raw error.message).
    assert js.count("humanizeApiError(error") >= 2
    assert 'toast(humanizeApiError(error, "Couldn\'t add the file."), { error: true })' in js
    assert 'toast(humanizeApiError(error, "Couldn\'t remove the file."), { error: true })' in js


def test_api_js_shares_error_parsing_between_fetch_paths():
    js = _read("js/api.js")
    # One implementation for both request() and the raw-body upload — the
    # upload used to drop structured details and had no timeout.
    assert js.count("throw await responseError(response)") == 2
    assert js.count("return parseBody(response)") == 2
    upload = js[js.index("uploadApplicationFile") :]
    assert "AbortError" in upload  # the upload path gained the timeout


# --- Wave 2: coded rewrites + the structured criteria/409 details -------------


def test_persona_422_carries_code(client, criteria_doc):
    r = client.put(
        "/api/scoring/persona", json={"display_name": "x", "domain_label": "y" * 300}
    )
    assert r.status_code == 422
    assert "[JSHQ-303]" in r.json()["detail"]


def test_geocode_404_is_the_try_town_st_sentence(client):
    r = client.get("/api/scoring/geocode", params={"q": "Nowheresville, ZZ"})
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert 'Town, ST' in detail  # the actionable format hint, not a !r repr
    assert "'" + "Nowheresville" not in detail  # no Python repr quoting
    assert "[JSHQ-305]" in detail


def test_stale_suggestion_404_is_coded(client):
    r = client.post("/api/suggestions/reminder", json={"key": "ghost", "action": "accept"})
    assert r.status_code == 404
    assert "[JSHQ-306]" in r.json()["detail"]


def test_stale_proposal_404_is_coded(client):
    r = client.post("/api/suggestions/scoring-rule", json={"id": "ghost", "action": "accept"})
    assert r.status_code == 404
    assert "[JSHQ-307]" in r.json()["detail"]


def test_upload_filename_rule_is_coded(client, seed_application):
    app_id = seed_application()
    r = client.put(f"/api/applications/{app_id}/files/CON.pdf", content=b"x")
    assert r.status_code == 400
    assert "[JSHQ-203]" in r.json()["detail"]


def test_key_test_503_uses_the_actionable_missing_message(client):
    from jshq import apikey

    r = client.post("/api/settings/api-key/test")
    assert r.status_code == 503
    assert r.json()["detail"] == apikey.MISSING_MESSAGE  # not "no API key configured"


def test_settings_parsers_read_structure_not_prose():
    js = _read("js/views/settings.js")
    # P1: the criteria mapper anchors on the 422's structured field/kind.
    assert "parseCriteriaError(error)" in js
    assert "info?.field" in js or "info.field" in js
    # The fragile includes/regex scanning over server prose is gone.
    assert "CRIT_FIELDS" not in js
    assert "missing required key" not in js
    # P2: the rules mapper no longer regexes the validator sentence.
    assert "location rule cannot be" not in js


def test_lib_errors_exports_errorCodes():
    js = _read("js/lib/errors.js")
    assert "export function errorCodes(" in js


# --- Wave 3: the user-manual appendix stays generated, never hand-edited ------


def test_manual_error_appendix_matches_the_registry():
    manual = (paths.DEFAULTS_DIR / "user-manual.md").read_text(encoding="utf-8")
    assert errors.APPENDIX_START in manual, "run scripts/gen_error_appendix.py"
    section = manual.split(errors.APPENDIX_START, 1)[1].split(errors.APPENDIX_END, 1)[0]
    assert section.strip() == errors.manual_appendix().strip(), (
        "the shipped appendix drifted from the registry — run "
        "scripts/gen_error_appendix.py and commit both"
    )


# --- Wave 3: silent failures now have surfaces (source-scan pins) -------------


def test_pollers_tolerate_blips_and_surface_giving_up():
    # Each poller used to die on its first failed tick: today.js froze the
    # green bar with state.running stuck true, settings.js cleared the rescore
    # bar with no message, jobs.js just never delivered its completion toast.
    for rel, needle in [
        ("js/views/today.js", "Lost contact with the refresh"),
        ("js/views/jobs.js", "Lost contact with the refresh"),
        ("js/views/settings.js", "Lost contact while rescoring"),
    ]:
        js = _read(rel)
        assert "misses" in js and needle in js, rel
    # app.js's load-time background watcher tolerates blips too (its give-up
    # stays quiet by design — the user never asked for that refresh).
    assert "misses" in _read("js/app.js")


def test_application_sections_offer_retry_instead_of_eternal_loading():
    js = _read("js/views/applications.js")
    assert "loadErrorHtml(" in js
    for action in ("retry-files", "retry-tailoring", "retry-chat"):
        assert f'loadErrorHtml("{action}")' in js, action
        assert f'case "{action}"' in js, action
    # The swallowed-catch idiom must not return to this file (the autosave
    # data-loss path and the three eternal-Loading sections all wore it).
    assert ".catch(() => {})" not in js


def test_cover_letter_autosave_failure_is_surfaced_once():
    js = _read("js/views/applications.js")
    assert "tailorAutosaveFailed" in js  # one toast per failure streak
    assert "Couldn't save the cover letter" in js


def test_tracker_dismiss_failure_reverts_and_says_so():
    js = _read("js/lib/onboardingTracker.js")
    assert ".catch(() => {})" not in js
    assert "tracker_dismissed: false" in js  # the revert on a failed persist


def test_wizard_title_seed_failures_toast():
    js = _read("js/views/welcome.js")
    assert "titleSeedFailed" in js
    assert "Settings → Sourcing" in js  # points at where to fix it


def test_careers_probe_error_is_not_no_board_found():
    js = _read("js/views/welcome.js")
    assert "{ error: true }" in js  # a failed lookup is its own state...
    assert "Couldn't check for a job board" in js  # ...with its own line
    assert "!s.careersSearch.error" in js  # and doesn't block the retry


def test_dismiss_reasons_fallback_is_not_cached():
    js = _read("js/views/jobs.js")
    assert "Couldn't load your dismissal reasons" in js


def test_backup_banner_translates_the_persisted_detail():
    js = _read("js/views/today.js")
    assert "backupDetailText" in js
    assert "integrity check" in js
