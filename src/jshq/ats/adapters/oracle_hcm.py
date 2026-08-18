"""Oracle Cloud HCM (Fusion Recruiting / Candidate Experience) adapter.

The recruitingCEJobRequisitions list endpoint is paginated and carries no job
descriptions, so the title filter runs on the list and the per-req detail
endpoint (recruitingCEJobRequisitionDetails) is fetched only for matches — the
SmartRecruiters two-phase shape. The slug is "{pod_host}/{siteNumber}"
(e.g. "exco.fa.us2.oraclecloud.com/CX"): the public REST API lives on the
Oracle pod host, not the company's branded careers domain.
"""

import re

import httpx

from .. import patterns as p
from ..normalize import NormalizedJob, classify_remote, extract_salary, strip_html
from ._http import get_json

PAGE_LIMIT = 200
MAX_PAGES = 80  # safety ceiling (80 * 200 = 16k reqs, above any real board)

_DESC_FIELDS = (
    "ExternalDescriptionStr",
    "ExternalResponsibilitiesStr",
    "ExternalQualificationsStr",
)


def _remote_hint(code: str | None) -> str | None:
    """Map an Oracle WorkplaceTypeCode (ORA_REMOTE / ORA_HYBRID / ORA_ONSITE)."""
    if not code:
        return None
    code = code.upper()
    if "REMOTE" in code:
        return "remote"
    if "HYBRID" in code:
        return "hybrid"
    if "ON" in code:  # ORA_ONSITE / ORA_ON_SITE
        return "onsite"
    return None


async def fetch(
    client: httpx.AsyncClient, slug: str, title_filter: re.Pattern
) -> list[NormalizedJob]:
    matched: list[dict] = []
    offset = 0
    for _ in range(MAX_PAGES):
        data = await get_json(client, p.oracle_list_url(slug, PAGE_LIMIT, offset))
        items = data.get("items") or []
        search = items[0] if items else {}
        rows = search.get("requisitionList") or []
        matched.extend(
            r for r in rows
            if (r.get("Title") or "").strip() and title_filter.search(r["Title"])
        )
        offset += len(rows)
        if not rows or offset >= search.get("TotalJobsCount", 0):
            break

    jobs: list[NormalizedJob] = []
    for row in matched:
        detail = await get_json(client, p.oracle_detail_url(slug, row["Id"]))
        ditems = detail.get("items") or []
        d = ditems[0] if ditems else {}
        description = strip_html(
            "\n\n".join(t for t in (d.get(k) for k in _DESC_FIELDS) if t)
        )
        location = (
            row.get("PrimaryLocation")
            or d.get("PrimaryLocation")
            or row.get("PrimaryLocationCountry")
        )
        code = row.get("WorkplaceTypeCode") or d.get("WorkplaceTypeCode")
        salary_min, salary_max, stated = extract_salary(description)
        jobs.append(
            NormalizedJob(
                external_id=str(row["Id"]),
                title=row["Title"].strip(),
                url=p.oracle_job_url(slug, row["Id"]),
                location=location,
                remote_type=classify_remote(location, _remote_hint(code)),
                salary_min=salary_min,
                salary_max=salary_max,
                salary_stated=stated,
                description_text=description,
            )
        )
    return jobs
