"""ATS detection for tracked companies.

Standalone run:  python -m jshq.ats.detect [--dry-run] [--company-id N]

Per company: fetch the careers/website URL (robots.txt-respecting, honest
User-Agent), scan the final URL + HTML for ATS signatures, then verify each
candidate against the ATS's public API. If the page yields nothing, probe
name-derived slugs against the slug-addressable APIs (never Workday — its
tenant/site must come from page evidence). Confirmed results are written to
companies.ats_type/ats_slug (+ careers_url backfill); full results land in
data/ats_detect_results.json for inspection.

Politeness: robots.txt honored for page fetches, one pass,
capped concurrency, 10s timeouts, failures logged and never fatal.
"""

import argparse
import asyncio
import json
import re
import sqlite3
import sys
from urllib import robotparser
from urllib.parse import urlsplit

import httpx

from .. import db, paths
from . import patterns as p

USER_AGENT = "JobSearchHQ/0.1 (personal job tracker; single user; twice-daily max)"
TIMEOUT = httpx.Timeout(10.0)
CONCURRENCY = 5
MAX_PROBE_SLUGS = 4

RESULTS_PATH = paths.DATA_DIR / "ats_detect_results.json"


def _with_scheme(url: str | None) -> str | None:
    """A user-typed URL may lack a scheme ("discord.com"), which httpx rejects
    outright — so a scheme-less website silently failed every detection path
    (add-time, URL-edit re-probe, "Check again", the wizard's careers preview).
    Assume https, matching the frontend's companyLink normalization. Anything
    that already carries a scheme is left untouched."""
    if not url:
        return url
    return url if re.match(r"^[a-z][a-z0-9+.-]*:", url, re.I) else f"https://{url}"


