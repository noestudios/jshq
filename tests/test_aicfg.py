"""Per-task AI model selection (Providers Tiers 1-2): aicfg resolution
(including provider bindings), the /api/settings/ai-models routes, per-model
request-shape helpers, the unpriced/local spend guards, and the Settings UI
wiring (source-scan, settings-frontend style)."""

import json
from types import SimpleNamespace

import httpx

from jshq import aicfg, oaicompat, paths, providers, usage
from jshq.main import app, get_compose_client

FRONTEND = paths.FRONTEND_DIR


def _read(rel):
    return (FRONTEND / rel).read_text(encoding="utf-8")


def _set_overrides(db, **overrides):
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (aicfg.SETTING_KEY, json.dumps(overrides)),
    )
    db.commit()


def _set_base_url(db, url="http://localhost:11434/v1"):
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (providers.SETTING_KEY, json.dumps({"openai_compat": {"base_url": url}})),
    )
    db.commit()


# --- resolution ---------------------------------------------------------------


def test_every_task_has_a_default_and_an_axis():
    assert set(aicfg.DEFAULTS) == set(aicfg.TASK_AXIS)
    assert set(aicfg.TASK_AXIS.values()) == set(aicfg.AXES)


def test_unset_resolves_to_the_shipped_default_per_task(db):
    for task, default in aicfg.DEFAULTS.items():
        assert aicfg.model_for(db, task) == default


def test_axis_override_covers_its_tasks_and_only_its_tasks(db):
    _set_overrides(db, analysis="claude-sonnet-4-6")
    for task in aicfg.AXES["analysis"]:
        assert aicfg.model_for(db, task) == "claude-sonnet-4-6"
    for task in aicfg.AXES["writing"]:
        assert aicfg.model_for(db, task) == aicfg.DEFAULTS[task]


def test_garbled_or_blank_row_reads_as_unset(db):
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)",
        (aicfg.SETTING_KEY, "not json"),
    )
    db.commit()
    assert aicfg.read_overrides(db) == {"analysis": None, "writing": None}
    _set_overrides(db, analysis="", writing=None)
    assert aicfg.model_for(db, "scoring") == aicfg.DEFAULTS["scoring"]


# --- provider bindings (Tier 2) -------------------------------------------------


def test_legacy_string_row_reads_as_anthropic_binding(db):
    # A Tier-1 row (bare id string) keeps working forever, no migration.
    _set_overrides(db, analysis="claude-sonnet-4-6")
    b = aicfg.binding_for(db, "scoring")
    assert b == aicfg.Binding("anthropic", "claude-sonnet-4-6", "claude-sonnet-4-6", False)


def test_unset_binding_is_the_anthropic_default(db):
    b = aicfg.binding_for(db, "compose")
    assert b.provider == "anthropic"
    assert b.model == b.ledger_key == aicfg.DEFAULTS["compose"]
    assert b.local is False


def test_anthropic_object_with_null_model_reads_as_unset(db):
    _set_overrides(db, analysis={"provider": "anthropic", "model": None})
    assert aicfg.read_overrides(db)["analysis"] is None


def test_compat_binding_carries_namespaced_ledger_key_and_locality(db):
    _set_overrides(db, analysis={"provider": "openai_compat", "model": "llama3.3"})
    _set_base_url(db, "http://localhost:11434/v1")
    b = aicfg.binding_for(db, "scoring")
    assert b.provider == "openai_compat"
    assert b.model == "llama3.3"
    # Namespaced: a local model sharing an Anthropic id can't pollute its
    # spend row or pick up its price.
    assert b.ledger_key == "openai-compat:llama3.3"
    assert b.local is True

    _set_base_url(db, "https://api.openai.com/v1")
    assert aicfg.binding_for(db, "scoring").local is False


def test_compat_object_without_model_reads_as_unset(db):
    _set_overrides(db, analysis={"provider": "openai_compat", "model": "  "})
    assert aicfg.read_overrides(db)["analysis"] is None
    assert aicfg.model_for(db, "scoring") == aicfg.DEFAULTS["scoring"]


# --- per-model request shape ----------------------------------------------------


def test_thinking_disabled_only_where_thinking_defaults_on():
    # Sonnet 5 / Opus 5 think by default and accept "disabled"; Haiku 4.5 and
    # Sonnet 4.6 run thinking-free when the param is omitted — and omission is
    # the safe default for unknown ids (Fable 5 rejects "disabled" with a 400).
    assert aicfg.thinking_kwargs("claude-sonnet-5") == {"thinking": {"type": "disabled"}}
    assert aicfg.thinking_kwargs("claude-opus-5") == {"thinking": {"type": "disabled"}}
    assert aicfg.thinking_kwargs("claude-haiku-4-5") == {}
    assert aicfg.thinking_kwargs("claude-sonnet-4-6") == {}
    assert aicfg.thinking_kwargs("claude-future-99") == {}


