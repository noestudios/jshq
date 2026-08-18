"""_http retry contract: one retry on transport errors, none on HTTP status."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from jshq.ats.adapters import _http
from jshq.ats.normalize import AdapterError


@pytest.fixture(autouse=True)
def _no_retry_delay(monkeypatch):
    monkeypatch.setattr(_http, "_RETRY_DELAY_S", 0)


def ok_response(payload):
    return SimpleNamespace(status_code=200, json=lambda: payload, text="")


def test_get_json_retries_once_on_timeout():
    calls = {"n": 0}

    async def get(url, headers=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("slow")
        return ok_response({"total": 1})

    client = SimpleNamespace(get=get)
    assert asyncio.run(_http.get_json(client, "https://x/jobs")) == {"total": 1}
    assert calls["n"] == 2


def test_get_json_second_timeout_raises_with_type_name():
    # Exactly one retry; the error keeps the "<desc>: <ExcType>:" shape the
    # refresh outage guard's connectivity markers match against.
    calls = {"n": 0}

    async def get(url, headers=None):
        calls["n"] += 1
        raise httpx.ReadTimeout("slow")

    client = SimpleNamespace(get=get)
    with pytest.raises(AdapterError, match="GET https://x/jobs: ReadTimeout"):
        asyncio.run(_http.get_json(client, "https://x/jobs"))
    assert calls["n"] == 2


def test_post_json_retries_on_connect_error():
    calls = {"n": 0}

    async def post(url, json=None, headers=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("reset")
        return ok_response({"jobPostings": []})

    client = SimpleNamespace(post=post)
    data = asyncio.run(_http.post_json(client, "https://x/jobs", {}))
    assert data == {"jobPostings": []}
    assert calls["n"] == 2


def test_http_status_never_retries():
    calls = {"n": 0}

    async def get(url, headers=None):
        calls["n"] += 1
        return SimpleNamespace(status_code=500, json=lambda: {}, text="")

    client = SimpleNamespace(get=get)
    with pytest.raises(AdapterError, match="HTTP 500"):
        asyncio.run(_http.get_json(client, "https://x/j"))
    assert calls["n"] == 1


def test_non_transport_http_error_never_retries():
    calls = {"n": 0}

    async def get(url, headers=None):
        calls["n"] += 1
        raise httpx.TooManyRedirects("loop")

    client = SimpleNamespace(get=get)
    with pytest.raises(AdapterError, match="TooManyRedirects"):
        asyncio.run(_http.get_json(client, "https://x/j"))
    assert calls["n"] == 1


def test_get_text_retries_on_connect_timeout():
    calls = {"n": 0}

    async def get(url):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectTimeout("t")
        return SimpleNamespace(status_code=200, text="<html>")

    client = SimpleNamespace(get=get)
    assert asyncio.run(_http.get_text(client, "https://x")) == "<html>"
    assert calls["n"] == 2
