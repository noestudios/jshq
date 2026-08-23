"""Workable widget API adapter.

One GET to the widget accounts endpoint (?details=true) returns the account
name plus every published job with its HTML description, structured
locations, and a `telecommuting` remote flag — no per-job detail fetches.
The `shortcode` is the stable per-posting identifier (it is the job URL's
path segment). Top-level city/state/country describe the primary location;
the structured `locations[]` list mirrors them.
"""

import re

import httpx

from .. import patterns as p
from ..normalize import NormalizedJob, classify_remote, extract_salary, strip_html
from ._http import get_json


async def fetch(
    client: httpx.AsyncClient, slug: str, title_filter: re.Pattern
) -> list[NormalizedJob]:
    url = p.API_TEMPLATES[p.WORKABLE].format(slug=slug)
    data = await get_json(client, url)
    postings = data.get("jobs", []) if isinstance(data, dict) else []
    jobs: list[NormalizedJob] = []
    for j in postings:
        title = (j.get("title") or "").strip()
        if not title or not title_filter.search(title):
            continue
        location = ", ".join(
            s for s in (j.get("city"), j.get("state"), j.get("country")) if s
        ) or None
        hint = "remote" if j.get("telecommuting") else None
        description = strip_html(j.get("description"))
        salary_min, salary_max, stated = extract_salary(description)
        jobs.append(
            NormalizedJob(
                external_id=j.get("shortcode"),
                title=title,
                url=j.get("url"),
                location=location,
                remote_type=classify_remote(location, hint),
                salary_min=salary_min,
                salary_max=salary_max,
                salary_stated=stated,
                description_text=description,
            )
        )
    return jobs
