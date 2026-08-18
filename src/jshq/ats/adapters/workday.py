"""Workday CXS adapter.

Large Workday tenants post thousands of roles,
so instead of paginating whole boards this runs the board's own search once
per keyword and unions the results by externalPath (~20 list requests per
tenant instead of ~150). The search terms are a fetch optimization only — the
configurable title filter of record still runs on everything returned.
Details are fetched only for title matches.
"""

import re

import httpx

from .. import patterns as p
from ..normalize import AdapterError, NormalizedJob, classify_remote, extract_salary, strip_html
from ._http import get_json, post_json

PAGE_LIMIT = 20
# Per-term safety ceiling (~1000 postings/term), same politeness posture as the
# other paginating adapters' caps. total normally terminates the loop; this
# bounds the damage of a pathologically broad term on a huge tenant.
MAX_PAGES = 50


async def fetch(
    client: httpx.AsyncClient, slug: str, title_filter: re.Pattern, *, config=None
) -> list[NormalizedJob]:
    jobs_url = p.workday_cxs_url(slug)
    cxs_root = jobs_url.rsplit("/jobs", 1)[0]  # detail = cxs_root + externalPath

    # Workday's API requires a searchText, so the term list is a hard ceiling
    # on what can be fetched at all. refresh._fetch_config derives it from the
    # title_keywords include list (or the workday_search_terms override); with
    # neither set there is nothing to search FOR. That must be an ERROR, not a
    # successful empty board: an "ok" empty fetch feeds the decay counter, so
    # previously ingested jobs would be closed as "no longer listed" within
    # two refreshes while still live on the board — and the status would read
    # healthy. AdapterError takes the skip path (no decay) and puts the reason
    # in ats_last_status where the user can see it.
    terms = (config or {}).get("workday_search_terms") or []
    if not terms:
        raise AdapterError(
            "no search terms — Workday can only be searched, not enumerated; "
            "add a title keyword or sourcing rule (Settings → Sourcing) so "
            "this board has something to search for"
        )

    seen: dict[str, dict] = {}
    for term in terms:
        offset = 0
        for _page in range(MAX_PAGES):
            data = await post_json(
                client,
                jobs_url,
                {"appliedFacets": {}, "limit": PAGE_LIMIT, "offset": offset, "searchText": term},
            )
            postings = data.get("jobPostings", [])
            for jp in postings:
                path = jp.get("externalPath")
                if path and path not in seen:
                    seen[path] = jp
            offset += len(postings)
            if not postings or offset >= data.get("total", 0):
                break

    jobs: list[NormalizedJob] = []
    for path, jp in seen.items():
        title = (jp.get("title") or "").strip()
        if not title or not title_filter.search(title):
            continue
        detail = await get_json(client, cxs_root + path)
        info = detail.get("jobPostingInfo") or {}
        location = info.get("location") or jp.get("locationsText")
        remote_text = (info.get("remoteType") or "").lower()
        hint = "remote" if "remote" in remote_text else None
        description = strip_html(info.get("jobDescription"))
        salary_min, salary_max, stated = extract_salary(description)
        bullet = (jp.get("bulletFields") or [None])[0]
        jobs.append(
            NormalizedJob(
                external_id=info.get("jobReqId") or bullet,
                title=title,
                url=info.get("externalUrl") or p.workday_board_url(slug) + path,
                location=location,
                remote_type=classify_remote(location, hint),
                salary_min=salary_min,
                salary_max=salary_max,
                salary_stated=stated,
                description_text=description,
            )
        )
    return jobs
