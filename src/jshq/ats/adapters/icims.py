"""iCIMS / Jibe Candidate Gateway adapter.

iCIMS career sites built on Jibe (iCIMS acquired Jibe in 2019) expose a public,
unauthenticated JSON job feed at {careers_host}/api/jobs. The list is paginated
(limit caps at 100 — a larger limit returns HTTP 422) and embeds each job's full
HTML description inline, so unlike SmartRecruiters/Oracle this is a single-call
adapter: the title filter runs on the list and the same rows build the jobs, with
no per-job detail fetch. The slug is "{careers_host}/{cid}"
(e.g. "careers.exampleco.com/exampleco"): the feed lives on the company's
branded careers domain, not on *.icims.com.

Staleness guard: a Jibe gateway is a company-branded frontend that can outlive
the ATS behind it — one tracked gateway kept serving its final snapshot long
after the company migrated off, and this adapter briefly ingested ghost jobs
from it. A feed whose newest posted/update date is months old is a corpse, not
a quiet board — fail loudly so the company shows a failing banner instead of
ghost listings. An empty board (some tenants' normal state) or a feed without
dates stands the guard down.
"""

import re
from datetime import date, datetime

import httpx

from .. import patterns as p
from ..normalize import AdapterError, NormalizedJob, classify_remote, extract_salary, strip_html
from ._http import get_json

PAGE_LIMIT = 100  # the API's hard page cap; limit > ~100 returns HTTP 422
MAX_PAGES = 60  # safety ceiling (60 * 100 = 6k jobs, above any real board)

# Newest posted/update date older than this -> abandoned gateway, not a quiet
# board (same guard as the atlassian adapter, born from the same incident).
MAX_FEED_AGE_DAYS = 90

_DATE_FIELDS = ("update_date", "posted_date", "create_date")
_DATE_FMT = "%Y-%m-%dT%H:%M:%S%z"  # "2025-08-15T16:07:35+0000"

_DESC_FIELDS = ("description", "responsibilities", "qualifications")


def _row_date(d: dict) -> date | None:
    """Latest parseable date on a row, None if Jibe reshaped the stamps
    (then the guard stands down rather than false-failing)."""
    newest = None
    for field in _DATE_FIELDS:
        stamp = d.get(field)
        if not isinstance(stamp, str):
            continue
        try:
            parsed = datetime.strptime(stamp.strip(), _DATE_FMT).date()
        except ValueError:
            continue
        if newest is None or parsed > newest:
            newest = parsed
    return newest


def _first(value) -> str:
    """A Jibe tagN field is a list (e.g. ['Remote']) or, rarely, a bare string."""
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value) if value else ""


def _remote_hint(d: dict) -> str | None:
    """Jibe location_type + customer-configured tags -> remote hint, else None.

    location_type ANY = location-flexible (remote-eligible); LAT_LNG = a fixed
    site (return None so classify_remote falls back to the location string).
    tags2/tags6 are customer-configured (some tenants populate them, others
    leave them null), so they only ever refine a None hint.
    """
    if (d.get("location_type") or "").upper() == "ANY":
        return "remote"
    if _first(d.get("tags2")).strip().lower() == "yes":
        return "remote"
    if "remote" in _first(d.get("tags6")).lower():
        return "remote"
    return None


def _salary_text(d: dict) -> str | None:
    """The formatted comp range lives in tags8 (the salary_*_value numerics are
    0/unpopulated); fall back to None so the caller mines the description."""
    tags8 = d.get("tags8")
    if isinstance(tags8, list):
        return " ".join(str(t) for t in tags8) or None
    if isinstance(tags8, str):
        return tags8 or None
    return None


async def fetch(
    client: httpx.AsyncClient, slug: str, title_filter: re.Pattern
) -> list[NormalizedJob]:
    jobs: list[NormalizedJob] = []
    seen = 0
    newest_update: date | None = None
    for page in range(1, MAX_PAGES + 1):
        data = await get_json(client, p.icims_list_url(slug, page, PAGE_LIMIT))
        rows = [j.get("data") or {} for j in (data.get("jobs") or [])]
        for d in rows:
            row_date = _row_date(d)
            if row_date and (newest_update is None or row_date > newest_update):
                newest_update = row_date
            title = (d.get("title") or "").strip()
            if not (title and title_filter.search(title)):
                continue
            location = d.get("full_location") or d.get("short_location")
            description = strip_html(
                "\n\n".join(t for t in (d.get(k) for k in _DESC_FIELDS) if t)
            )
            salary_min, salary_max, stated = extract_salary(
                _salary_text(d) or description
            )
            jobs.append(
                NormalizedJob(
                    external_id=str(d.get("req_id") or d.get("slug") or "") or None,
                    title=title,
                    url=(d.get("meta_data") or {}).get("canonical_url"),
                    location=location,
                    remote_type=classify_remote(location, _remote_hint(d)),
                    salary_min=salary_min,
                    salary_max=salary_max,
                    salary_stated=stated,
                    description_text=description,
                )
            )
        seen += len(rows)
        if not rows or seen >= data.get("totalCount", 0):
            break
    if newest_update is not None and (date.today() - newest_update).days > MAX_FEED_AGE_DAYS:
        raise AdapterError(
            f"{slug}: feed looks abandoned — newest posting update {newest_update.isoformat()}"
        )
    return jobs
