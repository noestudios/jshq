"""ClearCompany adapter.

Modern ClearCompany career sites are client-side shells: the page embeds
careers-content.clearcompany.com/js/v1/career-site.js?siteId=<uuid> and the
widget injects the postings in the browser — which is why an HTML scan of the
careers page sees no jobs. The widget's own data source is a public JSON API
(careers-api.clearcompany.com/v1/{siteId}) whose results carry the full HTML
description inline, so this is a single-phase adapter (no per-job fetches).
The slug is the siteId UUID from the embed.

Pagination: the widget sends ?p=N (1-based), but small sites return every
posting in one unpaginated page and IGNORE ?p (verified against a small
tenant: p=2 re-returns page 0) — so the loop guards on totalCount, empty
pages, and repeated ids, never on the page counter alone.
"""

import re

import httpx

from .. import patterns as p
from ..normalize import AdapterError, NormalizedJob, classify_remote, extract_salary, strip_html
from ._http import get_json

MAX_PAGES = 20  # runaway guard; a small tenant's whole board fits in one page


def _posting_url(item: dict) -> str | None:
    """applyLink points at the application form; its parent path is the public
    posting page (verified 200). Fall back to the applyLink itself."""
    link = item.get("applyLink") or None
    if link and link.rstrip("/").endswith("/apply"):
        return link.rstrip("/").removesuffix("/apply")
    return link


async def fetch(
    client: httpx.AsyncClient, slug: str, title_filter: re.Pattern
) -> list[NormalizedJob]:
    postings: list[dict] = []
    seen: set[str] = set()
    for page in range(1, MAX_PAGES + 1):
        url = p.clearcompany_list_url(slug, page)
        data = await get_json(client, url)
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise AdapterError(f"GET {url}: unexpected response shape (no results list)")
        results = data["results"]
        new = [r for r in results if str(r.get("id")) not in seen]
        if not new:
            break  # empty page, or the API ignored ?p and re-served the same page
        seen.update(str(r.get("id")) for r in new)
        postings.extend(new)
        total = data.get("totalCount")
        if not isinstance(total, int) or len(postings) >= total:
            break

    jobs: list[NormalizedJob] = []
    for item in postings:
        title = (item.get("positionTitle") or "").strip()
        if not title or not title_filter.search(title):
            continue
        location = item.get("location") or None
        locations = item.get("locations") or []
        hint = (
            "remote"
            if any(loc.get("isRemote") for loc in locations if isinstance(loc, dict))
            else None
        )
        description = strip_html(item.get("description"))
        salary_min, salary_max, stated = extract_salary(description)
        jobs.append(
            NormalizedJob(
                external_id=str(item["id"]) if item.get("id") else None,
                title=title,
                url=_posting_url(item),
                location=location,
                remote_type=classify_remote(location, hint),
                salary_min=salary_min,
                salary_max=salary_max,
                salary_stated=stated,
                description_text=description,
            )
        )
    return jobs
