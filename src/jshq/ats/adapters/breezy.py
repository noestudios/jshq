"""Breezy HR adapter.

Breezy publishes a public JSON list of postings at {slug}.breezy.hr/json, but
the list carries no job description — each per-posting page ({slug}.breezy.hr/p/
{friendly_id}) embeds a schema.org JobPosting JSON-LD block with the full
description. So this is a two-phase adapter (like SmartRecruiters/Oracle): the
title filter runs on the list, and the JSON-LD description is fetched only for
matches. Title, location, and remote flag come from the list item directly.
"""

import json
import re

import httpx

from .. import patterns as p
from ..normalize import NormalizedJob, classify_remote, extract_salary, strip_html
from ._http import get_json, get_text

_LD_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.I | re.S
)


def _find_job_posting(node):
    """schema.org JSON-LD may be a dict, a list, or wrapped in @graph; @type a
    str or list. Return the first JobPosting node found, else None."""
    if isinstance(node, list):
        for item in node:
            found = _find_job_posting(item)
            if found:
                return found
        return None
    if isinstance(node, dict):
        t = node.get("@type")
        types = t if isinstance(t, list) else [t]
        if "JobPosting" in types:
            return node
        if "@graph" in node:
            return _find_job_posting(node["@graph"])
    return None


def _description_from_page(html_text: str) -> str | None:
    for m in _LD_RE.finditer(html_text):
        try:
            data = json.loads(m.group(1).strip())
        except (json.JSONDecodeError, ValueError):
            continue
        jp = _find_job_posting(data)
        if jp and jp.get("description"):
            return strip_html(jp["description"])
    return None


async def fetch(
    client: httpx.AsyncClient, slug: str, title_filter: re.Pattern
) -> list[NormalizedJob]:
    listing = await get_json(client, p.breezy_list_url(slug))
    postings = listing if isinstance(listing, list) else []
    matched = [
        j for j in postings
        if (j.get("name") or "").strip() and title_filter.search(j["name"])
    ]

    jobs: list[NormalizedJob] = []
    for posting in matched:
        fid = posting.get("friendly_id") or posting.get("id")
        url = posting.get("url") or (p.breezy_job_url(slug, fid) if fid else None)
        description = _description_from_page(await get_text(client, url)) if url else None
        loc = posting.get("location") or {}
        location = loc.get("name") or None
        hint = "remote" if loc.get("is_remote") else None
        salary_min, salary_max, stated = extract_salary(description)
        jobs.append(
            NormalizedJob(
                external_id=str(posting["id"]) if posting.get("id") else None,
                title=posting["name"].strip(),
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
