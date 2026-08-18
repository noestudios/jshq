"""ATS signatures, slug extraction, and public-API URL templates.

Pure functions only — no I/O. detect.py (Phase 3a) and the ingestion
adapters (Phase 3b) both build on this module.

Workday slugs need a tenant, a wdN cluster, and a site name to address the
CXS endpoint, so they are stored as one string: "{tenant}.{wd}/{site}"
(e.g. "exampleco.wd1/ExamplecoCareers"). Oracle Cloud HCM likewise needs its
pod host + siteNumber, stored as "{host}/{siteNumber}"
(e.g. "exco.fa.us2.oraclecloud.com/CX"). iCIMS career sites built on Jibe
(iCIMS acquired Jibe in 2019) serve a public JSON feed at {careers_host}/api/jobs
on the company's branded careers domain (NOT on *.icims.com), so their slug also
carries the host: "{careers_host}/{cid}" (e.g. "careers.exampleco.com/exampleco").
All other ATS slugs are the bare board identifier.
"""

import json
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit

GREENHOUSE = "greenhouse"
LEVER = "lever"
ASHBY = "ashby"
SMARTRECRUITERS = "smartrecruiters"
WORKDAY = "workday"
ORACLE_HCM = "oracle_hcm"
ICIMS = "icims"
BREEZY = "breezy"
CLEARCOMPANY = "clearcompany"
APPLE = "apple"
ATLASSIAN = "atlassian"
MANUAL = "manual"

ATS_TYPES = (
    GREENHOUSE, LEVER, ASHBY, SMARTRECRUITERS, WORKDAY, ORACLE_HCM, ICIMS, BREEZY,
    CLEARCOMPANY, APPLE, ATLASSIAN, MANUAL,
)

_SLUG = r"([A-Za-z0-9_-]+)"

# ClearCompany siteId — a full UUID, the only slug shape its API accepts.
_CC_SITE_ID = r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"

# Ordered: first match in HTML/URL wins within each family.
SIGNATURES: dict[str, list[re.Pattern]] = {
    GREENHOUSE: [
        re.compile(r"(?:job-boards|boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?(?:[^\"'\s]*&)?for=|v1/boards/)?" + _SLUG, re.I),
        re.compile(r"boards-api(?:\.eu)?\.greenhouse\.io/v1/boards/" + _SLUG, re.I),
        re.compile(r"grnh\.se/" + _SLUG, re.I),
        # Branded careers sites that render the board client-side embed the token
        # as a JS/JSON variable instead of a boards.greenhouse.io URL (e.g. a
        # Next.js "PUBLIC_GREENHOUSE_BOARD":"acmeco"). Such a site's careers URL
        # and its boards.greenhouse.io/<token> URL both redirect to the branded
        # page, so nothing above matches — the var is the only signal. The
        # separator is optional/underscore (never a space), so prose can't match,
        # and verify() rejects any wrong token against the live board API.
        re.compile(r"greenhouse[_-]?board(?:[_-]?token)?[\"']?\s*[:=]\s*[\"']" + _SLUG, re.I),
    ],
    LEVER: [
        re.compile(r"(?:jobs|api)(?:\.eu)?\.lever\.co/(?:v0/postings/)?" + _SLUG, re.I),
    ],
    ASHBY: [
        re.compile(r"jobs\.ashbyhq\.com/" + _SLUG, re.I),
        re.compile(r"api\.ashbyhq\.com/posting-api/job-board/" + _SLUG, re.I),
    ],
    SMARTRECRUITERS: [
        re.compile(r"(?:careers|jobs)\.smartrecruiters\.com/" + _SLUG, re.I),
        re.compile(r"api\.smartrecruiters\.com/v1/companies/" + _SLUG, re.I),
    ],
    # tenant.wdN.myworkdayjobs.com[/xx-XX][/Site]; site segment optional in
    # the wild — without it the CXS endpoint can't be built (partial match).
    WORKDAY: [
        re.compile(
            r"([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com"
            r"(?:/(?:[a-z]{2}-[A-Z]{2}/)?(?!wday\b|en\b|job\b)([A-Za-z0-9_-]+))?",
        ),
    ],
    # Oracle Cloud HCM (Fusion Candidate Experience). Branded careers sites
    # embed the pod host + siteNumber as
    # {pod}.fa.{dc}.oraclecloud.com/hcmUI/CandidateExperience/{locale}/sites/CX...
    ORACLE_HCM: [
        re.compile(
            r"([a-z0-9-]+\.fa\.[a-z0-9-]+\.oraclecloud\.com)"
            r"/hcmUI/CandidateExperience/[a-z-]+/sites/(CX[A-Za-z0-9_]*)",
            re.I,
        ),
    ],
    # Breezy HR: postings live at a per-company subdomain {slug}.breezy.hr. The
    # public JSON list (/json) carries no description, so the adapter fetches the
    # schema.org JobPosting JSON-LD from each /p/{id} page (two-phase).
    BREEZY: [
        re.compile(r"([a-z0-9-]+)\.breezy\.hr", re.I),
    ],
    # ClearCompany: modern career sites are client-side shells that embed
    # careers-content.clearcompany.com/js/v1/career-site.js?siteId=<uuid> — the
    # widget injects the postings in the browser, so an HTML scan sees no jobs.
    # The siteId UUID is the slug; requiring the full UUID shape keeps API-path
    # words (settings/chatbot/insights) from ever reading as slugs. A company's
    # own {sub}.clearcompany.com host is NOT a signature — the API is only
    # addressable by siteId, which such pages still embed via the widget.
    CLEARCOMPANY: [
        re.compile(
            r"careers-content\.clearcompany\.com/js/v\d+/career-site[^\"'\s]*?"
            r"[?&]siteId=" + _CC_SITE_ID,
            re.I,
        ),
        re.compile(r"careers-api\.clearcompany\.com/v\d+/" + _CC_SITE_ID, re.I),
    ],
    # Apple Jobs: no public API — the careers site is a React Router app whose
    # server render embeds full search/detail state in a hydration blob (see
    # apple_hydration_data). The slug is the search query string from the
    # careers URL (e.g. "team=human-interface-design-DESGN-HID"), so the
    # tracked scope is whatever filter the careers_url selects; the APPLE
    # branch of extract_ats_candidates normalizes it (drops page/locale noise)
    # and rejects an unfiltered query, which would pull the entire board.
    APPLE: [
        re.compile(r"jobs\.apple\.com/[a-z]{2}-[a-z]{2}/search\?([^\s\"'<>#]+)", re.I),
    ],
    # Atlassian's careers site is bespoke (the join.atlassian.com Jibe gateway
    # is an ABANDONED snapshot — frozen 2025-11, disjoint from the live
    # board). The slug is just the matched host: the feed
    # endpoint is fixed, nothing to parameterize.
    ATLASSIAN: [
        re.compile(r"(www\.atlassian\.com)/company/careers", re.I),
    ],
}

