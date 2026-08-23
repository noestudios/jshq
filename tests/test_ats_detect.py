"""detect_company fallback: host- and name-derived slug probing.

No live endpoints (per the testing rules) — the httpx client and verify() are
faked, so these exercise the fallback's slug ordering and gating deterministically.
"""

import asyncio
from types import SimpleNamespace

import pytest

from jshq.ats import detect
from jshq.ats import patterns as p


def _fake_client(html="<html>no ats signature here</html>"):
    async def get(url, headers=None):
        return SimpleNamespace(url=url, text=html, status_code=200)

    return SimpleNamespace(get=get)


@pytest.fixture(autouse=True)
def _allow_robots(monkeypatch):
    async def _allow(client, url):
        return True

    monkeypatch.setattr(detect, "_robots_allows", _allow)


def _verify_only(monkeypatch, boards):
    """Stub verify() to succeed only for the given {(ats_type, slug): evidence}."""

    async def verify(client, ats_type, slug):
        return boards.get((ats_type, slug))

    monkeypatch.setattr(detect, "verify", verify)


def test_detect_company_probes_host_derived_slug(monkeypatch):
    # Client-rendered careers page (no in-page signature); the tracked company
    # name does not match the board, but the careers-URL host does. Before the
    # host probe this returned "no ATS"; now the host label resolves it.
    _verify_only(monkeypatch, {(p.GREENHOUSE, "exampleco"): {"job_count": 42, "board_name": "Exampleco"}})
    company = {
        "id": 1, "name": "Cobalt Analytics",
        "careers_url": "https://www.exampleco.com/careers", "website": "https://www.exampleco.com",
    }
    res = asyncio.run(detect.detect_company(_fake_client(), company))
    assert (res["ats_type"], res["ats_slug"], res["method"]) == (p.GREENHOUSE, "exampleco", "slug-probe")


def test_detect_company_name_probe_still_resolves(monkeypatch):
    # Regression: a name that matches still resolves even when the host doesn't.
    _verify_only(monkeypatch, {(p.GREENHOUSE, "exampleco"): {"job_count": 7, "board_name": "Exampleco"}})
    company = {
        "id": 2, "name": "Exampleco",
        "careers_url": "https://careers.somehost.test", "website": None,
    }
    res = asyncio.run(detect.detect_company(_fake_client(), company))
    assert (res["ats_type"], res["ats_slug"]) == (p.GREENHOUSE, "exampleco")


def test_detect_company_unresolved_when_nothing_matches(monkeypatch):
    _verify_only(monkeypatch, {})  # no board verifies
    company = {
        "id": 3, "name": "Cobalt Analytics",
        "careers_url": "https://www.exampleco.com/careers", "website": None,
    }
    res = asyncio.run(detect.detect_company(_fake_client(), company))
    assert res["ats_type"] is None


def test_detect_company_html_signature_beats_probe(monkeypatch):
    # If the page DOES expose a signature (incl. the embedded greenhouse token),
    # it wins over any slug probe — the company name AND the host are irrelevant.
    _verify_only(monkeypatch, {(p.GREENHOUSE, "acmeco"): {"job_count": 99, "board_name": "Acmeco"}})
    html = '<script>window.__ENV={"PUBLIC_GREENHOUSE_BOARD":"acmeco"}</script>'
    company = {
        "id": 4, "name": "Contoso",  # neither the name nor the host is the board slug
        "careers_url": "https://contoso.example/careers", "website": "https://contoso.example",
    }
    res = asyncio.run(detect.detect_company(_fake_client(html), company))
    assert (res["ats_type"], res["ats_slug"], res["method"]) == (p.GREENHOUSE, "acmeco", "html-scan")


# --- blind-probe identity guard -----------------
# These run the REAL verify() over httpx.MockTransport (no stubbing): a
# name-derived slug that lands on a STRANGER'S board must be rejected, not
# written — the Marigold Workshop -> lever/marigold mis-map.

import httpx

from jshq.ats.detect import detect_company, verify

COMPANY = {
    "id": 62,
    "name": "Marigold Workshop",
    "careers_url": "https://careers.exampleco.org/",
    "website": None,
}

# Careers page with no ATS signature -> detection falls through to the probe.
BLANK_PAGE = "<html><body>Join our team! Email jobs@exampleco.org</body></html>"

STRANGER_POSTING = {
    "id": "aaa-111",
    "text": "Provider Success - Senior Account Manager",
    "descriptionPlain": "Marigold is a pet-insurance marketplace connecting owners to vets.",
    "additionalPlain": "",
    "hostedUrl": "https://jobs.lever.co/marigold/aaa-111",
}

