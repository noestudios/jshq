"""Apple Jobs (jobs.apple.com) adapter.

Apple retired its public search API; the careers site is a React Router app
whose server render embeds the full search/detail state in a hydration blob
(patterns.apple_hydration_data). List pages carry titles but only a short
summary, so the title filter runs on the list and the detail page is fetched
only for matches — the Oracle/SmartRecruiters two-phase shape. Detail
hydration has the full JD, qualifications, and the "Pay & Benefits" posting
footer that carries the salary range on regular reqs.

type == "PIPE" rows are talent-pipeline posts ("submitting your resume ...
expressing interest ... in the future"), not openings — skipped at ingestion.
"""

import re

import httpx

from .. import patterns as p
from ..normalize import AdapterError, NormalizedJob, classify_remote, extract_salary, strip_html
from ._http import get_text

PAGE_SIZE = 20  # fixed by the site; informational only, paging stops on totalRecords
MAX_PAGES = 25  # safety ceiling (25 * 20 = 500 reqs, above any filtered scope)

_DESC_FIELDS = ("description", "minimumQualifications", "preferredQualifications")


def _search_state(html: str, url: str) -> dict:
    data = p.apple_hydration_data(html)
    search = ((data or {}).get("loaderData") or {}).get("search") or {}
    if not isinstance(search.get("searchResults"), list):
        raise AdapterError(f"GET {url}: no hydration search state")
    return search


def _location(record: dict) -> str | None:
    locs = record.get("locations") or []
    first = (locs[0] or {}) if locs else {}
    parts = [first.get("name") or "", first.get("stateProvince") or ""]
    return ", ".join(s for s in parts if s) or None


def _footer_text(detail: dict) -> str | None:
    parts = [
        block["content"]
        for footer in detail.get("postingFooters") or []
        for block in (footer.get("localizations") or {}).get("en_US") or []
        if block.get("content")
    ]
    return strip_html("\n".join(parts)) if parts else None


async def fetch(
    client: httpx.AsyncClient, slug: str, title_filter: re.Pattern
) -> list[NormalizedJob]:
    matched: list[dict] = []
    seen: set[str] = set()
    for page in range(1, MAX_PAGES + 1):
        url = p.apple_list_url(slug, page)
        search = _search_state(await get_text(client, url), url)
        rows = [r for r in search["searchResults"] if r.get("id") and r["id"] not in seen]
        if not rows:  # empty or repeated page
            break
        seen.update(r["id"] for r in rows)
        matched.extend(
            r for r in rows
            if r.get("type") != "PIPE"
            and (r.get("postingTitle") or "").strip()
            and title_filter.search(r["postingTitle"])
        )
        total = search.get("totalRecords")
        if isinstance(total, int) and len(seen) >= total:
            break

    jobs: list[NormalizedJob] = []
    for row in matched:
        url = p.apple_detail_url(row["id"], row.get("transformedPostingTitle") or "")
        data = p.apple_hydration_data(await get_text(client, url)) or {}
        d = ((data.get("loaderData") or {}).get("jobDetails") or {}).get("jobsData") or {}
        if not d:
            raise AdapterError(f"GET {url}: no hydration job detail")
        description = strip_html(
            "\n\n".join(t for t in (d.get(k) for k in _DESC_FIELDS) if t)
        )
        salary_min, salary_max, stated = extract_salary(
            "\n".join(t for t in (description, _footer_text(d)) if t)
        )
        location = _location(row) or _location(d)
        hint = "remote" if (row.get("homeOffice") or d.get("homeOffice")) else None
        jobs.append(
            NormalizedJob(
                external_id=str(row["id"]),
                title=row["postingTitle"].strip(),
                url=url,
                location=location,
                remote_type=classify_remote(location, hint),
                salary_min=salary_min,
                salary_max=salary_max,
                salary_stated=stated,
                description_text=description,
            )
        )
    return jobs