# Slugs that are URL path noise, never board identifiers.
_SLUG_STOPWORDS = {"embed", "v1", "boards", "jobs", "job", "postings", "css", "js", "img"}


def workday_slug(tenant: str, wd: str, site: str | None) -> str | None:
    if not site:
        return None
    return f"{tenant}.{wd}/{site}"


def split_workday_slug(slug: str) -> tuple[str, str, str]:
    """'exampleco.wd1/ExamplecoCareers' -> ('exampleco', 'wd1', 'ExamplecoCareers')."""
    host, site = slug.split("/", 1)
    tenant, wd = host.rsplit(".", 1)
    return tenant, wd, site


def oracle_slug(host: str, site: str) -> str:
    return f"{host}/{site}"


def split_oracle_slug(slug: str) -> tuple[str, str]:
    """'exco.fa.us2.oraclecloud.com/CX' -> ('exco.fa.us2.oraclecloud.com', 'CX')."""
    host, site = slug.split("/", 1)
    return host, site


# iCIMS/Jibe Candidate Gateway fingerprints, found in the careers-page HTML.
# The cid (customer code, e.g. "exampleco") is the second slug segment; the host —
# the load-bearing part — comes from the page's own URL, so iCIMS is detected by
# icims_page_slug (below), not via SIGNATURES/extract_ats_candidates.
_JIBE_CID_RE = re.compile(r'window\._jibe\s*=\s*\{[^}]*"cid"\s*:\s*"([A-Za-z0-9_-]+)"', re.I)
_JIBE_CDN_RE = re.compile(r"(?:cms|app)\.jibecdn\.com/prod/([A-Za-z0-9_-]+)", re.I)


def icims_slug(host: str, cid: str) -> str:
    return f"{host}/{cid}"


def split_icims_slug(slug: str) -> tuple[str, str]:
    """'careers.exampleco.com/exampleco' -> ('careers.exampleco.com', 'exampleco')."""
    host, cid = slug.split("/", 1)
    return host, cid


def icims_page_slug(final_url: str | None, html: str) -> str | None:
    """Compound iCIMS/Jibe slug "{careers_host}/{cid}" from a careers page.

    The Jibe JSON feed lives at {careers_host}/api/jobs on the branded careers
    domain (it 404s on *.icims.com), and that host can't be recovered from the
    page text alone — so the host is taken from the page's final URL and the cid
    from the window._jibe bootstrap (or a jibecdn.com/prod/<cid> reference).
    """
    if not final_url:
        return None
    m = _JIBE_CID_RE.search(html) or _JIBE_CDN_RE.search(html)
    if not m:
        return None
    host = urlsplit(final_url).netloc
    if not host:
        return None
    return icims_slug(host, m.group(1))


