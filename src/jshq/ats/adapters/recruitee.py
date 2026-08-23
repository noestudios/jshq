"""Recruitee careers-site API adapter.

One GET returns every published offer with its HTML description inline (plus
a separate `requirements` HTML block — both feed the JD text), so there are
no per-job detail fetches. `remote`/`hybrid` are structured booleans. The
structured `salary` object was all-null on the live tenant the shape was
recorded from (bunq, 2026-08-22) and its filled value/period semantics are
unrecorded, so pay comes from the text like the other adapters.
"""

import re

import httpx

from .. import patterns as p
from ..normalize import NormalizedJob, classify_remote, extract_salary, strip_html
from ._http import get_json


async def fetch(
    client: httpx.AsyncClient, slug: str, title_filter: re.Pattern
) -> list[NormalizedJob]:
    url = p.API_TEMPLATES[p.RECRUITEE].format(slug=slug)
    data = await get_json(client, url)
    offers = data.get("offers", []) if isinstance(data, dict) else []
    jobs: list[NormalizedJob] = []
    for o in offers:
        title = (o.get("title") or "").strip()
        if not title or not title_filter.search(title):
            continue
        location = o.get("location") or None
        hint = "remote" if o.get("remote") else "hybrid" if o.get("hybrid") else None
        parts = [strip_html(o.get("description")), strip_html(o.get("requirements"))]
        description = "\n".join(s for s in parts if s) or None
        salary_min, salary_max, stated = extract_salary(description)
        jobs.append(
            NormalizedJob(
                external_id=str(o["id"]) if o.get("id") is not None else None,
                title=title,
                url=o.get("careers_url"),
                location=location,
                remote_type=classify_remote(location, hint),
                salary_min=salary_min,
                salary_max=salary_max,
                salary_stated=stated,
                description_text=description,
            )
        )
    return jobs
