"""The OpenAI-compat adapter (oaicompat.py) — the Tier 2 wire translation.

Everything runs against httpx.MockTransport: the suite is offline by hard
rule, and the transport seam is exactly how the adapter is meant to be tested.
The contract under test is the de facto client interface the feature-module
fakes pin (messages.create kwargs in, content-blocks + Anthropic-named usage
out) — if these pass, every feature module ports untouched.
"""

import asyncio
import json

import httpx
import pytest

from jshq import jobparse, linkedin_titles, oaicompat, refine
from jshq.scoring import _is_rate_limit, haiku, learned, synthesis

BASE = "http://localhost:11434/v1"


def _transport(handler):
    return httpx.MockTransport(handler)


def _client(handler, **kwargs):
    return oaicompat.OpenAICompatClient(BASE, transport=_transport(handler), **kwargs)


def _ok_body(text="hello", usage=None, **extra):
    body = {
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "model": "test-model",
        **extra,
    }
    if usage is not None:
        body["usage"] = usage
    return body


def _create(client, **overrides):
    kwargs = {
        "model": "llama3",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "hi"}],
    }
    kwargs.update(overrides)
    return asyncio.run(client.messages.create(**kwargs))


# --- request translation ---


def test_system_blocks_flatten_and_cache_control_never_reaches_the_wire():
    seen = {}

    def handler(request):
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json=_ok_body())

    _create(
        _client(handler),
        system=[
            {"type": "text", "text": "part one. ", "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "part two."},
        ],
    )
    payload = seen["payload"]
    assert payload["messages"][0] == {"role": "system", "content": "part one. part two."}
    assert "cache_control" not in json.dumps(payload)


def test_plain_string_system_and_temperature_pass_through():
    seen = {}

    def handler(request):
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json=_ok_body())

    _create(_client(handler), system="be terse", temperature=0.0)
    assert seen["payload"]["messages"][0] == {"role": "system", "content": "be terse"}
    assert seen["payload"]["temperature"] == 0.0
    assert seen["payload"]["max_tokens"] == 100


def test_thinking_kwarg_is_accepted_and_dropped():
    seen = {}

    def handler(request):
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json=_ok_body())

    _create(_client(handler), thinking={"type": "disabled"})
    assert "thinking" not in seen["payload"]


def test_auth_header_only_when_a_key_is_set():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=_ok_body())

    _create(_client(handler))
    assert seen["auth"] is None
    _create(oaicompat.OpenAICompatClient(BASE, "sk-test", transport=_transport(handler)))
    assert seen["auth"] == "Bearer sk-test"


# --- schema translation ---


def test_output_config_becomes_response_format_with_closed_objects():
    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {"type": "object", "properties": {"x": {"type": "string"}}},
            }
        },
        "required": ["items"],
    }
    original = json.dumps(schema)
    seen = {}

    def handler(request):
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json=_ok_body())

    _create(
        _client(handler),
        output_config={"format": {"type": "json_schema", "schema": schema}},
    )
    sent = seen["payload"]["response_format"]
    assert sent["type"] == "json_schema"
    translated = sent["json_schema"]["schema"]
    assert translated["additionalProperties"] is False
    assert translated["properties"]["items"]["items"]["additionalProperties"] is False
    # Strict mode is deliberately off: the scoring schema is dynamic and
    # strict demands required == all keys.
    assert "strict" not in sent["json_schema"]
    # The caller's dict is never mutated.
    assert json.dumps(schema) == original


def test_every_shipped_output_schema_survives_translation():
    """Each module's real schema round-trips the translator and stays
    JSON-serializable — the shape the wire needs."""
    from jshq.scoring.criteria import load_criteria

    schemas = [
        synthesis.SCHEMA,
        refine.SCHEMA,
        learned.SCHEMA,
        linkedin_titles.SCHEMA,
        jobparse._LLM_SCHEMA,
        haiku.build_schema(),
        haiku.build_schema(load_criteria()),
    ]
    for schema in schemas:
        translated = oaicompat._translate_schema(schema)
        json.dumps(translated)  # must be serializable
        assert translated["additionalProperties"] is False


