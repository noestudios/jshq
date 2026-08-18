"""Company-logo fetch/cache + payload exposure — all HTTP mocked via MockTransport."""

import asyncio
import hashlib

import httpx
import pytest

from jshq import logos

# Valid magic bytes + padding so length is over MIN_BYTES (the sniff path is real).
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 256
ICO = b"\x00\x00\x01\x00" + b"\x00" * 256


def _run(coro):
    return asyncio.run(coro)


def _mock_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _fetch(handler, domain="acme.com"):
    async with _mock_client(handler) as c:
        return await logos.fetch_logo(domain, client=c)


# --- registrable_domain ---


@pytest.mark.parametrize(
    "inp,expect",
    [
        ("https://careers.exampleco.com", "exampleco.com"),
        ("https://careers.acmeco.org", "acmeco.org"),
        ("https://www.exampleco.com/en-us/careers", "exampleco.com"),
        ("https://brandco.team/careers/", "brandco.team"),
        ("https://examplefoundation.org/about", "examplefoundation.org"),
        ("jobs.example.com", "example.com"),
        ("https://sub.brand.co/x", "brand.co"),
        (None, None),
        ("", None),
        ("localhost", None),
    ],
)
def test_registrable_domain(inp, expect):
    assert logos.registrable_domain(inp) == expect


# --- fetch_logo ---


def test_fetch_logo_apple_touch_icon_from_head():
    def handler(request):
        host, path = request.url.host, request.url.path
        if host == "acme.com" and path == "/":
            return httpx.Response(200, text='<link rel="apple-touch-icon" href="/touch.png">')
        if host == "acme.com" and path == "/touch.png":
            return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
        return httpx.Response(404)

    content, ext = _run(_fetch(handler))
    assert ext == "png" and content == PNG


def test_fetch_logo_falls_back_to_duckduckgo():
    def handler(request):
        host, path = request.url.host, request.url.path
        if host == "acme.com" and path == "/":
            return httpx.Response(200, text="<html>no icons declared</html>")
        if host == "icons.duckduckgo.com":
            return httpx.Response(200, content=ICO, headers={"content-type": "image/x-icon"})
        return httpx.Response(404)  # conventional apple-touch paths miss

    content, ext = _run(_fetch(handler))
    assert ext == "ico" and content == ICO


def test_fetch_logo_rejects_duckduckgo_placeholder(monkeypatch):
    placeholder = b"\x89PNG\r\n\x1a\n" + b"GLOBE" * 40
    monkeypatch.setattr(logos, "_DDG_PLACEHOLDER_SHA1", {hashlib.sha1(placeholder).hexdigest()})

    def handler(request):
        if request.url.host == "acme.com" and request.url.path == "/":
            return httpx.Response(200, text="x")
        if request.url.host == "icons.duckduckgo.com":
            return httpx.Response(200, content=placeholder, headers={"content-type": "image/png"})
        return httpx.Response(404)

    assert _run(_fetch(handler)) is None  # the globe is rejected → monogram


def test_fetch_logo_none_when_nothing_found():
    assert _run(_fetch(lambda request: httpx.Response(404))) is None


def test_fetch_logo_rejects_tiny_response():
    def handler(request):
        host, path = request.url.host, request.url.path
        if host == "acme.com" and path == "/apple-touch-icon.png":
            return httpx.Response(200, content=b"\x89PNG\r\n\x1a\n", headers={"content-type": "image/png"})
        if host == "acme.com" and path == "/":
            return httpx.Response(200, text="")
        return httpx.Response(404)

    assert _run(_fetch(handler)) is None  # 8 bytes is below MIN_BYTES


# --- refresh_company_logo (cache write + DB) ---


def test_refresh_company_logo_caches_and_sets_ext(db, seed_company, tmp_path, monkeypatch):
    monkeypatch.setattr(logos, "LOGOS_DIR", tmp_path / "logos")
    cid = seed_company(website="https://acme.com")

    async def fake_fetch(domain, *, client=None):
        assert domain == "acme.com"
        return PNG, "png"

    monkeypatch.setattr(logos, "fetch_logo", fake_fetch)
    assert _run(logos.refresh_company_logo(db, cid)) is True
    assert (tmp_path / "logos" / f"{cid}.png").read_bytes() == PNG
    row = db.execute("SELECT logo_ext FROM companies WHERE id = ?", (cid,)).fetchone()
    assert row["logo_ext"] == "png"


def test_refresh_company_logo_false_when_no_logo(db, seed_company, tmp_path, monkeypatch):
    monkeypatch.setattr(logos, "LOGOS_DIR", tmp_path / "logos")
    cid = seed_company(website="https://acme.com")
    monkeypatch.setattr(logos, "fetch_logo", lambda *a, **k: _async_none())
    assert _run(logos.refresh_company_logo(db, cid)) is False
    assert db.execute("SELECT logo_ext FROM companies WHERE id = ?", (cid,)).fetchone()["logo_ext"] is None


def test_refresh_company_logo_false_without_domain(db, seed_company, monkeypatch):
    cid = seed_company(website=None, careers_url=None)

    async def boom(*a, **k):
        raise AssertionError("must not fetch without a domain")

    monkeypatch.setattr(logos, "fetch_logo", boom)
    assert _run(logos.refresh_company_logo(db, cid)) is False


