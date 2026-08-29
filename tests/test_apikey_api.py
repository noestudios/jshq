"""The key settings endpoints: status in, key never out, and the routes sit
ahead of the /api/settings/{key} catch-all. The test endpoint 503s keyless and
never reaches the network (conftest deletes the key for every test)."""

import pytest

from jshq import apikey, paths


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Per-test DATA_DIR and a clean process key; restore os.environ after, since
    the endpoints mutate it directly through apikey."""
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    monkeypatch.delenv(apikey.ENV_KEY, raising=False)
    yield
    import os

    os.environ.pop(apikey.ENV_KEY, None)


def test_get_reports_unconfigured(client):
    r = client.get("/api/settings/api-key")
    assert r.status_code == 200
    assert r.json() == {
        "configured": False,
        "masked": None,
        "source": None,
        "editable": True,
        "rejected": False,  # #33: no key ⇒ nothing to reject
    }


def test_put_then_get_reflects_configured(client, tmp_path):
    r = client.put("/api/settings/api-key", json={"key": "sk-ant-secret-WXYZ"})
    assert r.status_code == 200
    assert r.json()["configured"] is True
    assert r.json()["masked"] == "····WXYZ"
    # And it persisted to DATA_DIR/.env, not the DB.
    assert "ANTHROPIC_API_KEY=sk-ant-secret-WXYZ" in (tmp_path / ".env").read_text(encoding="utf-8")

    g = client.get("/api/settings/api-key")
    assert g.json()["configured"] is True
    assert g.json()["source"] == "data-dir"


def test_key_is_never_echoed_back(client):
    secret = "sk-ant-super-secret-DO-NOT-LEAK"
    put = client.put("/api/settings/api-key", json={"key": secret})
    get = client.get("/api/settings/api-key")
    for body in (put.text, get.text):
        assert secret not in body
        assert "secret" not in body.lower() or "····" in body  # only the mask survives


def test_delete_clears(client, tmp_path):
    client.put("/api/settings/api-key", json={"key": "sk-ant-goeswith-AB12"})
    d = client.delete("/api/settings/api-key")
    assert d.status_code == 200
    assert d.json()["configured"] is False
    assert "ANTHROPIC_API_KEY" not in (tmp_path / ".env").read_text(encoding="utf-8")


def test_put_empty_key_422(client):
    r = client.put("/api/settings/api-key", json={"key": "   "})
    assert r.status_code == 422


def test_put_key_with_space_422(client):
    # NonEmptyStr strips outer space; an inner space is rejected by apikey.write_key.
    r = client.put("/api/settings/api-key", json={"key": "has inner space"})
    assert r.status_code == 422


def test_test_endpoint_503_without_key(client):
    """The probe never runs keyless — it 503s before constructing a client, so no
    test can reach api.anthropic.com."""
    r = client.post("/api/settings/api-key/test")
    assert r.status_code == 503


def test_test_endpoint_reports_empty_balance_as_billing(client, monkeypatch):
    """A configured key with no credits: Anthropic answers 400 with a
    'credit balance is too low' body. The probe must say the account is out of
    credits, not echo a bare status that reads as a broken key."""
    import anthropic
    import httpx

    apikey.write_env_value(apikey.ENV_KEY, "sk-ant-" + "x" * 40)

    class _NoCreditClient:
        def __init__(self, *a, **k):
            self.messages = self

        async def create(self, *a, **k):
            request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
            response = httpx.Response(400, request=request)
            raise anthropic.APIStatusError(
                "Your credit balance is too low to access the Anthropic API.",
                response=response,
                body=None,
            )

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _NoCreditClient)
    r = client.post("/api/settings/api-key/test")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "out of credits" in body["error"]
    assert "console.anthropic.com" in body["error"]


def test_routes_precede_catchall(client):
    """/api/settings/api-key must not be swallowed by /api/settings/{key} (which
    would 404 it as an unknown editable setting)."""
    r = client.get("/api/settings/api-key")
    assert r.status_code == 200  # not the {key} catch-all's 404
    # The catch-all still works for its own keys.
    assert client.get("/api/settings/dismiss_reasons").status_code == 200
    assert client.get("/api/settings/not-a-real-setting").status_code == 404
