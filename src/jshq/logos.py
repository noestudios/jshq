"""Fetch + cache a company's logo for the listing/detail avatars.

A best-effort, keyless enrichment: derive the company's registrable domain from
its website (or careers URL), fetch a logo image, and cache it at
``data/logos/{id}.{ext}``. The UI always has a deterministic initials monogram to
fall back on, so a miss here is invisible — there is no API key and nothing is
required for the app to render.

Sources, in order: the site's own apple-touch-icon (crisp, zero third-party),
then DuckDuckGo's icon service (``icons.duckduckgo.com/ip3/{domain}.ico``,
keyless, private-ish), then any declared ``<link rel="icon">``. Mirrors
jobparse.py's honest-UA httpx posture; tests inject a MockTransport-backed client
so nothing hits the network.
"""

import hashlib
import re
from urllib.parse import urljoin, urlparse

import httpx

from . import db, paths
from .ats.detect import TIMEOUT, USER_AGENT

LOGOS_DIR = paths.DATA_DIR / "logos"

MIN_BYTES = 100  # below this it's an error page / empty pixel, not an icon
MAX_BYTES = 2_000_000  # 2 MB cap — an apple-touch-icon is far smaller

# content-type -> stored file extension, for the formats we accept.
_EXT_BY_TYPE = {
    "image/png": "png",
    "image/x-icon": "ico",
    "image/vnd.microsoft.icon": "ico",
    "image/ico": "ico",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/svg+xml": "svg",
    "image/gif": "gif",
}

# Leading subdomain labels stripped to reach the brand domain
# (careers.exampleco.com -> exampleco.com, www.acmeco.com -> acmeco.com).
_STRIP_LABELS = {"careers", "career", "www", "jobs", "job", "apply", "boards"}

# DuckDuckGo's ip3 service 404s for domains it has nothing for (so the status
# check below already rejects those), but it can also serve a generic globe at
# HTTP 200; caching that would paint every miss with the same icon. Reject
# responses whose sha1 matches the known fallback (observed empirically).
_DDG_PLACEHOLDER_SHA1: set[str] = {"980aa215c45dd3b92f40b272234a21f6d850b14a"}

_LINK_RE = re.compile(r"<link\b[^>]*>", re.I)
_REL_RE = re.compile(r'rel=["\']([^"\']*)["\']', re.I)
_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.I)


class LogoError(Exception):
    """A logo couldn't be fetched (best-effort; callers swallow it)."""


def registrable_domain(url_or_host: str | None) -> str | None:
    """Reduce a website / careers URL to its brand domain for a logo lookup.

    Dependency-free: strips a known set of leading subdomain labels (careers./
    www./jobs./apply.) then keeps the last two labels. This is a heuristic, not a
    true Public Suffix List lookup — fine for the simple .com/.org/.team domains
    in use; a multi-part TLD (e.g. co.uk) would need a small exception set.
    """
    if not url_or_host:
        return None
    s = url_or_host.strip()
    if "//" not in s:
        s = "//" + s  # urlparse needs a scheme-ish prefix to read the netloc
    host = (urlparse(s).hostname or "").lower()
    if not host:
        return None
    labels = host.split(".")
    while len(labels) > 2 and labels[0] in _STRIP_LABELS:
        labels = labels[1:]
    return ".".join(labels[-2:]) if len(labels) >= 2 else None


def _ext_for(resp: httpx.Response) -> str | None:
    """The stored extension for an image response, by content-type then magic bytes."""
    ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    ext = _EXT_BY_TYPE.get(ctype)
    if ext:
        return ext
    b = resp.content  # some hosts serve icons as octet-stream — sniff the header
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if b[:4] == b"\x00\x00\x01\x00":
        return "ico"
    if b[:3] == b"\xff\xd8\xff":
        return "jpg"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "webp"
    if b[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return None


def _usable(resp: httpx.Response | None) -> str | None:
    """The file ext if this response is a usable image, else None."""
    if resp is None or resp.status_code != 200:
        return None
    if not (MIN_BYTES <= len(resp.content) <= MAX_BYTES):
        return None
    return _ext_for(resp)


def _links(base_url: str, html_text: str, want: set[str]) -> list[str]:
    """Absolute href URLs of <link> tags whose rel intersects `want`."""
    out = []
    for tag in _LINK_RE.findall(html_text or ""):
        rel = _REL_RE.search(tag)
        href = _HREF_RE.search(tag)
        if rel and href and ({r.lower() for r in rel.group(1).split()} & want):
            out.append(urljoin(base_url, href.group(1)))
    return out


async def _get(client: httpx.AsyncClient, url: str) -> httpx.Response | None:
    try:
        return await client.get(url)
    except httpx.HTTPError:
        return None


def _ddg_is_placeholder(content: bytes) -> bool:
    return hashlib.sha1(content).hexdigest() in _DDG_PLACEHOLDER_SHA1


async def fetch_logo(domain: str, *, client: httpx.AsyncClient | None = None) -> tuple[bytes, str] | None:
    """Return ``(image_bytes, ext)`` for a domain, or None if nothing usable.

    The brand's own apple-touch-icon first (crisp, zero third-party), then
    DuckDuckGo's icon service, then any declared ``<link rel="icon">``. Accepts an
    injected httpx client so tests can mock the transport.
    """
    owns = client is None
    if owns:
        client = httpx.AsyncClient(
            timeout=TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "image/*,*/*"},
        )
    try:
        home = f"https://{domain}/"
        apple: list[str] = []
        icons: list[str] = []
        resp = await _get(client, home)
        if resp is not None and resp.status_code == 200:
            apple = _links(home, resp.text, {"apple-touch-icon", "apple-touch-icon-precomposed"})
            icons = _links(home, resp.text, {"icon", "shortcut icon"})
        apple += [
            urljoin(home, "/apple-touch-icon.png"),
            urljoin(home, "/apple-touch-icon-precomposed.png"),
        ]
        for url in apple:
            r = await _get(client, url)
            ext = _usable(r)
            if ext:
                return r.content, ext
        ddg = await _get(client, f"https://icons.duckduckgo.com/ip3/{domain}.ico")
        ext = _usable(ddg)
        if ext and not _ddg_is_placeholder(ddg.content):
            return ddg.content, ext
        for url in icons:
            r = await _get(client, url)
            ext = _usable(r)
            if ext:
                return r.content, ext
        return None
    finally:
        if owns:
            await client.aclose()


async def refresh_company_logo(conn, company_id: int, *, client: httpx.AsyncClient | None = None) -> bool:
    """Fetch + cache one company's logo. Best-effort: returns True when a logo was
    cached, False otherwise (the UI falls back to a monogram). Never raises."""
    try:
        row = conn.execute(
            "SELECT website, careers_url FROM companies WHERE id = ?", (company_id,)
        ).fetchone()
        if row is None:
            return False
        domain = registrable_domain(row["website"]) or registrable_domain(row["careers_url"])
        if not domain:
            return False
        found = await fetch_logo(domain, client=client)
        if not found:
            return False
        content, ext = found
        LOGOS_DIR.mkdir(parents=True, exist_ok=True)
        # drop any prior cached ext for this id so a format change leaves no stale file
        for old in LOGOS_DIR.glob(f"{company_id}.*"):
            try:
                old.unlink()
            except OSError:
                pass
        (LOGOS_DIR / f"{company_id}.{ext}").write_bytes(content)
        conn.execute("UPDATE companies SET logo_ext = ? WHERE id = ?", (ext, company_id))
        conn.commit()
        return True
    except Exception:
        return False
