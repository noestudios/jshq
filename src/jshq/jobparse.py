"""Best-effort extraction of a job posting from a pasted URL (Add-job prefill).

Primary path: schema.org JobPosting JSON-LD, which most ATS / careers pages emit
for Google for Jobs. Fallback: a Haiku pass over the page's stripped text when no
JSON-LD is present. A single, user-initiated fetch with the honest UA (not a
crawl); LinkedIn is refused per the no-scraping hard rule. The Anthropic client
is injected like the scoring modules, so tests pass a fake and never hit the
live API.
"""

import html
import json
import re
from urllib.parse import urlparse

import httpx

from . import aicfg, apikey
from .ats.detect import TIMEOUT, USER_AGENT

MAX_TOKENS = 512  # the model returns only the small structured fields, not the JD
PAGE_TEXT_LIMIT = 16_000  # chars of stripped page text fed to the LLM fallback
JD_CHAR_LIMIT = 12_000  # cap on the description we hand back (matches the scorer's)

# Returned when the page is reachable but nothing parseable is found — the user
# then fills the modal by hand.
EMPTY = {
    "title": None,
    "location": None,
    "remote_type": None,
    "salary_min": None,
    "salary_max": None,
    "description_text": None,
    "source": "none",
    "detail": None,  # a user-facing reason when nothing could be extracted
}

# schema.org baseSalary unitText → multiplier to annualize the figure.
_SALARY_UNIT_TO_YEAR = {"HOUR": 2080, "DAY": 260, "WEEK": 52, "MONTH": 12, "YEAR": 1}

_LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.I | re.S
)

_LLM_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "location": {"type": "string"},
        "remote_type": {"type": "string", "enum": ["remote", "hybrid", "onsite", "unknown"]},
        "salary_min": {"type": "string"},
        "salary_max": {"type": "string"},
    },
    "required": ["title", "location", "remote_type", "salary_min", "salary_max"],
    "additionalProperties": False,
}


class JobParseError(Exception):
    """A user-facing reason the URL couldn't be parsed (surfaced as a 422)."""


def _strip_html(s: str) -> str:
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", s or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _as_int(v):
    try:
        return int(round(float(v))) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _find_job_posting(node, _depth: int = 0):
    """JSON-LD can be a dict, a list, or wrapped in @graph; @type a str or list."""
    if _depth > 6:
        return None
    if isinstance(node, list):
        for item in node:
            found = _find_job_posting(item, _depth + 1)
            if found:
                return found
    elif isinstance(node, dict):
        t = node.get("@type")
        types = t if isinstance(t, list) else [t]
        if any(isinstance(x, str) and x.lower() == "jobposting" for x in types):
            return node
        if "@graph" in node:
            return _find_job_posting(node["@graph"], _depth + 1)
    return None


def _salary_from_ld(jp):
    bs = jp.get("baseSalary")
    if not isinstance(bs, dict):
        return None, None
    val = bs.get("value")
    if isinstance(val, dict):
        smin, smax = _as_int(val.get("minValue")), _as_int(val.get("maxValue"))
        if smin is None and smax is None:
            smin = smax = _as_int(val.get("value"))
        unit = (val.get("unitText") or bs.get("unitText") or "YEAR").upper()
    else:
        smin = smax = _as_int(val)
        unit = (bs.get("unitText") or "YEAR").upper()
    mult = _SALARY_UNIT_TO_YEAR.get(unit, 1)
    if mult != 1:
        smin = smin * mult if smin is not None else None
        smax = smax * mult if smax is not None else None
    return smin, smax


def _location_from_ld(jp):
    jl = jp.get("jobLocation")
    if isinstance(jl, list):
        jl = jl[0] if jl else None
    if isinstance(jl, dict):
        addr = jl.get("address")
        if isinstance(addr, dict):
            parts = [addr.get("addressLocality"), addr.get("addressRegion")]
            return ", ".join(p for p in parts if p) or None
        if isinstance(addr, str):
            return addr
    return None


def _from_json_ld(html_text: str):
    for m in _LD_RE.finditer(html_text):
        try:
            data = json.loads(m.group(1).strip())
        except (json.JSONDecodeError, ValueError):
            continue
        jp = _find_job_posting(data)
        if not jp:
            continue
        title = jp.get("title")
        desc = _strip_html(jp.get("description")) if jp.get("description") else None
        if not (title or desc):
            continue
        smin, smax = _salary_from_ld(jp)
        return {
            "title": title or None,
            "location": _location_from_ld(jp),
            "remote_type": "remote" if (jp.get("jobLocationType") or "").upper() == "TELECOMMUTE" else None,
            "salary_min": smin,
            "salary_max": smax,
            "description_text": desc,
            "source": "json-ld",
        }
    return None


