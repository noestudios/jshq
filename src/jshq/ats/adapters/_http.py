"""Shared HTTP helpers for ATS adapters: JSON or AdapterError, nothing else.

One retry on transport-shaped errors (timeouts, dropped connections): a
Workday tenant can run ~160 serial requests per refresh, so with per-request
transient-failure probability p a single slow response aborts the whole
company — observed in practice as scheduled runs failing repeatedly on the
big tenants, always ReadTimeout, on a different endpoint each time. One
retry takes per-request failure p to p². HTTP status codes never retry —
a 4xx/5xx is the board speaking, not the network.
"""

import asyncio
import json

import httpx

from ..normalize import AdapterError

_JSON_HEADERS = {"Accept": "application/json"}
_RETRY_DELAY_S = 2.0


async def _send(request, desc: str) -> httpx.Response:
    """Await request() with one retry on httpx.TransportError; any other
    httpx error (redirect loops, invalid URLs) raises AdapterError at once.
    The error text keeps the "<desc>: <ExcType>: <msg>" shape refresh.py's
    connectivity markers match against."""
    for attempt in (0, 1):
        try:
            return await request()
        except httpx.TransportError as e:
            if attempt:
                raise AdapterError(f"{desc}: {type(e).__name__}: {e}") from e
            await asyncio.sleep(_RETRY_DELAY_S)
        except httpx.HTTPError as e:
            raise AdapterError(f"{desc}: {type(e).__name__}: {e}") from e
    raise AssertionError("unreachable")


async def get_json(client: httpx.AsyncClient, url: str) -> dict | list:
    r = await _send(lambda: client.get(url, headers=_JSON_HEADERS), f"GET {url}")
    if r.status_code != 200:
        raise AdapterError(f"GET {url}: HTTP {r.status_code}")
    try:
        return r.json()
    except (json.JSONDecodeError, ValueError) as e:
        raise AdapterError(f"GET {url}: bad JSON: {e}") from e


async def post_json(client: httpx.AsyncClient, url: str, body: dict) -> dict:
    r = await _send(
        lambda: client.post(url, json=body, headers=_JSON_HEADERS), f"POST {url}"
    )
    if r.status_code != 200:
        raise AdapterError(f"POST {url}: HTTP {r.status_code}")
    try:
        return r.json()
    except (json.JSONDecodeError, ValueError) as e:
        raise AdapterError(f"POST {url}: bad JSON: {e}") from e


async def get_text(client: httpx.AsyncClient, url: str) -> str:
    """Fetch an HTML page (e.g. to scrape a JSON-LD block) or AdapterError."""
    r = await _send(lambda: client.get(url), f"GET {url}")
    if r.status_code != 200:
        raise AdapterError(f"GET {url}: HTTP {r.status_code}")
    return r.text
