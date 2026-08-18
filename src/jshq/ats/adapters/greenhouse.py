"""Greenhouse boards API adapter.

One GET returns every posting including its (entity-escaped HTML) content,
so there are no per-job detail fetches. Pay ranges, when present, live in
the content text.
"""

import re

import httpx

from .. import patterns as p
from ..normalize import NormalizedJob, classify_remote, extract_salary, strip_html
from ._http import get_json


async def fetch(
    client: httpx.AsyncClient, slug: str, title_filter: re.Pattern
) -> list[NormalizedJob]:
    url = p.API_TEMPLATES[p.GREENHOUSE].format(slug=slug) + "?content=true"
    data = await get_json(client, url)
    jobs: list[NormalizedJob] = []
    for j in data.get("jobs", []):
        title = (j.get("title") or "").strip()
        if not title or not title_filter.search(title):
            continue
        location = (j.get("location") or {}).get("name")
        description = strip_html(j.get("content"))
        salary_min, salary_max, stated = extract_salary(description)
        jobs.append(
            NormalizedJob(
                external_id=str(j["id"]) if j.get("id") is not None else None,
                title=title,
                url=j.get("absolute_url"),
                location=location,
                remote_type=classify_remote(location),
                salary_min=salary_min,
                salary_max=salary_max,
                salary_stated=stated,
                description_text=description,
            )
        )
    return jobs