GENUINE_POSTING = {
    "id": "bbb-222",
    "text": "Senior Product Designer",
    "descriptionPlain": "Marigold Workshop is a nonprofit children's media studio.",
    "additionalPlain": "",
    "hostedUrl": "https://jobs.lever.co/marigoldworkshop/bbb-222",
}


def run_detect(handler, company=COMPANY):
    async def go():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await detect_company(client, company)

    return asyncio.run(go())


def run_verify(handler, ats_type, slug):
    async def go():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await verify(client, ats_type, slug)

    return asyncio.run(go())


def _base_handler(request):
    """robots + the signature-less careers page; everything else 404s."""
    if request.url.path == "/robots.txt":
        return httpx.Response(404)
    if request.url.host == "careers.exampleco.org":
        return httpx.Response(200, text=BLANK_PAGE)
    return httpx.Response(404)


def test_probe_rejects_strangers_lever_board():
    """The failure mode: lever/marigold 200s with a non-empty board owned by a
    different company. Identity scan must reject it and leave the result
    unmapped, with the rejection recorded."""

    def handler(request):
        if request.url.host == "api.lever.co" and request.url.path == "/v0/postings/marigold":
            return httpx.Response(200, json=[STRANGER_POSTING])
        return _base_handler(request)

    result = run_detect(handler)
    assert result["ats_type"] is None and result["ats_slug"] is None
    assert any(
        "lever/marigold: board identity mismatch, rejected" in e for e in result["errors"]
    )


def test_probe_accepts_lever_board_that_names_the_company():
    def handler(request):
        if request.url.host == "api.lever.co" and request.url.path == "/v0/postings/marigoldworkshop":
            return httpx.Response(200, json=[GENUINE_POSTING])
        return _base_handler(request)

    result = run_detect(handler)
    assert (result["ats_type"], result["ats_slug"]) == (p.LEVER, "marigoldworkshop")
    assert result["method"] == "slug-probe"
    # probe-only evidence is stripped before storing
    assert "identity_text" not in result["evidence"]


def test_probe_greenhouse_board_name_must_match():
    """The GH/SR guard is strengthened from "board name exists" to "board
    name names the company" — a stranger's board with a name still fails."""

    def gh(request, board_name):
        path = request.url.path
        if path == "/v1/boards/marigoldworkshop/jobs":
            return httpx.Response(200, json={"jobs": [{"id": 1, "title": "Designer"}]})
        if path == "/v1/boards/marigoldworkshop":
            return httpx.Response(200, json={"name": board_name})
        return _base_handler(request)

    result = run_detect(lambda r: gh(r, "Marigold Petcare Test Board"))
    assert result["ats_type"] is None
    assert any("board name mismatch, rejected" in e for e in result["errors"])

    result = run_detect(lambda r: gh(r, "Marigold Workshop"))
    assert (result["ats_type"], result["ats_slug"]) == (p.GREENHOUSE, "marigoldworkshop")
    assert result["evidence"]["board_name"] == "Marigold Workshop"


def test_probe_empty_board_still_rejected():
    def handler(request):
        if request.url.host == "api.lever.co":
            return httpx.Response(200, json=[])
        return _base_handler(request)

    result = run_detect(handler)
    assert result["ats_type"] is None
    assert any("empty board, rejected" in e for e in result["errors"])


def test_verify_captures_identity_text_for_lever_and_ashby():
    def lever_handler(request):
        return httpx.Response(200, json=[STRANGER_POSTING])

    evidence = run_verify(lever_handler, p.LEVER, "marigold")
    assert evidence["job_count"] == 1
    assert "pet-insurance marketplace" in evidence["identity_text"]

    def ashby_handler(request):
        return httpx.Response(
            200,
            json={"apiVersion": "1", "jobs": [
                {"title": "Designer", "descriptionPlain": "Acme builds rockets."}
            ]},
        )

    evidence = run_verify(ashby_handler, p.ASHBY, "acme")
    assert "Acme builds rockets." in evidence["identity_text"]


def test_host_derived_probe_gated_on_brand_or_name(monkeypatch):
    """Host-derived slugs accept on host-brand identity (the Cobalt/Exampleco
    case above) — but a board naming NEITHER the tracked company nor the host
    brand is a stranger's board and must be rejected (pre-guard code accepted
    it on job_count alone)."""
    _verify_only(monkeypatch, {(p.GREENHOUSE, "exampleco"): {"job_count": 42, "board_name": "Northwind Traders"}})
    company = {
        "id": 5, "name": "Cobalt Analytics",
        "careers_url": "https://www.exampleco.com/careers", "website": None,
    }
    res = asyncio.run(detect.detect_company(_fake_client(), company))
    assert res["ats_type"] is None
    assert any("board name mismatch, rejected" in e for e in res["errors"])