def test_temperature_dropped_on_the_no_sampling_tier():
    assert aicfg.temperature_kwargs("claude-haiku-4-5", 0.0) == {"temperature": 0.0}
    assert aicfg.temperature_kwargs("claude-sonnet-4-6", 0.4) == {"temperature": 0.4}
    assert aicfg.temperature_kwargs("claude-sonnet-5", 0.0) == {}
    assert aicfg.temperature_kwargs("claude-opus-5", 0.0) == {}


def test_every_curated_model_is_priced():
    # The curated list is the safety boundary that keeps the spend ledger
    # honest: a selectable model without a PRICES entry would bill $0.00.
    for m in aicfg.MODELS:
        assert usage.rate_for(m["id"]) is not None, m["id"]


# --- the unpriced spend guard ---------------------------------------------------


def test_unknown_model_spend_is_marked_unpriced(db):
    u = SimpleNamespace(
        input_tokens=1000, output_tokens=100,
        cache_read_input_tokens=0, cache_creation_input_tokens=0,
    )
    usage.record_usage(db, "claude-mystery-model", u)
    usage.record_usage(db, "claude-haiku-4-5", u)
    db.commit()
    by = usage.read_usage_totals(db)["by_model"]
    assert by["claude-mystery-model"]["unpriced"] is True
    assert by["claude-mystery-model"]["cost"] == 0.0
    assert "unpriced" not in by["claude-haiku-4-5"]


def test_loopback_endpoint_spend_is_marked_local_not_unpriced(db):
    # A loopback endpoint's $0.00 is TRUE — label it local so the spend total
    # stays exact instead of hedging with the unpriced "$X+".
    u = SimpleNamespace(
        input_tokens=1000, output_tokens=100,
        cache_read_input_tokens=0, cache_creation_input_tokens=0,
    )
    usage.record_usage(db, "openai-compat:llama3.3", u, local=True)
    db.commit()
    by = usage.read_usage_totals(db)["by_model"]
    assert by["openai-compat:llama3.3"]["local"] is True
    assert "unpriced" not in by["openai-compat:llama3.3"]
    assert by["openai-compat:llama3.3"]["cost"] == 0.0


# --- the API -------------------------------------------------------------------


def test_get_returns_defaults_and_the_curated_list(client):
    r = client.get("/api/settings/ai-models")
    assert r.status_code == 200
    body = r.json()
    assert body["analysis"] is None and body["writing"] is None
    assert [m["id"] for m in body["models"]] == [m["id"] for m in aicfg.MODELS]
    assert body["defaults"]["analysis"]["scoring"] == aicfg.DEFAULTS["scoring"]
    assert body["defaults"]["writing"]["compose"] == aicfg.DEFAULTS["compose"]
    assert body["calibrated_scoring_model"] == aicfg.CALIBRATED_SCORING_MODEL


def test_put_round_trips_and_null_resets(client):
    # The Tier-1 bare-string shorthand still writes; axes read back as
    # explicit {provider, model} objects since Tier 2.
    r = client.put(
        "/api/settings/ai-models",
        json={"analysis": "claude-sonnet-4-6", "writing": "claude-opus-5"},
    )
    assert r.status_code == 200
    assert r.json()["analysis"] == {"provider": "anthropic", "model": "claude-sonnet-4-6"}
    assert client.get("/api/settings/ai-models").json()["writing"] == {
        "provider": "anthropic", "model": "claude-opus-5",
    }
    r = client.put("/api/settings/ai-models", json={"analysis": None, "writing": None})
    assert r.status_code == 200
    body = client.get("/api/settings/ai-models").json()
    assert body["analysis"] is None and body["writing"] is None