def test_schema_400_falls_back_once_to_inline_schema():
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    payloads = []

    def handler(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        if "response_format" in payload:
            return httpx.Response(400, json={"error": "response_format unsupported"})
        return httpx.Response(200, json=_ok_body('{"x": "y"}'))

    resp = _create(
        # max_retries=0 proves the fallback does NOT consume the retry budget.
        _client(handler, max_retries=0),
        system="sys",
        output_config={"format": {"type": "json_schema", "schema": schema}},
    )
    assert resp.content[0].text == '{"x": "y"}'
    assert len(payloads) == 2
    fallback = payloads[1]
    assert "response_format" not in fallback
    assert "JSON Schema" in fallback["messages"][0]["content"]
    assert json.dumps(schema) in fallback["messages"][0]["content"]


def test_second_400_in_fallback_mode_is_a_real_error():
    schema = {"type": "object"}

    def handler(request):
        return httpx.Response(400, json={"error": "no"})

    with pytest.raises(oaicompat.APIStatusError) as err:
        _create(
            _client(handler),
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
    assert err.value.status_code == 400


# --- response mapping ---


def test_response_maps_to_text_blocks_and_anthropic_usage_names():
    usage = {
        "prompt_tokens": 120,
        "completion_tokens": 30,
        "prompt_tokens_details": {"cached_tokens": 40},
    }

    def handler(request):
        return httpx.Response(200, json=_ok_body("out", usage=usage))

    resp = _create(_client(handler))
    assert [b.text for b in resp.content if b.type == "text"] == ["out"]
    assert resp.usage.input_tokens == 120
    assert resp.usage.output_tokens == 30
    assert resp.usage.cache_read_input_tokens == 40
    assert resp.usage.cache_creation_input_tokens == 0


def test_absent_usage_maps_to_none():
    def handler(request):
        return httpx.Response(200, json=_ok_body("out"))

    resp = _create(_client(handler))
    assert getattr(resp, "usage", None) is None


def test_null_content_reads_as_empty_text():
    def handler(request):
        return httpx.Response(200, json=_ok_body(None))

    resp = _create(_client(handler))
    assert resp.content[0].text == ""


def test_non_json_200_is_a_typed_error():
    def handler(request):
        return httpx.Response(200, text="<html>proxy page</html>")

    with pytest.raises(oaicompat.OpenAICompatError):
        _create(_client(handler))


# --- retries and typed errors ---


def test_429_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(oaicompat, "_retry_delay", lambda attempt, retry_after: 0)
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "0"}, json={})
        return httpx.Response(200, json=_ok_body())

    resp = _create(_client(handler, max_retries=2))
    assert len(calls) == 2
    assert resp.content[0].text == "hello"


def test_retry_after_header_is_honored():
    assert oaicompat._retry_delay(0, "7") == 7.0
    assert oaicompat._retry_delay(0, "bogus") < 7.0  # falls back to backoff


def test_max_retries_zero_fails_fast_on_429():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(429, json={})

    with pytest.raises(oaicompat.RateLimitError):
        _create(_client(handler, max_retries=0))
    assert len(calls) == 1


def test_401_raises_authentication_error_with_no_retry(monkeypatch):
    monkeypatch.setattr(oaicompat, "_retry_delay", lambda attempt, retry_after: 0)
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(401, json={"error": "bad key"})

    with pytest.raises(oaicompat.AuthenticationError):
        _create(_client(handler, max_retries=3))
    assert len(calls) == 1


def test_5xx_retries_up_to_budget(monkeypatch):
    monkeypatch.setattr(oaicompat, "_retry_delay", lambda attempt, retry_after: 0)
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503, json={})

    with pytest.raises(oaicompat.APIStatusError) as err:
        _create(_client(handler, max_retries=2))
    assert len(calls) == 3
    assert err.value.status_code == 503


def test_connection_error_maps_to_typed_error(monkeypatch):
    monkeypatch.setattr(oaicompat, "_retry_delay", lambda attempt, retry_after: 0)

    def handler(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(oaicompat.APIConnectionError):
        _create(_client(handler, max_retries=1))


def test_scoring_rate_limit_detection_accepts_the_adapters_429():
    """scoring._is_rate_limit duck-types the class name / response status —
    the adapter's RateLimitError must satisfy it with zero scoring changes."""
    response = httpx.Response(429, request=httpx.Request("POST", BASE))
    exc = oaicompat.RateLimitError("limited", response=response)
    assert _is_rate_limit(exc) is True
    # And the plain status error satisfies the response-status branch too.
    plain = oaicompat.APIStatusError("x", response=response)
    assert _is_rate_limit(plain) is True


# --- probe ---


def _probe(handler, api_key=None):
    return asyncio.run(oaicompat.probe(BASE, api_key, transport=_transport(handler)))


def test_probe_returns_model_ids():
    def handler(request):
        assert request.url.path.endswith("/v1/models")
        return httpx.Response(200, json={"data": [{"id": "llama3"}, {"id": "qwen"}]})

    assert _probe(handler) == {"ok": True, "models": ["llama3", "qwen"]}


def test_probe_tolerates_an_unparseable_200():
    def handler(request):
        return httpx.Response(200, text="ok")

    assert _probe(handler) == {"ok": True, "models": []}


def test_probe_distinguishes_rejection_from_unreachable():
    def rejected(request):
        return httpx.Response(401, json={})

    with pytest.raises(oaicompat.AuthenticationError):
        _probe(rejected, api_key="bad")

    def unreachable(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(oaicompat.APIConnectionError):
        _probe(unreachable)
