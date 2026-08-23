"""Rippling ATS board API adapter.

The public board API lists jobs as a bare array (uuid/name/url/department/
workLocation) with no descriptions, and the per-job detail endpoint adds a
`description` dict of HTML sections ({company, role}) plus `workLocations`
and an `unlistedFromSearch` flag. So this is a two-phase adapter (Breezy
pattern): the title filter runs on the list and details are fetched only for
matches; detail rows flagged unlisted are skipped. `payRangeDetails` was
empty on the live tenant the shape was recorded from (dlb-associates,
2026-08-22) and its entry shape is unrecorded, so pay comes from the text.
"""

import re

import httpx

from .. import patterns as p
from ..normalize import NormalizedJob, classify_remote, extract_salary, strip_html
from ._http import get_json


def _description_text(detail: dict) -> str | None:
    """The detail `description` is a dict of HTML sections; join the known
    ones in page order (company blurb, then role)."""
    desc = detail.get("description")
    if not isinstance(desc, dict):
        return strip_html(desc) if isinstance(desc, str) else None
    parts = [strip_html(desc.get("company")), strip_html(desc.get("role"))]
    return "\n".join(s for s in parts if s) or None


def _location(detail: dict, listing_item: dict) -> str | None:
    locs = detail.get("workLocations")
    if isinstance(locs, list) and locs:
        return "; ".join(s for s in locs if isinstance(s, str)) or None
    wl = listing_item.get("workLocation")
    return wl.get("label") if isinstance(wl, dict) else None


async def fetch(
    client: httpx.AsyncClient, slug: str, title_filter: re.Pattern
) -> list[NormalizedJob]:
    listing = await get_json(client, p.rippling_list_url(slug))
    postings = listing if isinstance(listing, list) else []
    matched = [
        j for j in postings
        if (j.get("name") or "").strip() and title_filter.search(j["name"])
    ]

    jobs: list[NormalizedJob] = []
    for item in matched:
        uuid = item.get("uuid")
        detail = await get_json(client, p.rippling_detail_url(slug, uuid)) if uuid else {}
        if not isinstance(detail, dict):
            detail = {}
        if detail.get("unlistedFromSearch"):
            continue
        description = _description_text(detail)
        location = _location(detail, item)
        salary_min, salary_max, stated = extract_salary(description)
        jobs.append(
            NormalizedJob(
                external_id=uuid,
                title=item["name"].strip(),
                url=item.get("url"),
                location=location,
                remote_type=classify_remote(location),
                salary_min=salary_min,
                salary_max=salary_max,
                salary_stated=stated,
                description_text=description,
            )
        )
    return jobs
