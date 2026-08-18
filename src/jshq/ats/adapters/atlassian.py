"""Atlassian careers adapter.

Atlassian's live board is the bespoke JSON feed behind www.atlassian.com's
careers pages: /endpoint/careers/listings returns every posting in ONE
unpaginated list with the full HTML description inline (overview /
responsibilities / qualifications), so this is a single-call adapter. Apply
links go to the classic careers-americas.icims.com portal (a dead SPA — never
fetch it); the canonical public posting page is
/company/careers/details/{id}.

CAUTION that motivated this adapter: Atlassian ALSO still serves a Jibe
Candidate Gateway at join.atlassian.com/api/jobs which the generic icims
adapter happily speaks — but that feed is an abandoned snapshot (nothing
posted or updated after 2025-11, id-space disjoint from the live board).
The staleness guard below exists so that if THIS
feed is ever abandoned the same way, the company shows a failing banner
instead of silently serving ghost listings.

The feed repeats some postings (dozens of duplicate ids observed in one
pull), so rows are deduped by id.
"""

import re
from datetime import date, datetime

import httpx

from .. import patterns as p
from ..normalize import AdapterError, NormalizedJob, classify_remote, extract_salary, strip_html
from ._http import get_json

# Newest portalJobPost.updatedDate older than this -> the feed is a corpse,
# not a quiet board. Atlassian posts continuously; 90 days of total silence
# across ~200 listings can only mean the endpoint moved.
MAX_FEED_AGE_DAYS = 90

_UPDATED_FMT = "%Y-%m-%d %I:%M %p"  # "2026-07-20 10:05 PM" (no timezone)

_DESC_FIELDS = ("overview", "responsibilities", "qualifications")


def _clean_location(raw: str) -> str:
    """'San Francisco - United States -   San Francisco, California 94104 …'
    -> 'San Francisco, United States' (first two ' - ' segments, deduped)."""
    parts = [s.strip() for s in raw.split(" - ") if s.strip()]
    return ", ".join(dict.fromkeys(parts[:2]))


def _newest_update(listings: list[dict]) -> date | None:
    """Latest parseable portalJobPost.updatedDate, None if the format drifted
    (then the staleness guard stands down rather than false-failing)."""
    newest = None
    for item in listings:
        portal = item.get("portalJobPost")
        stamp = portal.get("updatedDate") if isinstance(portal, dict) else None
        if not isinstance(stamp, str):
            continue
        try:
            d = datetime.strptime(stamp.strip(), _UPDATED_FMT).date()
        except ValueError:
            continue
        if newest is None or d > newest:
            newest = d
    return newest


async def fetch(
    client: httpx.AsyncClient, slug: str, title_filter: re.Pattern
) -> list[NormalizedJob]:
    url = p.atlassian_list_url(slug)
    data = await get_json(client, url)
    # An empty list is a shape failure here, not a quiet board — Atlassian
    # always has openings; zero rows means the endpoint moved.
    if not isinstance(data, list) or not data or not isinstance(data[0], dict) or "title" not in data[0]:
        raise AdapterError(f"GET {url}: unexpected response shape (no listings)")

    newest = _newest_update(data)
    if newest is not None and (date.today() - newest).days > MAX_FEED_AGE_DAYS:
        raise AdapterError(
            f"GET {url}: feed looks abandoned — newest posting update {newest.isoformat()}"
        )

    jobs: list[NormalizedJob] = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        ext = str(item["id"]) if item.get("id") is not None else None
        if ext:
            if ext in seen:
                continue
            seen.add(ext)
        title = (item.get("title") or "").strip()
        if not title or not title_filter.search(title):
            continue
        locs = [str(loc) for loc in item.get("locations") or [] if str(loc).strip()]
        hint = "remote" if any("remote" in loc.lower() for loc in locs) else None
        location = next(
            (_clean_location(loc) for loc in locs if "remote" not in loc.lower()), None
        ) or ("Remote" if hint else None)
        description = strip_html(
            " ".join(item.get(f) or "" for f in _DESC_FIELDS)
        )
        # The comp range usually sits in the qualifications HTML; the separate
        # `compensation` field, when present, is often boilerplate without
        # numbers — try it first, fall back to mining the full description.
        salary_min = salary_max = None
        stated = False
        comp = item.get("compensation")
        if isinstance(comp, str) and comp.strip():
            salary_min, salary_max, stated = extract_salary(strip_html(comp))
        if salary_min is None:
            salary_min, salary_max, stated = extract_salary(description)
        jobs.append(
            NormalizedJob(
                external_id=ext,
                title=title,
                url=p.atlassian_job_url(ext) if ext else None,
                location=location,
                remote_type=classify_remote(location, hint),
                salary_min=salary_min,
                salary_max=salary_max,
                salary_stated=stated,
                description_text=description,
            )
        )
    return jobs