def test_put_accepts_axis_objects(client, db):
    _set_base_url(db)
    r = client.put(
        "/api/settings/ai-models",
        json={
            "analysis": {"provider": "openai_compat", "model": "llama3.3"},
            "writing": {"provider": "anthropic", "model": "claude-sonnet-5"},
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["analysis"] == {"provider": "openai_compat", "model": "llama3.3"}
    assert body["writing"] == {"provider": "anthropic", "model": "claude-sonnet-5"}


def test_put_rejects_a_model_off_the_curated_list(client):
    r = client.put("/api/settings/ai-models", json={"analysis": "claude-fable-5"})
    assert r.status_code == 422
    assert "[JSHQ-205]" in r.json()["detail"]
    # Nothing was written: the row stays unset.
    assert client.get("/api/settings/ai-models").json()["analysis"] is None


def test_put_compat_requires_a_model_id(client, db):
    _set_base_url(db)
    for bad in (None, "", "   ", "two words"):
        r = client.put(
            "/api/settings/ai-models",
            json={"analysis": {"provider": "openai_compat", "model": bad}},
        )
        assert r.status_code == 422, bad
        assert "[JSHQ-206]" in r.json()["detail"]
    assert client.get("/api/settings/ai-models").json()["analysis"] is None


def test_put_compat_requires_a_configured_endpoint(client):
    r = client.put(
        "/api/settings/ai-models",
        json={"analysis": {"provider": "openai_compat", "model": "llama3.3"}},
    )
    assert r.status_code == 422
    assert "[JSHQ-207]" in r.json()["detail"]
    assert client.get("/api/settings/ai-models").json()["analysis"] is None


def test_writing_override_reaches_compose_and_the_ledger(client, seed_job):
    state = {}

    async def create(**kwargs):
        state.update(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="Thanks for the time.")],
            usage=SimpleNamespace(
                input_tokens=100, output_tokens=10,
                cache_read_input_tokens=0, cache_creation_input_tokens=0,
            ),
        )

    fake = SimpleNamespace(messages=SimpleNamespace(create=create))
    app.dependency_overrides[get_compose_client] = lambda: fake
    try:
        assert client.put(
            "/api/settings/ai-models", json={"writing": "claude-opus-5"}
        ).status_code == 200
        r = client.post(
            "/api/compose",
            json={"intent": "thank_you", "entity_type": "job", "entity_id": seed_job()},
        )
        assert r.status_code == 200
        assert r.json()["model"] == "claude-opus-5"
        assert state["model"] == "claude-opus-5"
        assert state["thinking"] == {"type": "disabled"}
    finally:
        app.dependency_overrides.pop(get_compose_client, None)


# --- the compat path end to end (Tier 2) ----------------------------------------


def test_compat_override_reaches_compose_and_the_ledger(client, db, seed_job):
    """The writing axis on the user's endpoint: the wire request is
    chat-completions shaped (no cache_control anywhere), and the ledger bills
    the namespaced key with the local label."""
    seen = {}

    def handler(request):
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "Thanks!"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10},
        })

    fake = oaicompat.OpenAICompatClient(
        "http://localhost:11434/v1", transport=httpx.MockTransport(handler)
    )
    app.dependency_overrides[get_compose_client] = lambda: fake
    try:
        _set_base_url(db)
        assert client.put(
            "/api/settings/ai-models",
            json={"writing": {"provider": "openai_compat", "model": "llama3.3"}},
        ).status_code == 200
        r = client.post(
            "/api/compose",
            json={"intent": "thank_you", "entity_type": "job", "entity_id": seed_job()},
        )
        assert r.status_code == 200
        assert r.json()["model"] == "llama3.3"  # provenance keeps the bare wire id
        assert seen["payload"]["model"] == "llama3.3"
        assert "cache_control" not in json.dumps(seen["payload"])
        by = usage.read_usage_totals(db)["by_model"]
        assert by["openai-compat:llama3.3"]["local"] is True
        assert by["openai-compat:llama3.3"]["cost"] == 0.0
    finally:
        app.dependency_overrides.pop(get_compose_client, None)


def test_compat_selected_but_endpoint_gone_degrades_actionably(client, db, seed_job):
    """The drift case: an axis points at the endpoint but its config was
    deleted later. Runtime guards carry it — 503 with the actionable message,
    scoring skips — never a crash."""
    import asyncio

    from jshq import scoring

    _set_overrides(db, writing={"provider": "openai_compat", "model": "llama3.3"},
                   analysis={"provider": "openai_compat", "model": "llama3.3"})
    r = client.post(
        "/api/compose",
        json={"intent": "thank_you", "entity_type": "job", "entity_id": seed_job()},
    )
    assert r.status_code == 503
    assert r.json()["detail"] == providers.MISSING_ENDPOINT_MESSAGE

    report = asyncio.run(scoring.run_scoring(db))
    assert report == {"skipped": providers.MISSING_ENDPOINT_MESSAGE}


# --- Settings UI wiring (source-scan) ------------------------------------------


def test_api_js_defines_the_ai_models_methods():
    api = _read("js/api.js")
    assert 'request("GET", "/api/settings/ai-models")' in api
    assert 'request("PUT", "/api/settings/ai-models", { analysis, writing })' in api


def test_settings_view_wires_the_model_selects():
    js = _read("js/views/settings.js")
    assert "api.getAiModels()" in js
    assert "api.putAiModels(" in js
    assert 'data-ai-model-axis="${axis}"' in js
    assert "saveAiModel(" in js
    # The calibration honesty note keys on the blessed baseline's model.
    assert "calibrated_scoring_model" in js


