"""Ashby posting-api adapter.

One GET returns every listed posting with plain-text description and,
with includeCompensation=true, a scrapeable salary summary string.
"""

import re

import httpx

from .. import patterns as p
from ..normalize import NormalizedJob, classify_remote, extract_salary, strip_html
from ._http import get_json

_WORKPLACE_HINTS = {"Remote": "remote", "Hybrid": "hybrid", "OnSite": "onsite"}


async def fetch(
    client: httpx.AsyncClient, slug: str, title_filter: re.Pattern
) -> list[NormalizedJob]:
    url = p.API_TEMPLATES[p.ASHBY].format(slug=slug) + "?includeCompensation=true"
    data = await get_json(client, url)
    jobs: list[NormalizedJob] = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        title = (j.get("title") or "").strip()
        if not title or not title_filter.search(title):
            continue
        location = j.get("location")
        hint = "remote" if j.get("isRemote") else _WORKPLACE_HINTS.get(j.get("workplaceType"))
        description = j.get("descriptionPlain") or strip_html(j.get("descriptionHtml"))

        comp = j.get("compensation") or {}
        comp_text = comp.get("scrapeableCompensationSalarySummary") or comp.get(
            "compensationTierSummary"
        )
        salary_min, salary_max, stated = extract_salary(comp_text)
        if not stated:
            salary_min, salary_max, stated = extract_salary(description)

        jobs.append(
            NormalizedJob(
                external_id=j.get("id"),
                title=title,
                url=j.get("jobUrl"),
                location=location,
                remote_type=classify_remote(location, hint),
                salary_min=salary_min,
                salary_max=salary_max,
                salary_stated=stated,
                description_text=description,
            )
        )
    return jobs
