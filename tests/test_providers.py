"""The provider roster (providers.py) and the generic .env secret accessors —
the two secrets coexist in one file, the base URL rides a tolerant settings
row, "local" means loopback only, and the client factory is the one place a
client class is named. No test reaches the network."""

import asyncio
import json
import os

import pytest

from jshq import apikey, oaicompat, paths, providers


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Own DATA_DIR + clean process env per test (write/clear mutate
    os.environ directly, so restore explicitly — mirrors test_apikey.py)."""
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    monkeypatch.delenv(apikey.ENV_KEY, raising=False)
    monkeypatch.delenv(providers.COMPAT_ENV_KEY, raising=False)
    yield
    os.environ.pop(apikey.ENV_KEY, None)
    os.environ.pop(providers.COMPAT_ENV_KEY, None)


def _set_base_url(db, url):
    db.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (providers.SETTING_KEY, json.dumps({"openai_compat": {"base_url": url}})),
    )
    db.commit()


# --- the two secrets share one .env ---


def test_compat_key_write_preserves_the_anthropic_line(tmp_path):
    apikey.write_key("sk-ant-test-1234")
    apikey.write_env_value(providers.COMPAT_ENV_KEY, "sk-compat-5678")
    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "ANTHROPIC_API_KEY=sk-ant-test-1234" in text
    assert f"{providers.COMPAT_ENV_KEY}=sk-compat-5678" in text


def test_anthropic_write_preserves_the_compat_line(tmp_path):
    apikey.write_env_value(providers.COMPAT_ENV_KEY, "sk-compat-5678")
    apikey.write_key("sk-ant-test-1234")
    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert f"{providers.COMPAT_ENV_KEY}=sk-compat-5678" in text
    assert "ANTHROPIC_API_KEY=sk-ant-test-1234" in text


def test_clearing_one_key_leaves_the_other(tmp_path):
    apikey.write_key("sk-ant-test-1234")
    apikey.write_env_value(providers.COMPAT_ENV_KEY, "sk-compat-5678")
    apikey.clear_env_value(providers.COMPAT_ENV_KEY)
    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert providers.COMPAT_ENV_KEY not in text
    assert "ANTHROPIC_API_KEY=sk-ant-test-1234" in text
    assert os.environ.get(providers.COMPAT_ENV_KEY) is None
    assert apikey.is_configured()


def test_env_value_status_reports_masked_never_the_key():
    apikey.write_env_value(providers.COMPAT_ENV_KEY, "sk-compat-WXYZ")
    st = apikey.env_value_status(providers.COMPAT_ENV_KEY)
    assert st == {
        "configured": True,
        "masked": "····WXYZ",
        "source": "data-dir",
        "editable": True,
    }


def test_shadowing_environment_compat_key_reported_not_editable(monkeypatch):
    monkeypatch.setenv(providers.COMPAT_ENV_KEY, "sk-shadow-1234")
    st = apikey.env_value_status(providers.COMPAT_ENV_KEY)
    assert st["source"] == "environment"
    assert st["editable"] is False


def test_env_file_written_owner_only(tmp_path):
    import stat
    import sys

    apikey.write_env_value(providers.COMPAT_ENV_KEY, "sk-compat-5678")
    if sys.platform != "win32":
        mode = stat.S_IMODE((tmp_path / ".env").stat().st_mode)
        assert mode == 0o600


# --- base URL config row ---


def test_read_config_tolerates_missing_and_garbled_rows(db):
    assert providers.read_config(db) == {"openai_compat": {"base_url": None}}
    db.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (providers.SETTING_KEY, "not json"),
    )
    db.commit()
    assert providers.compat_base_url(db) is None
    db.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (providers.SETTING_KEY, json.dumps({"openai_compat": {"base_url": "   "}})),
    )
    db.commit()
    assert providers.compat_base_url(db) is None


def test_base_url_round_trips(db):
    _set_base_url(db, "http://localhost:11434/v1")
    assert providers.compat_base_url(db) == "http://localhost:11434/v1"


def test_validate_base_url_accepts_and_normalizes():
    assert (
        providers.validate_base_url("  http://localhost:11434/v1/  ")
        == "http://localhost:11434/v1"
    )
    assert providers.validate_base_url("https://api.openai.com/v1").startswith("https")


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "localhost:11434",          # no scheme
        "ftp://host/v1",            # wrong scheme
        "http://",                  # no host
        "http://host/v1 extra",     # embedded space
        "http://host/v1\nX=1",      # control char / injection
    ],
)
def test_validate_base_url_rejects(bad):
    with pytest.raises(ValueError):
        providers.validate_base_url(bad)


# --- "local" means loopback only ---


@pytest.mark.parametrize(
    ("url", "local"),
    [
        ("http://localhost:11434/v1", True),
        ("http://127.0.0.1:8080/v1", True),
        ("http://127.5.5.5/v1", True),
        ("http://[::1]:1234/v1", True),
        ("http://192.168.1.20:11434/v1", False),  # LAN is NOT local
        ("http://10.0.0.5/v1", False),
        ("https://api.openai.com/v1", False),
        ("http://myhost.local/v1", False),
    ],
)
def test_is_local_matrix(url, local):
    assert providers.is_local(url) is local


# --- readiness and messages ---


def test_readiness_per_provider(db, monkeypatch):
    assert providers.is_ready(db, "anthropic") is False
    monkeypatch.setenv(apikey.ENV_KEY, "sk-ant-x")
    assert providers.is_ready(db, "anthropic") is True

    assert providers.is_ready(db, "openai_compat") is False
    _set_base_url(db, "http://localhost:11434/v1")
    # The compat key is optional — a base URL alone is ready (Ollama has no key).
    assert providers.is_ready(db, "openai_compat") is True

    assert providers.is_ready(db, "nonsense") is False


def test_missing_messages_are_the_actionable_pair():
    assert providers.missing_message("anthropic") == apikey.MISSING_MESSAGE
    assert providers.missing_message("openai_compat") == providers.MISSING_ENDPOINT_MESSAGE
    assert "Settings" in providers.MISSING_ENDPOINT_MESSAGE


# --- the client factory ---


def test_build_client_wires_the_compat_adapter(db, monkeypatch):
    _set_base_url(db, "http://localhost:11434/v1")
    monkeypatch.setenv(providers.COMPAT_ENV_KEY, "sk-compat-x")
    client = providers.build_client(db, "openai_compat", max_retries=4)
    assert isinstance(client, oaicompat.OpenAICompatClient)
    assert client._base_url == "http://localhost:11434/v1"
    assert client._api_key == "sk-compat-x"
    assert client._max_retries == 4


def test_build_client_compat_without_endpoint_raises(db):
    with pytest.raises(ValueError):
        providers.build_client(db, "openai_compat", max_retries=2)


def test_build_client_anthropic_lazily_imports(db):
    pytest.importorskip("anthropic")
    client = providers.build_client(db, "anthropic", max_retries=2)
    assert type(client).__name__ == "AsyncAnthropic"


def test_build_client_unknown_provider_raises(db):
    with pytest.raises(ValueError):
        providers.build_client(db, "gemini", max_retries=2)


# --- the /api/settings/ai-providers routes ---


def test_providers_routes_precede_the_settings_catchall(client):
    # Same ordering hazard as api-key: "ai-providers" must not match
    # /api/settings/{key} (which would 404 it as a non-editable setting).
    assert client.get("/api/settings/ai-providers").status_code == 200


def test_put_round_trips_and_normalizes(client, tmp_path):
    r = client.put(
        "/api/settings/ai-providers",
        json={"base_url": "http://localhost:11434/v1/", "api_key": "sk-compat-1234"},
    )
    assert r.status_code == 200
    st = r.json()
    assert st["configured"] is True
    assert st["base_url"] == "http://localhost:11434/v1"  # trailing slash normalized
    assert st["local"] is True
    assert st["key"]["masked"] == "····1234"
    assert "sk-compat-1234" not in json.dumps(st)
    assert os.environ.get(providers.COMPAT_ENV_KEY) == "sk-compat-1234"


def test_put_bad_url_is_coded_and_writes_nothing(client):
    r = client.put("/api/settings/ai-providers", json={"base_url": "localhost:11434"})
    assert r.status_code == 422
    assert "[JSHQ-208]" in r.json()["detail"]
    assert client.get("/api/settings/ai-providers").json()["configured"] is False


def test_put_key_semantics_value_blank_absent(client):
    client.put(
        "/api/settings/ai-providers",
        json={"base_url": "http://localhost:11434/v1", "api_key": "sk-compat-1234"},
    )
    # Absent key: re-saving the URL never wipes the stored key.
    r = client.put(
        "/api/settings/ai-providers", json={"base_url": "http://127.0.0.1:8080/v1"}
    )
    assert r.json()["key"]["configured"] is True
    # Empty string clears it.
    r = client.put(
        "/api/settings/ai-providers",
        json={"base_url": "http://127.0.0.1:8080/v1", "api_key": ""},
    )
    assert r.json()["key"]["configured"] is False
    assert os.environ.get(providers.COMPAT_ENV_KEY) is None


def test_delete_clears_config_and_key_but_not_axes(client, db):
    client.put(
        "/api/settings/ai-providers",
        json={"base_url": "http://localhost:11434/v1", "api_key": "sk-compat-1234"},
    )
    client.put(
        "/api/settings/ai-models",
        json={"analysis": {"provider": "openai_compat", "model": "llama3.3"}},
    )
    r = client.delete("/api/settings/ai-providers")
    assert r.status_code == 200
    assert r.json()["configured"] is False
    assert os.environ.get(providers.COMPAT_ENV_KEY) is None
    # The axis choice survives deliberately (runtime guards carry the drift;
    # re-adding the endpoint restores it untouched).
    assert client.get("/api/settings/ai-models").json()["analysis"] == {
        "provider": "openai_compat", "model": "llama3.3",
    }


def test_test_route_503s_before_any_network_when_unconfigured(client):
    r = client.post("/api/settings/ai-providers/test")
    assert r.status_code == 503
    assert r.json()["detail"] == providers.MISSING_ENDPOINT_MESSAGE


def test_test_route_maps_probe_outcomes(client, monkeypatch):
    client.put(
        "/api/settings/ai-providers", json={"base_url": "http://localhost:11434/v1"}
    )

    async def ok_probe(base_url, api_key, **kw):
        return {"ok": True, "models": ["llama3.3", "qwen"]}

    monkeypatch.setattr("jshq.main.oaicompat.probe", ok_probe)
    body = client.post("/api/settings/ai-providers/test").json()
    assert body == {"ok": True, "error": None, "models": ["llama3.3", "qwen"]}

    async def down_probe(base_url, api_key, **kw):
        raise oaicompat.APIConnectionError("refused")

    monkeypatch.setattr("jshq.main.oaicompat.probe", down_probe)
    body = client.post("/api/settings/ai-providers/test").json()
    assert body["ok"] is False
    assert "http://localhost:11434/v1" in body["error"]


# --- status payload ---


def test_compat_status_never_carries_the_key(db, monkeypatch):
    assert providers.compat_status(db) == {
        "configured": False,
        "base_url": None,
        "local": None,
        "key": {"configured": False, "masked": None, "source": None, "editable": True},
    }
    _set_base_url(db, "http://localhost:11434/v1")
    monkeypatch.setenv(providers.COMPAT_ENV_KEY, "sk-compat-WXYZ")
    st = providers.compat_status(db)
    assert st["configured"] is True
    assert st["local"] is True
    assert st["key"]["masked"] == "····WXYZ"
    assert "sk-compat-WXYZ" not in json.dumps(st)