def extract_ats_candidates(text: str) -> list[tuple[str, str]]:
    """Scan a URL or HTML blob for ATS signatures.

    Returns deduped (ats_type, slug) candidates in discovery order. Workday
    matches without a site segment are skipped (unverifiable).
    """
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for ats_type, pats in SIGNATURES.items():
        for pat in pats:
            for m in pat.finditer(text):
                if ats_type == WORKDAY:
                    slug = workday_slug(m.group(1), m.group(2), m.group(3))
                    if slug is None:
                        continue
                elif ats_type == ORACLE_HCM:
                    slug = oracle_slug(m.group(1), m.group(2))
                elif ats_type == APPLE:
                    slug = apple_normalize_slug(m.group(1))
                    if slug is None:
                        continue
                else:
                    slug = m.group(1)
                    if slug.lower() in _SLUG_STOPWORDS:
                        continue
                key = (ats_type, slug.lower())
                if key not in seen:
                    seen.add(key)
                    out.append((ats_type, slug))
    return out


# Public, unauthenticated endpoints used to verify a (type, slug) candidate.
# Workday is POST with a JSON body; the rest are GET.
API_TEMPLATES = {
    GREENHOUSE: "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
    LEVER: "https://api.lever.co/v0/postings/{slug}?mode=json",
    ASHBY: "https://api.ashbyhq.com/posting-api/job-board/{slug}",
    SMARTRECRUITERS: "https://api.smartrecruiters.com/v1/companies/{slug}/postings",
    BREEZY: "https://{slug}.breezy.hr/json",
    CLEARCOMPANY: "https://careers-api.clearcompany.com/v1/{slug}",
}

# Board-name endpoints, used to sanity-check blind slug probes.
NAME_TEMPLATES = {
    GREENHOUSE: "https://boards-api.greenhouse.io/v1/boards/{slug}",
    SMARTRECRUITERS: "https://api.smartrecruiters.com/v1/companies/{slug}",
}

PUBLIC_BOARD_TEMPLATES = {
    GREENHOUSE: "https://boards.greenhouse.io/{slug}",
    LEVER: "https://jobs.lever.co/{slug}",
    ASHBY: "https://jobs.ashbyhq.com/{slug}",
    SMARTRECRUITERS: "https://careers.smartrecruiters.com/{slug}",
    BREEZY: "https://{slug}.breezy.hr",
}


def workday_cxs_url(slug: str) -> str:
    tenant, wd, site = split_workday_slug(slug)
    return f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"


def workday_board_url(slug: str) -> str:
    tenant, wd, site = split_workday_slug(slug)
    return f"https://{tenant}.{wd}.myworkdayjobs.com/{site}"


_ORACLE_API = "https://{host}/hcmRestApi/resources/latest"


def oracle_list_url(slug: str, limit: int, offset: int) -> str:
    host, site = split_oracle_slug(slug)
    return (
        f"{_ORACLE_API.format(host=host)}/recruitingCEJobRequisitions"
        f"?onlyData=true&expand=requisitionList"
        f"&finder=findReqs;siteNumber={site},limit={limit},offset={offset}"
        f",sortBy=POSTING_DATES_DESC"
    )


def oracle_detail_url(slug: str, req_id: str) -> str:
    host, site = split_oracle_slug(slug)
    return (
        f"{_ORACLE_API.format(host=host)}/recruitingCEJobRequisitionDetails"
        f'?onlyData=true&expand=all&finder=ById;Id="{req_id}",siteNumber={site}'
    )


def oracle_job_url(slug: str, req_id: str) -> str:
    host, site = split_oracle_slug(slug)
    return f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{req_id}/"


def icims_list_url(slug: str, page: int, limit: int) -> str:
    host, _cid = split_icims_slug(slug)
    return f"https://{host}/api/jobs?page={page}&limit={limit}"


def clearcompany_list_url(slug: str, page: int = 1) -> str:
    """Postings JSON for a siteId. ?p is the widget's 1-based page param;
    omitted for page 1 (small sites return everything in one unpaginated page
    and ignore ?p entirely — the adapter guards on repeated ids)."""
    base = API_TEMPLATES[CLEARCOMPANY].format(slug=slug)
    return base if page <= 1 else f"{base}?p={page}"


def breezy_list_url(slug: str) -> str:
    return f"https://{slug}.breezy.hr/json"


def breezy_job_url(slug: str, fid: str) -> str:
    return f"https://{slug}.breezy.hr/p/{fid}"


_APPLE_BASE = "https://jobs.apple.com/en-us"

# Query params that scope a search; anything else (page, sort, locale noise)
# is dropped from the slug so refreshes always start at page 1.
_APPLE_SCOPE_PARAMS = {"team", "search", "location", "product", "lob", "key"}

