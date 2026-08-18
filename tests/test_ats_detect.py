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
