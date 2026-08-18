"""SmartRecruiters postings API adapter.

The list endpoint is paginated and has no descriptions, so the title filter
runs on the list and per-posting details are fetched only for matches.
"""

import re

import httpx

from .. import patterns as p
from ..normalize import NormalizedJob, classify_remote, extract_salary, strip_html
from ._http import get_json

PAGE_LIMIT = 100
_SECTION_ORDER = ("companyDescription", "jobDescription", "qualifications", "additionalInformation")


def _location_text(loc: dict | None) -> str | None:
    if not loc:
        return None
    if loc.get("fullLocation"):
        return loc["fullLocation"]
    parts = [loc.get("city"), loc.get("region"), loc.get("country")]
    return ", ".join(x for x in parts if x) or None


def _remote_hint(loc: dict | None) -> str | None:
    if not loc:
        return None
    if loc.get("remote"):
        return "remote"
    if loc.get("hybrid"):
        return "hybrid"
    return None


async def fetch(
    client: httpx.AsyncClient, slug: str, title_filter: re.Pattern
) -> list[NormalizedJob]:
    base = p.API_TEMPLATES[p.SMARTRECRUITERS].format(slug=slug)
    matched: list[dict] = []
    offset = 0
    while True:
        data = await get_json(client, f"{base}?limit={PAGE_LIMIT}&offset={offset}")
        postings = data.get("content", [])
        matched.extend(
            j for j in postings if (j.get("name") or "").strip() and title_filter.search(j["name"])
        )
        offset += len(postings)
        if not postings or offset >= data.get("totalFound", 0):
            break

    jobs: list[NormalizedJob] = []
    for posting in matched:
        detail = await get_json(client, f"{base}/{posting['id']}")
        sections = (detail.get("jobAd") or {}).get("sections", {})
        texts = [
            strip_html((sections.get(k) or {}).get("text")) for k in _SECTION_ORDER
        ]
        description = "\n\n".join(t for t in texts if t) or None
        location = posting.get("location") or detail.get("location")
        salary_min, salary_max, stated = extract_salary(description)
        jobs.append(
            NormalizedJob(
                external_id=str(posting["id"]),
                title=posting["name"].strip(),
                url=detail.get("postingUrl") or detail.get("applyUrl"),
                location=_location_text(location),
                remote_type=classify_remote(_location_text(location), _remote_hint(location)),
                salary_min=salary_min,
                salary_max=salary_max,
                salary_stated=stated,
                description_text=description,
            )
        )
    return jobs
