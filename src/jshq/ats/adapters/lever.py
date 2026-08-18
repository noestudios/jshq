"""Lever postings API adapter.

One public GET returns every posting with its content inline — no per-job
detail fetches (added 2026-08 when the first tracked company adopted Lever;
the detector knew jobs.lever.co URLs long before any tracked company used
one).
descriptionPlain already includes the opening; the bulleted sections live in
`lists` (entity-escaped HTML fragments) and the closing text in
additionalPlain, so all three are joined for the JD. salaryRange, when
present with a yearly interval, beats regex extraction; postings without it
fall back to extract_salary over the joined text. workplaceType is Lever's
own remote/hybrid/onsite enum and feeds classify_remote as the structured
hint ("unspecified" defers to the location string).
"""

import re

import httpx

from .. import patterns as p
from ..normalize import NormalizedJob, classify_remote, extract_salary, strip_html
from ._http import get_json


def _description(j: dict) -> str:
    parts = [j.get("descriptionPlain") or ""]
    for section in j.get("lists") or []:
        header = (section.get("text") or "").strip()
        body = strip_html(section.get("content")) or ""
        parts.append(f"{header}\n{body}" if header else body)
    parts.append(j.get("additionalPlain") or "")
    return "\n\n".join(s.strip() for s in parts if s and s.strip())


def _salary(j: dict, description: str) -> tuple[int | None, int | None, bool]:
    rng = j.get("salaryRange") or {}
    lo, hi = rng.get("min"), rng.get("max")
    yearly = "year" in (rng.get("interval") or "")
    if yearly and isinstance(lo, int) and isinstance(hi, int):
        return lo, hi, True
    return extract_salary(description)


async def fetch(
    client: httpx.AsyncClient, slug: str, title_filter: re.Pattern
) -> list[NormalizedJob]:
    url = p.API_TEMPLATES[p.LEVER].format(slug=slug)
    data = await get_json(client, url)
    postings = data if isinstance(data, list) else []
    jobs: list[NormalizedJob] = []
    for j in postings:
        title = (j.get("text") or "").strip()
        if not title or not title_filter.search(title):
            continue
        location = (j.get("categories") or {}).get("location")
        wt = (j.get("workplaceType") or "").lower()
        hint = wt if wt in ("remote", "hybrid", "onsite") else None
        description = _description(j)
        salary_min, salary_max, stated = _salary(j, description)
        jobs.append(
            NormalizedJob(
                external_id=str(j["id"]) if j.get("id") else None,
                title=title,
                url=j.get("hostedUrl"),
                location=location,
                remote_type=classify_remote(location, hint),
                salary_min=salary_min,
                salary_max=salary_max,
                salary_stated=stated,
                description_text=description or None,
            )
        )
    return jobs