async def _async_none():
    return None


# --- /logo endpoint + payload exposure ---


def test_logo_endpoint_serves_cached_file(client, db, seed_company, tmp_path, monkeypatch):
    monkeypatch.setattr(logos, "LOGOS_DIR", tmp_path / "logos")
    (tmp_path / "logos").mkdir()
    cid = seed_company()
    (tmp_path / "logos" / f"{cid}.png").write_bytes(PNG)
    db.execute("UPDATE companies SET logo_ext = 'png' WHERE id = ?", (cid,))
    db.commit()
    r = client.get(f"/api/companies/{cid}/logo")
    assert r.status_code == 200
    assert r.content == PNG
    assert r.headers["content-type"] == "image/png"


def test_logo_endpoint_404_when_no_logo(client, seed_company):
    assert client.get(f"/api/companies/{seed_company()}/logo").status_code == 404


def test_company_payload_has_logo_url(client, db, seed_company):
    cid = seed_company()
    db.execute("UPDATE companies SET logo_ext = 'png' WHERE id = ?", (cid,))
    db.commit()
    listed = next(c for c in client.get("/api/companies").json() if c["id"] == cid)
    assert listed["logo_url"] == f"/api/companies/{cid}/logo"
    bare = client.get(f"/api/companies/{seed_company(name='NoLogo')}").json()
    assert bare["logo_url"] is None


def test_job_payload_has_company_logo(client, db, seed_company, seed_job):
    cid = seed_company()
    db.execute("UPDATE companies SET logo_ext = 'ico' WHERE id = ?", (cid,))
    db.commit()
    jid = seed_job(company_id=cid)
    job = next(j for j in client.get("/api/jobs").json() if j["id"] == jid)
    assert job["company_logo"] == f"/api/companies/{cid}/logo"
    assert "company_logo_ext" not in job  # the raw ext is dropped


def test_contact_payload_has_company_logo(client, db, seed_company, seed_contact):
    cid = seed_company()
    db.execute("UPDATE companies SET logo_ext = 'png' WHERE id = ?", (cid,))
    db.commit()
    ctid = seed_contact(company_id=cid)
    ct = next(c for c in client.get("/api/contacts").json() if c["id"] == ctid)
    assert ct["company_logo"] == f"/api/companies/{cid}/logo"


def test_application_payload_has_company_logo(client, db, seed_company, seed_job, seed_application):
    cid = seed_company()
    db.execute("UPDATE companies SET logo_ext = 'png' WHERE id = ?", (cid,))
    db.commit()
    aid = seed_application(job_id=seed_job(company_id=cid))
    app = next(a for a in client.get("/api/applications").json() if a["id"] == aid)
    assert app["company_logo"] == f"/api/companies/{cid}/logo"


def test_logo_ext_migration_applied(db):
    cols = {r["name"] for r in db.execute("PRAGMA table_info(companies)")}
    assert "logo_ext" in cols


# ---- POST /api/companies/{id}/logo/refresh (the detail-pane ↻) --------------


def test_logo_refresh_endpoint_returns_company(client, db, seed_company, monkeypatch):
    company_id = seed_company(website="https://acme.example")
    calls = []

    async def fake_refresh(conn, cid, *, client=None):
        calls.append(cid)
        return True

    monkeypatch.setattr(logos, "refresh_company_logo", fake_refresh)
    r = client.post(f"/api/companies/{company_id}/logo/refresh")
    assert r.status_code == 200
    assert calls == [company_id]
    assert r.json()["id"] == company_id  # full company payload rides back


def test_logo_refresh_endpoint_miss_is_still_200(client, db, seed_company, monkeypatch):
    """Best-effort contract: a lookup miss returns the company unchanged (the
    monogram stays), never an error."""
    company_id = seed_company()

    async def fake_refresh(conn, cid, *, client=None):
        return False

    monkeypatch.setattr(logos, "refresh_company_logo", fake_refresh)
    r = client.post(f"/api/companies/{company_id}/logo/refresh")
    assert r.status_code == 200
    assert r.json()["logo_url"] is None


def test_logo_refresh_endpoint_unknown_company_404s(client, monkeypatch):
    async def fake_refresh(conn, cid, *, client=None):  # must never be reached
        raise AssertionError("refresh ran for an unknown company")

    monkeypatch.setattr(logos, "refresh_company_logo", fake_refresh)
    assert client.post("/api/companies/999/logo/refresh").status_code == 404


def test_logo_refresh_is_wired_into_the_frontend():
    """The endpoint shipped Phase 0–5 with no caller — the docstring's
    'detail-pane ↻' was aspirational. Pin the wiring so it can't regress
    to dead code."""
    from jshq import paths

    api_js = (paths.FRONTEND_DIR / "js" / "api.js").read_text(encoding="utf-8")
    assert "/logo/refresh" in api_js
    companies_js = (paths.FRONTEND_DIR / "js" / "views" / "companies.js").read_text(encoding="utf-8")
    assert 'data-action="refresh-logo"' in companies_js
    assert "api.refreshCompanyLogo(" in companies_js
