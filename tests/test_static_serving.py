"""Single-process frontend serving (Phase 1): the app serves frontend/ at /
with Cache-Control: no-cache on every static response (CLAUDE.md invariant —
the ES-module graph is un-hashed, so heuristic caching half-updates it)."""


def test_index_at_root(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.headers["cache-control"] == "no-cache"
    assert '<script type="module"' in resp.text


def test_css_asset_no_cache(client):
    resp = client.get("/css/tokens.css")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-cache"


def test_js_asset_no_cache(client):
    resp = client.get("/js/app.js")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-cache"


def test_304_revalidation_keeps_no_cache(client):
    first = client.get("/css/tokens.css")
    etag = first.headers.get("etag")
    assert etag, "StaticFiles should emit an ETag"
    resp = client.get("/css/tokens.css", headers={"If-None-Match": etag})
    assert resp.status_code == 304
    assert resp.headers["cache-control"] == "no-cache"


def test_api_routes_unaffected(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert "no-cache" not in resp.headers.get("cache-control", "")


def test_unknown_path_404s(client):
    assert client.get("/definitely-not-a-file.xyz").status_code == 404