async def _from_llm(page_text: str, client, model: str | None = None):
    """Haiku extraction over the stripped page text. The model returns only the
    small structured fields (fast); the description is the page text itself, so we
    never pay to regenerate a multi-thousand-char JD. Returns None when the text
    isn't a real posting (no title) or no model is available."""
    if client is None:
        if not apikey.is_configured():
            return None
        from anthropic import AsyncAnthropic  # lazy: app must run without the package

        client = AsyncAnthropic(max_retries=4)
    system = (
        "From the job-posting page text, extract JSON: title; location "
        "('City, ST', or 'Remote' if stated); remote_type "
        "(remote/hybrid/onsite/unknown); salary_min and salary_max (annual USD as "
        "plain number strings, '' if not stated). Use '' for any field not present "
        "— never invent. If the text is NOT a single real job posting (a landing, "
        "job-list, error, or nav/cookie shell page), return '' for title."
    )
    model = model or aicfg.DEFAULTS["jobparse"]
    resp = await client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        **aicfg.thinking_kwargs(model),
        **aicfg.temperature_kwargs(model, 0.0),
        system=system,
        messages=[{"role": "user", "content": page_text[:PAGE_TEXT_LIMIT]}],
        output_config={"format": {"type": "json_schema", "schema": _LLM_SCHEMA}},
    )
    data = json.loads(next(b.text for b in resp.content if b.type == "text"))
    title = (data.get("title") or "").strip()
    if not title:  # not a real posting (shell/list/error page)
        return None
    rt = data.get("remote_type")
    return {
        "title": title,
        "location": data.get("location") or None,
        "remote_type": rt if rt in ("remote", "hybrid", "onsite") else None,
        "salary_min": _as_int(data.get("salary_min")) or None,
        "salary_max": _as_int(data.get("salary_max")) or None,
        "description_text": page_text[:JD_CHAR_LIMIT] or None,
        "source": "llm",
    }


async def parse_job_url(url: str, *, client=None, model: str | None = None) -> dict:
    """Fetch a pasted posting URL and extract its fields. JSON-LD first, then a
    model pass; returns EMPTY when the page is reachable but unparseable. Raises
    JobParseError (→ 422) for a bad/LinkedIn URL or an unreachable page."""
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise JobParseError("That doesn't look like a web URL.")
    if "linkedin.com" in parsed.hostname.lower():
        raise JobParseError(
            "LinkedIn postings can't be auto-pulled. Open the role on the company's "
            "own careers site and paste that URL, or fill the fields in manually."
        )
    try:
        async with httpx.AsyncClient(
            timeout=TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,*/*"},
        ) as c:
            resp = await c.get(url)
    except httpx.HTTPError as exc:
        raise JobParseError(f"Couldn't reach that page ({type(exc).__name__}).") from exc
    if resp.status_code != 200:
        raise JobParseError(f"That page returned HTTP {resp.status_code}.")

    ld = _from_json_ld(resp.text)
    if ld:
        return ld  # JSON-LD needs no key — the structured fields were in the HTML
    # The Haiku pass is the only remaining extractor. If it can't run for lack of
    # a key (no injected client, no env key), say THAT — the JavaScript-render
    # explanation below would otherwise misdiagnose a missing key as a rendering
    # quirk. Mirrors _from_llm's own client-vs-key condition.
    if client is None and not apikey.is_configured():
        return {
            **EMPTY,
            "detail": apikey.MISSING_MESSAGE + " Or paste the job details in below.",
        }
    # _from_llm returns None for a shell/list/error page (no title), so anything
    # it returns is a real posting worth prefilling. The keyed Haiku pass reaches
    # api.anthropic.com: a bad/expired key, a 429/529 outlasting the SDK retries,
    # a dropped connection, or an unexpected response shape all raise here.
    # anthropic's typed exceptions can't be named (it is lazily imported), so
    # catch broadly and degrade to manual paste -- the graceful-degradation
    # invariant covers a key that is present but failing, not only an absent one.
    # Every sibling AI endpoint wraps its model call the same way; this one was
    # the sole omission, and it 500ed instead of returning an actionable message.
    try:
        llm = await _from_llm(_strip_html(resp.text), client, model)
    except Exception as exc:
        return {
            **EMPTY,
            "detail": (
                f"Couldn't reach the extractor to read this page ({type(exc).__name__}). "
                "Copy the description from the page and paste it in below."
            ),
        }
    if llm:
        return llm
    # Reached the page (HTTP 200) but found no posting in it — almost always
    # because the careers site renders the job client-side with JavaScript, so the
    # text isn't in the HTML a plain fetch receives. Say so.
    return {
        **EMPTY,
        "detail": (
            "We loaded the page but the job details weren't in it — this careers "
            "site renders the posting with JavaScript, which a plain fetch can't "
            "read. Copy the description from the page and paste it in below."
        ),
    }