async def _robots_allows(client: httpx.AsyncClient, url: str) -> bool:
    """True unless the host's robots.txt explicitly disallows us."""
    parts = urlsplit(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    try:
        r = await client.get(robots_url)
        if r.status_code >= 400:
            return True  # no robots.txt -> no restrictions
        rp = robotparser.RobotFileParser()
        rp.parse(r.text.splitlines())
        return rp.can_fetch(USER_AGENT, url)
    except (httpx.HTTPError, ValueError):
        # ValueError covers a malformed host: idna.IDNAError (a UnicodeError,
        # hence ValueError) and httpx.InvalidURL raise during host encoding,
        # BEFORE any network I/O, and are not httpx.HTTPError. Treat as allowed.
        return True


async def verify(client: httpx.AsyncClient, ats_type: str, slug: str) -> dict | None:
    """Hit the ATS public API for (type, slug); return evidence dict or None."""
    try:
        if ats_type == p.WORKDAY:
            url = p.workday_cxs_url(slug)
            r = await client.post(
                url,
                json={"limit": 1, "offset": 0, "searchText": ""},
                headers={"Accept": "application/json"},
            )
            if r.status_code != 200:
                return None
            data = r.json()
            return {"endpoint": url, "job_count": data.get("total", 0)}

        if ats_type == p.ORACLE_HCM:
            url = p.oracle_list_url(slug, limit=1, offset=0)
            r = await client.get(url, headers={"Accept": "application/json"})
            if r.status_code != 200:
                return None
            items = r.json().get("items") or []
            if not items:
                return None
            return {"endpoint": url, "job_count": items[0].get("TotalJobsCount", 0)}

        if ats_type == p.ICIMS:
            url = p.icims_list_url(slug, page=1, limit=1)
            r = await client.get(url, headers={"Accept": "application/json"})
            if r.status_code != 200:
                return None
            total = r.json().get("totalCount")
            if total is None:  # wrong host can still 200 with some other body
                return None
            return {"endpoint": url, "job_count": total}

        if ats_type == p.APPLE:
            # No JSON API — the search page's hydration blob is the evidence.
            url = p.apple_list_url(slug)
            r = await client.get(url, headers={"Accept": "text/html"})
            if r.status_code != 200:
                return None
            data = p.apple_hydration_data(r.text) or {}
            search = (data.get("loaderData") or {}).get("search") or {}
            total = search.get("totalRecords")
            if not isinstance(search.get("searchResults"), list) or not isinstance(total, int):
                return None
            return {"endpoint": url, "job_count": total}

        if ats_type == p.ATLASSIAN:
            url = p.atlassian_list_url(slug)
            r = await client.get(url, headers={"Accept": "application/json"})
            if r.status_code != 200:
                return None
            data = r.json()
            # Require the listings shape (a wrong path can 200 with HTML/JSON noise).
            if not isinstance(data, list) or not data or not isinstance(data[0], dict) or "title" not in data[0]:
                return None
            return {"endpoint": url, "job_count": len({str(j.get("id")) for j in data})}

        url = p.API_TEMPLATES[ats_type].format(slug=slug)
        r = await client.get(url, headers={"Accept": "application/json"})
        if r.status_code != 200:
            return None
        data = r.json()
        if ats_type == p.GREENHOUSE:
            count = len(data.get("jobs", []))
        elif ats_type == p.LEVER:
            if not isinstance(data, list):
                return None
            count = len(data)
        elif ats_type == p.ASHBY:
            count = len(data.get("jobs", []))
        elif ats_type == p.SMARTRECRUITERS:
            count = data.get("totalFound", 0)
        elif ats_type == p.BREEZY:
            if not isinstance(data, list):
                return None
            count = len(data)
        elif ats_type == p.CLEARCOMPANY:
            # A wrong siteId can still 200 with some other body — require the
            # postings envelope (same posture as the iCIMS totalCount check).
            total = data.get("totalCount") if isinstance(data, dict) else None
            if total is None:
                return None
            count = total
        else:
            return None
        evidence = {"endpoint": url, "job_count": count}
        # Board display name, where the ATS exposes one — guards against a
        # name-derived slug landing on some other company's board.
        name_url = p.NAME_TEMPLATES.get(ats_type)
        if name_url:
            rn = await client.get(name_url.format(slug=slug), headers={"Accept": "application/json"})
            if rn.status_code == 200:
                evidence["board_name"] = rn.json().get("name")
        return evidence
    except (httpx.HTTPError, json.JSONDecodeError, ValueError, AttributeError, TypeError):
        # AttributeError/TypeError: a wrong slug/host can 200 with a JSON
        # array or scalar where a dict is assumed — routine for blind
        # name-derived probes, so it means "not this candidate", not a crash.
        return None


async def detect_company(client: httpx.AsyncClient, company: sqlite3.Row) -> dict:
    result = {
        "id": company["id"],
        "name": company["name"],
        "start_url": company["careers_url"] or company["website"],
        "final_url": None,
        "ats_type": None,
        "ats_slug": None,
        "method": None,
        "evidence": None,
        "errors": [],
    }
    url = _with_scheme(result["start_url"])
    candidates: list[tuple[str, str]] = []

    if url:
        try:
            if await _robots_allows(client, url):
                r = await client.get(url, headers={"Accept": "text/html"})
                result["final_url"] = str(r.url)
                candidates = p.extract_ats_candidates(str(r.url))
                method_by_candidate = {c: "redirect" for c in candidates}
                for c in p.extract_ats_candidates(r.text):
                    if c not in method_by_candidate:
                        candidates.append(c)
                        method_by_candidate[c] = "html-scan"
                # iCIMS/Jibe: the cid is in the HTML but the API host is the
                # page's own host, so it can't come from a text scan alone.
                icims = p.icims_page_slug(result["final_url"], r.text)
                if icims and (p.ICIMS, icims) not in method_by_candidate:
                    candidates.append((p.ICIMS, icims))
                    method_by_candidate[(p.ICIMS, icims)] = "html-scan"
            else:
                result["errors"].append("robots.txt disallows page fetch")
                method_by_candidate = {}
        except (httpx.HTTPError, ValueError) as e:
            # ValueError: a malformed host (idna.IDNAError / httpx.InvalidURL,
            # both ValueError subclasses) raises here before any I/O. Without
            # this, detect_company propagated it and the guard-less
            # careers-preview route 500ed on a bad user-typed URL instead of
            # degrading to the no-board-found result.
            result["errors"].append(f"page fetch: {type(e).__name__}: {e}")
            method_by_candidate = {}
    else:
        result["errors"].append("no careers_url or website")
        method_by_candidate = {}

    for ats_type, slug in candidates:
        evidence = await verify(client, ats_type, slug)
        if evidence:
            result.update(
                ats_type=ats_type,
                ats_slug=slug,
                method=method_by_candidate.get((ats_type, slug), "html-scan"),
                evidence=evidence,
            )
            return result

    # Fallback: blind probe of slug-addressable ATS APIs. A probe is weaker
    # evidence than a signature found on the company's own page, so it must
    # clear a higher bar: a non-empty board (SmartRecruiters returns 200 with
    # totalFound=0 for slugs that don't even exist), and — where the ATS
    # exposes one — a board display name to eyeball against the company.
    for slug in p.candidate_slugs(company["name"])[:MAX_PROBE_SLUGS]:
        for ats_type in (p.GREENHOUSE, p.LEVER, p.ASHBY, p.SMARTRECRUITERS):
            evidence = await verify(client, ats_type, slug)
            if not evidence:
                continue
            if not evidence.get("job_count"):
                result["errors"].append(f"probe {ats_type}/{slug}: empty board, rejected")
                continue
            if ats_type in p.NAME_TEMPLATES and not evidence.get("board_name"):
                result["errors"].append(f"probe {ats_type}/{slug}: no board name, rejected")
                continue
            result.update(
                ats_type=ats_type, ats_slug=slug, method="slug-probe", evidence=evidence
            )
            return result

    return result


def write_result(conn: sqlite3.Connection, result: dict) -> None:
    careers_url = p.public_board_url(result["ats_type"], result["ats_slug"])
    conn.execute(
        """UPDATE companies
           SET ats_type = ?, ats_slug = ?,
               careers_url = COALESCE(careers_url, ?),
               updated_at = datetime('now')
           WHERE id = ?""",
        (result["ats_type"], result["ats_slug"], careers_url, result["id"]),
    )


async def run(dry_run: bool, company_id: int | None, redetect: bool = False) -> None:
    conn = db.connect()
    try:
        sql = "SELECT id, name, careers_url, website FROM companies"
        params: tuple = ()
        if company_id is not None:
            sql += " WHERE id = ?"
            params = (company_id,)
        elif not redetect:
            # Don't clobber rows already settled (including manual overrides).
            sql += " WHERE ats_type IS NULL"
        companies = conn.execute(sql + " ORDER BY id", params).fetchall()

        sem = asyncio.Semaphore(CONCURRENCY)
        async with httpx.AsyncClient(
            timeout=TIMEOUT, follow_redirects=True, headers={"User-Agent": USER_AGENT}
        ) as client:

            async def guarded(c: sqlite3.Row) -> dict:
                async with sem:
                    try:
                        return await detect_company(client, c)
                    except Exception as e:  # never let one company kill the run
                        return {
                            "id": c["id"], "name": c["name"], "ats_type": None,
                            "ats_slug": None, "method": None, "evidence": None,
                            "start_url": c["careers_url"] or c["website"],
                            "final_url": None,
                            "errors": [f"unexpected: {type(e).__name__}: {e}"],
                        }

            results = await asyncio.gather(*(guarded(c) for c in companies))

        for res in results:
            if res["ats_type"]:
                ev = res["evidence"] or {}
                extra = f", board_name={ev.get('board_name')!r}" if "board_name" in ev else ""
                print(
                    f"  [{res['id']:>2}] {res['name']}: {res['ats_type']}/{res['ats_slug']} "
                    f"({res['method']}, {ev.get('job_count', '?')} jobs{extra})"
                )
            else:
                errs = "; ".join(res["errors"]) or "no ATS signature found"
                print(f"  [{res['id']:>2}] {res['name']}: UNRESOLVED ({errs})")

        resolved = [r for r in results if r["ats_type"]]
        print(f"\n{len(resolved)}/{len(results)} resolved")

        RESULTS_PATH.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"full results -> {RESULTS_PATH}")

        if dry_run:
            print("dry-run: no DB writes")
        else:
            for res in resolved:
                write_result(conn, res)
            conn.commit()
            print(f"wrote {len(resolved)} companies to {db.DB_PATH}")
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Detect ATS type/slug for tracked companies")
    ap.add_argument("--dry-run", action="store_true", help="detect and report, no DB writes")
    ap.add_argument("--company-id", type=int, default=None, help="limit to one company")
    ap.add_argument(
        "--redetect", action="store_true",
        help="also re-check companies whose ats_type is already set",
    )
    args = ap.parse_args()
    asyncio.run(run(args.dry_run, args.company_id, args.redetect))


if __name__ == "__main__":
    main()