_APPLE_HYDRATION_RE = re.compile(
    r"window\.__staticRouterHydrationData\s*=\s*JSON\.parse\((\"(?:[^\"\\]|\\.)*\")\)"
)


def apple_normalize_slug(query: str) -> str | None:
    """Search query string -> slug, or None if it doesn't scope the search.

    An unscoped slug would ingest Apple's entire board (thousands of reqs), so
    a query with no scoping params is not a candidate.
    """
    pairs = [(k, v) for k, v in parse_qsl(query) if k in _APPLE_SCOPE_PARAMS and v]
    return urlencode(pairs) if pairs else None


def apple_list_url(slug: str, page: int = 1) -> str:
    url = f"{_APPLE_BASE}/search?{slug}"
    return url if page <= 1 else f"{url}&page={page}"


def apple_detail_url(job_id: str, transformed_title: str) -> str:
    return f"{_APPLE_BASE}/details/{job_id}/{transformed_title or 'job'}"


def apple_hydration_data(html_text: str) -> dict | None:
    """Decode the React Router hydration blob from a jobs.apple.com page.

    The server render assigns window.__staticRouterHydrationData =
    JSON.parse("<escaped JSON>") — the match is a JS string literal containing
    JSON text, hence the double json.loads.
    """
    m = _APPLE_HYDRATION_RE.search(html_text)
    if not m:
        return None
    try:
        data = json.loads(json.loads(m.group(1)))
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


_ATLASSIAN_BASE = "https://www.atlassian.com"


def atlassian_list_url(slug: str = "") -> str:
    """The whole board in one unpaginated JSON list; the slug records what the
    signature matched but the endpoint is fixed."""
    return f"{_ATLASSIAN_BASE}/endpoint/careers/listings"


def atlassian_job_url(job_id: str) -> str:
    return f"{_ATLASSIAN_BASE}/company/careers/details/{job_id}"


def public_board_url(ats_type: str, slug: str) -> str | None:
    if ats_type == WORKDAY:
        return workday_board_url(slug)
    if ats_type == ORACLE_HCM:
        host, site = split_oracle_slug(slug)
        return f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}"
    if ats_type == ICIMS:
        host, _cid = split_icims_slug(slug)
        return f"https://{host}"
    if ats_type == APPLE:
        return apple_list_url(slug)
    if ats_type == ATLASSIAN:
        return f"{_ATLASSIAN_BASE}/company/careers/all-jobs"
    tmpl = PUBLIC_BOARD_TEMPLATES.get(ats_type)
    return tmpl.format(slug=slug) if tmpl else None


_ASCII_MAP = str.maketrans({"ø": "o", "Ø": "o", "æ": "ae", "å": "a", "ß": "ss"})
_NOISE_WORDS = {"of", "the", "inc", "llc", "co", "corp", "group", "foundation", "pbc", "america"}


def _asciify(name: str) -> str:
    name = name.translate(_ASCII_MAP)
    return unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()


def candidate_slugs(name: str) -> list[str]:
    """Name-derived slug guesses for blind probing, best-first, deduped."""
    words = re.findall(r"[a-z0-9]+", _asciify(name).lower())
    if not words:
        return []
    core = [w for w in words if w not in _NOISE_WORDS] or words
    guesses = [
        "".join(words),
        "-".join(words),
        "".join(core),
        "-".join(core),
        core[0],
    ]
    out: list[str] = []
    for g in guesses:
        if g and g not in out:
            out.append(g)
    return out


# Careers-subdomain / host noise that is never the brand label.
_HOST_NOISE = {"www", "careers", "career", "jobs", "job", "apply", "work", "talent", "hire", "join"}


def host_slug_candidates(url: str | None) -> list[str]:
    """Slug guesses derived from a careers/website URL's host, best-first.

    A user-supplied careers URL is strong brand evidence even when the tracked
    company name differs from the ATS board slug, or when the careers page is
    fully client-rendered and exposes no in-page signature (the board loads via
    JS after the fetch). The host's registrable label ("exampleco" from
    www.exampleco.com, careers.exampleco.com, or exampleco.com) is probed like a
    name-derived slug — still gated by verify() + a non-empty board downstream,
    so a wrong guess is rejected rather than trusted.
    """
    if not url:
        return []
    scheme_less = re.match(r"^[a-z][a-z0-9+.-]*:", url, re.I) is None
    netloc = urlsplit(f"//{url}" if scheme_less else url).netloc
    host = netloc.rsplit("@", 1)[-1].split(":", 1)[0].lower()  # strip any creds / port
    labels = [seg for seg in host.split(".") if seg]
    if len(labels) >= 2:
        labels = labels[:-1]  # drop the TLD label
    labels = [seg for seg in labels if seg not in _HOST_NOISE] or labels
    brand = labels[-1] if labels else ""
    return candidate_slugs(brand)