def test_settings_view_says_unpriced_instead_of_zero_dollars():
    js = _read("js/views/settings.js")
    assert 'm.unpriced ? "unpriced"' in js
    assert "hasUnpriced" in js


def test_api_js_defines_the_ai_providers_methods():
    api = _read("js/api.js")
    assert 'request("GET", "/api/settings/ai-providers")' in api
    assert 'request("PUT", "/api/settings/ai-providers", { base_url: baseUrl, api_key: apiKey })' in api
    assert 'request("DELETE", "/api/settings/ai-providers")' in api
    assert 'request("POST", "/api/settings/ai-providers/test")' in api


def test_settings_view_wires_the_endpoint_section():
    js = _read("js/views/settings.js")
    assert "api.getAiProviders()" in js
    assert "api.putAiProviders(" in js
    assert "api.testAiProviders()" in js
    # The endpoint key input is a password field and the key is never rendered —
    # only the server's masked status (mirrors the api-key section's contract).
    assert 'type="password" class="settings-add-input settings-cred-input" data-compat-key-input' in js
    assert "data-compat-url-input" in js
    # Per-axis provider choice + the free-text compat model id.
    assert 'data-ai-provider-axis="${axis}"' in js
    assert "data-ai-compat-model" in js


def test_settings_view_labels_local_spend_and_endpoint_models():
    js = _read("js/views/settings.js")
    # Loopback spend renders as genuinely-free local, never as unpriced.
    assert '"$0.00 local"' in js
    # Namespaced compat ledger keys display as the bare model + where it ran.
    assert 'id.startsWith("openai-compat:")' in js
    # The rescore modal never fabricates a $ figure for an endpoint.
    assert '"cost unknown — endpoint pricing not tracked"' in js
    assert '"no API cost — local endpoint"' in js


# --- remembered (provider picker switch-back memory) --------------------------


def test_write_overrides_stamps_remembered_per_provider(db):
    aicfg.write_overrides(db, {"analysis": {"provider": "anthropic", "model": "claude-opus-5"}, "writing": None})
    aicfg.write_overrides(db, {"analysis": {"provider": "openai_compat", "model": "llama3.3"}, "writing": None})
    remembered = aicfg.read_remembered(db)
    # Both choices survive: switching provider back restores the prior model.
    assert remembered["analysis"] == {"anthropic": "claude-opus-5", "openai_compat": "llama3.3"}
    assert remembered["writing"] == {}
    # And the axes themselves read what was last written.
    assert aicfg.read_overrides(db)["analysis"] == {"provider": "openai_compat", "model": "llama3.3"}


def test_write_overrides_none_keeps_the_memory(db):
    """Default is not a model choice: resetting an axis must not erase the
    switch-back memory for either provider."""
    aicfg.write_overrides(db, {"analysis": {"provider": "openai_compat", "model": "llama3.3"}, "writing": None})
    aicfg.write_overrides(db, {"analysis": None, "writing": None})
    assert aicfg.read_overrides(db)["analysis"] is None
    assert aicfg.read_remembered(db)["analysis"] == {"openai_compat": "llama3.3"}


def test_read_remembered_tolerates_garbage_and_stale_ids(db):
    assert aicfg.read_remembered(db) == {"analysis": {}, "writing": {}}
    _set_overrides(db, remembered="not a dict")
    assert aicfg.read_remembered(db) == {"analysis": {}, "writing": {}}
    _set_overrides(
        db,
        remembered={
            "analysis": {"anthropic": "retired-model-id", "openai_compat": "  qwen3  ", "bogus_provider": "x"},
            "writing": "junk",
            "unknown_axis": {"anthropic": "claude-sonnet-5"},
        },
    )
    # Off-curated-list Anthropic ids drop (restoring one would only 422);
    # compat ids trim; unknown providers/axes vanish.
    assert aicfg.read_remembered(db) == {"analysis": {"openai_compat": "qwen3"}, "writing": {}}


def test_put_ai_models_serves_and_updates_remembered(client, db):
    _set_base_url(db)
    r = client.put(
        "/api/settings/ai-models",
        json={"analysis": {"provider": "openai_compat", "model": "llama3.3"}, "writing": None},
    )
    assert r.status_code == 200
    assert r.json()["remembered"]["analysis"] == {"openai_compat": "llama3.3"}
    # Tier-1 rows predate remembered entirely — the GET still serves the shape.
    _set_overrides(db, analysis="claude-sonnet-5")
    body = client.get("/api/settings/ai-models").json()
    assert body["analysis"] == {"provider": "anthropic", "model": "claude-sonnet-5"}
    assert body["remembered"] == {"analysis": {}, "writing": {}}
