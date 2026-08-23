"""Adapter tests against recorded fixtures via httpx.MockTransport — no live endpoints."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from jshq.ats import patterns as p
from jshq.ats.adapters import (
    ADAPTERS,
    apple,
    ashby,
    atlassian,
    breezy,
    clearcompany,
    greenhouse,
    icims,
    lever,
    oracle_hcm,
    recruitee,
    rippling,
    smartrecruiters,
    workable,
    workday,
)
from jshq.ats.normalize import AdapterError, compile_title_filter

FIXTURES = Path(__file__).parent / "fixtures"

FILTER = compile_title_filter(
    ["design", "designer", "ux", "user experience", "user research", "researcher"]
)


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def run_fetch(handler, fetch, slug, **kwargs):
    async def go():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await fetch(client, slug, FILTER, **kwargs)

    return asyncio.run(go())


# --- adapter registry contract ---


def test_adapter_registry_covers_all_ats_types():
    """ADAPTERS must track patterns.ATS_TYPES: every supported type is
    dispatchable, and the only type without an adapter is the one deliberate
    exception — MANUAL (hand-entered, not on a board we poll). Fails loudly if
    a new ATS_TYPE or adapter module is added without being wired into
    ADAPTERS."""
    assert set(ADAPTERS) <= set(p.ATS_TYPES), "ADAPTERS key missing from ATS_TYPES"
    assert all(callable(fn) for fn in ADAPTERS.values())
    assert set(p.ATS_TYPES) - set(ADAPTERS) == {p.MANUAL}


# --- greenhouse ---


def test_greenhouse_maps_and_filters():
    def handler(request):
        assert request.url.path == "/v1/boards/exampleco/jobs"
        assert request.url.params["content"] == "true"
        return httpx.Response(200, content=fixture_bytes("greenhouse_jobs.json"))

    jobs = run_fetch(handler, greenhouse.fetch, "exampleco")
    assert [j.title for j in jobs] == ["Director of Product Design", "Senior Product Designer"]

    j = jobs[0]
    assert j.external_id == "5001001001"
    assert j.url == "https://boards.greenhouse.io/exampleco/jobs/5001001001"
    assert j.location == "Remote - US"
    assert j.remote_type == "remote"
    assert (j.salary_min, j.salary_max, j.salary_stated) == (180000, 220000, True)
    # entity-escaped HTML decoded to plain text
    assert "Lead our design org" in j.description_text
    assert "&lt;" not in j.description_text and "<" not in j.description_text

    assert jobs[1].salary_stated is False
    assert jobs[1].remote_type == "onsite"


def test_greenhouse_http_error_raises():
    def handler(request):
        return httpx.Response(503)

    with pytest.raises(AdapterError):
        run_fetch(handler, greenhouse.fetch, "exampleco")


# --- lever ---


def test_lever_maps_and_filters():
    def handler(request):
        assert request.url.path == "/v0/postings/exampleco"
        assert request.url.params["mode"] == "json"
        return httpx.Response(200, content=fixture_bytes("lever_postings.json"))

    jobs = run_fetch(handler, lever.fetch, "exampleco")
    assert [j.title for j in jobs] == [
        "Sr. Director, Product Design",
        "Senior Product Designer",
        "Sr. UX Researcher",
    ]

    j = jobs[0]
    assert j.external_id == "a1b2c3d4-0001"
    assert j.url == "https://jobs.lever.co/exampleco/a1b2c3d4-0001"
    assert j.location == "Remote(US)"
    assert j.remote_type == "remote"
    # structured yearly salaryRange beats regex extraction
    assert (j.salary_min, j.salary_max, j.salary_stated) == (196000, 250000, True)
    # lists joined into the JD with entities decoded; additionalPlain kept
    assert "Mentor & grow senior designers" in j.description_text
    assert "equal opportunity" in j.description_text

    # no salaryRange -> regex extraction from the joined text
    assert (jobs[1].salary_min, jobs[1].salary_max, jobs[1].salary_stated) == (
        150000, 180000, True,
    )
    assert jobs[1].remote_type == "onsite"  # workplaceType "unspecified" defers

    # hourly salaryRange is not a stated annual salary; hybrid hint wins
    assert jobs[2].salary_stated is False
    assert jobs[2].remote_type == "hybrid"


def test_lever_empty_list_section_does_not_embed_the_string_none():
    # A lists section with a header but empty content strips to None; the
    # f-string would embed the literal text "None" in the stored JD (and
    # perturb the changed-JD rescore trigger on the next refresh).
    posting = {
        "id": "x-1",
        "text": "Product Designer",
        "descriptionPlain": "Intro paragraph.",
        "lists": [{"text": "Requirements", "content": ""}],
        "categories": {"location": "Chicago, IL"},
    }

    def handler(request):
        return httpx.Response(200, json=[posting])

    (job,) = run_fetch(handler, lever.fetch, "exampleco")
    assert "None" not in job.description_text
    assert "Requirements" in job.description_text


def test_lever_http_error_raises():
    def handler(request):
        return httpx.Response(503)

    with pytest.raises(AdapterError):
        run_fetch(handler, lever.fetch, "exampleco")


# --- ashby ---


def test_ashby_maps_and_filters():
    def handler(request):
        assert request.url.path == "/posting-api/job-board/exampleco"
        assert request.url.params["includeCompensation"] == "true"
        return httpx.Response(200, content=fixture_bytes("ashby_board.json"))

    jobs = run_fetch(handler, ashby.fetch, "exampleco")
    assert [j.title for j in jobs] == ["Product Designer", "Head of Design"]

    j = jobs[0]
    assert j.external_id == "11111111-2521-47d5-a97e-c51bbd2537ee"
    assert j.url.startswith("https://jobs.ashbyhq.com/exampleco/")
    assert j.remote_type == "hybrid"  # workplaceType wins over location scan
    # structured compensation summary parsed
    assert (j.salary_min, j.salary_max, j.salary_stated) == (150000, 190000, True)
    assert j.description_text == "Design core product surfaces end to end."

    assert jobs[1].remote_type == "remote"  # isRemote: true
    assert jobs[1].salary_stated is False


# --- smartrecruiters ---


def test_smartrecruiters_paginates_and_fetches_details_only_for_matches(monkeypatch):
    monkeypatch.setattr(smartrecruiters, "PAGE_LIMIT", 2)
    requests = []

    def handler(request):
        requests.append(request.url.path + ("?" + str(request.url.query, "ascii") if request.url.query else ""))
        path = request.url.path
        if path == "/v1/companies/exampleco/postings":
            offset = request.url.params.get("offset")
            name = "smartrecruiters_postings_p1.json" if offset == "0" else "smartrecruiters_postings_p2.json"
            return httpx.Response(200, content=fixture_bytes(name))
        if path.startswith("/v1/companies/exampleco/postings/"):
            return httpx.Response(200, content=fixture_bytes("smartrecruiters_detail.json"))
        return httpx.Response(404)

    jobs = run_fetch(handler, smartrecruiters.fetch, "exampleco")
    assert [j.title for j in jobs] == ["Design Lead, Brand", "Senior UX Researcher"]

    detail_paths = [r for r in requests if "/postings/6" in r]
    assert sorted(detail_paths) == [
        "/v1/companies/exampleco/postings/6000000000000001",
        "/v1/companies/exampleco/postings/6000000000000003",
    ]  # no detail fetch for the non-matching Data Engineer
    assert len([r for r in requests if "limit=" in r]) == 2  # two list pages

    j = jobs[0]
    assert j.external_id == "6000000000000001"
    assert j.url == "https://jobs.smartrecruiters.com/exampleco/6000000000000001-design-lead-brand"
    assert j.location == "Sydney, NSW, Australia"
    assert j.remote_type == "hybrid"  # location.hybrid flag
    assert (j.salary_min, j.salary_max, j.salary_stated) == (140000, 170000, True)
    assert "ExampleCo makes example things." in j.description_text
    assert "Lead brand design" in j.description_text

    assert jobs[1].remote_type == "remote"  # location.remote flag


# --- workday ---


def test_workday_keyword_union_pagination_and_selective_details(monkeypatch):
    monkeypatch.setattr(workday, "PAGE_LIMIT", 2)
    list_requests, detail_requests = [], []

    def handler(request):
        path = request.url.path
        if path == "/wday/cxs/exampleco/Careers/jobs":
            body = json.loads(request.content)
            list_requests.append((body["searchText"], body["offset"]))
            name = "workday_jobs_p1.json" if body["offset"] == 0 else "workday_jobs_p2.json"
            return httpx.Response(200, content=fixture_bytes(name))
        if path.startswith("/wday/cxs/exampleco/Careers/job/"):
            detail_requests.append(path)
            return httpx.Response(200, content=fixture_bytes("workday_detail.json"))
        return httpx.Response(404)

    # search terms come from the run's config now, not a module constant
    jobs = run_fetch(
        handler,
        workday.fetch,
        "exampleco.wd1/Careers",
        config={"workday_search_terms": ["design", "research"]},
    )
    assert [j.title for j in jobs] == [
        "Senior Design Program Manager",
        "Director, User Experience Design",
    ]

    # both terms paginated (2 pages each)...
    assert list_requests == [("design", 0), ("design", 2), ("research", 0), ("research", 2)]
    # ...but the union dedupes by externalPath: 2 details, never for the engineer
    assert sorted(detail_requests) == [
        "/wday/cxs/exampleco/Careers/job/Boston-MA/Senior-Design-Program-Manager_R100",
        "/wday/cxs/exampleco/Careers/job/Remote-USA/Director-User-Experience-Design_R102",
    ]

    j = jobs[0]
    assert j.external_id == "R100"
    assert j.url == "https://exampleco.wd1.myworkdayjobs.com/Careers/job/Boston-MA/Senior-Design-Program-Manager_R100"
    assert j.location == "Boston, MA"
    assert (j.salary_min, j.salary_max, j.salary_stated) == (160000, 195000, True)
    assert "Run design operations" in j.description_text


def test_workday_http_error_raises():
    def handler(request):
        return httpx.Response(500)

    with pytest.raises(AdapterError):
        run_fetch(
            handler,
            workday.fetch,
            "exampleco.wd1/Careers",
            config={"workday_search_terms": ["design"]},
        )


def test_workday_no_terms_is_an_error_not_an_empty_board():
    # Workday requires a searchText: with the include list empty and no
    # override there is nothing to search FOR. That must surface as an
    # AdapterError (the refresh skip path), NOT a successful empty fetch: an
    # "ok" empty board feeds the decay counter, so previously ingested jobs
    # would be closed as "no longer listed" within two refreshes while still
    # live — with a healthy-looking status. The error text lands in
    # ats_last_status where the user can actually see it.
    def handler(request):
        raise AssertionError("no request should be made without terms")

    with pytest.raises(AdapterError, match="no search terms"):
        run_fetch(handler, workday.fetch, "exampleco.wd1/Careers")


# --- oracle cloud hcm ---


def test_oracle_hcm_paginates_and_fetches_details_only_for_matches(monkeypatch):
    monkeypatch.setattr(oracle_hcm, "PAGE_LIMIT", 2)
    list_offsets, detail_queries = [], []

    def handler(request):
        path = request.url.path
        query = request.url.query.decode()
        if path.endswith("/recruitingCEJobRequisitions"):
            offset = "0" if "offset=0" in query else "2"
            list_offsets.append(offset)
            name = "oracle_hcm_list_p1.json" if offset == "0" else "oracle_hcm_list_p2.json"
            return httpx.Response(200, content=fixture_bytes(name))
        if path.endswith("/recruitingCEJobRequisitionDetails"):
            detail_queries.append(query)
            return httpx.Response(200, content=fixture_bytes("oracle_hcm_detail.json"))
        return httpx.Response(404)

    jobs = run_fetch(handler, oracle_hcm.fetch, "exco.fa.us2.oraclecloud.com/CX")
    assert [j.title for j in jobs] == ["Director, Product Design", "Senior UX Researcher"]

    assert list_offsets == ["0", "2"]  # two list pages via the offset loop
    # detail fetched only for the two title matches, never the non-design row
    assert len(detail_queries) == 2
    assert any("30000001" in q for q in detail_queries)
    assert any("30000003" in q for q in detail_queries)

    j = jobs[0]
    assert j.external_id == "30000001"
    assert j.url == (
        "https://exco.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/job/30000001/"
    )
    assert j.location == "Chicago, IL, United States"
    assert j.remote_type == "hybrid"  # WorkplaceTypeCode ORA_HYBRID wins
    assert (j.salary_min, j.salary_max, j.salary_stated) == (150000, 190000, True)
    assert "Exampleco Rewards" in j.description_text
    assert "leading design teams" in j.description_text  # qualifications section joined
    assert "<" not in j.description_text  # HTML stripped

    assert jobs[1].external_id == "30000003"
    assert jobs[1].remote_type == "remote"  # ORA_REMOTE


def test_oracle_hcm_http_error_raises():
    def handler(request):
        return httpx.Response(502)

    with pytest.raises(AdapterError):
        run_fetch(handler, oracle_hcm.fetch, "exco.fa.us2.oraclecloud.com/CX")


# --- icims / jibe candidate gateway ---


def test_icims_paginates_one_call_and_filters(monkeypatch):
    monkeypatch.setattr(icims, "PAGE_LIMIT", 2)
    pages, detail_hits = [], []

    def handler(request):
        if request.url.path == "/api/jobs":
            page = request.url.params.get("page")
            pages.append(page)
            name = "icims_list_p1.json" if page == "1" else "icims_list_p2.json"
            return httpx.Response(200, content=fixture_bytes(name))
        detail_hits.append(request.url.path)  # any per-job fetch would land here
        return httpx.Response(404)

    jobs = run_fetch(handler, icims.fetch, "careers.exampleco.com/exampleco")
    assert [j.title for j in jobs] == [
        "Director, Product Design",
        "Senior UX Researcher",
        "UX Design Intern",
    ]  # Staff Accountant filtered out

    assert pages == ["1", "2"]  # paged via totalCount, then stopped
    assert detail_hits == []  # one call: descriptions are inline, no detail fetch

    j = jobs[0]
    assert j.external_id == "30001"
    assert j.url == "https://careers.exampleco.com/jobs/30001?lang=en-us"  # meta_data.canonical_url
    assert j.location == "Chicago, IL, United States"
    assert j.remote_type == "onsite"  # LAT_LNG -> location scan -> onsite
    assert (j.salary_min, j.salary_max, j.salary_stated) == (150000, 190000, True)  # tags8
    assert "Lead our product design org" in j.description_text
    assert "leading design teams" in j.description_text  # qualifications section joined
    assert "<" not in j.description_text  # HTML stripped

    assert jobs[1].external_id == "30003"
    assert jobs[1].remote_type == "remote"  # location_type ANY
    assert jobs[1].salary_stated is False  # tags8 null, no salary in description

    assert jobs[2].remote_type == "onsite"
    assert jobs[2].salary_stated is False  # hourly comp never stored as an annual salary


def test_icims_missing_req_id_and_slug_yields_null_external_id():
    # str(None) is the truthy string "None": every such row would share the
    # dedupe key "{cid}:None" and collapse into one job. A null external_id
    # falls back to the title|location dedupe key like the other adapters.
    row = {"data": {"title": "Senior UX Researcher", "full_location": "Chicago, IL"}}

    def handler(request):
        return httpx.Response(200, json={"jobs": [row], "totalCount": 1})

    (job,) = run_fetch(handler, icims.fetch, "careers.exampleco.com/exampleco")
    assert job.external_id is None


def test_icims_http_error_raises():
    def handler(request):
        return httpx.Response(500)

    with pytest.raises(AdapterError):
        run_fetch(handler, icims.fetch, "careers.exampleco.com/exampleco")


def test_icims_stale_feed_raises():
    # A Jibe gateway can outlive the ATS behind it and keep serving its final
    # snapshot — months-old newest dates must fail loudly, never ingest.
    # NOTE the recorded fixtures carry NO date fields, so the guard stands down
    # for them (and for a legitimately empty board) by design.
    stale = {
        "jobs": [{"data": {
            "title": "Senior Design Manager, Design Systems",
            "req_id": "20001", "full_location": "San Francisco, CA",
            "description": "<p>x</p>",
            "posted_date": "2025-08-15T12:00:00+0000",
            "update_date": "2025-10-17T19:35:54+0000",
        }}],
        "totalCount": 1,
    }

    def handler(request):
        return httpx.Response(200, json=stale)

    with pytest.raises(AdapterError):
        run_fetch(handler, icims.fetch, "join.acmeco.com/acmeco")


def test_icims_fresh_dates_pass_the_guard():
    fresh = {
        "jobs": [{"data": {
            "title": "Product Designer",
            "req_id": "40001", "full_location": "Remote",
            "description": "<p>x</p>",
            # far-future so this inline fixture never ages into the guard
            "update_date": "2099-01-15T12:00:00+0000",
        }}],
        "totalCount": 1,
    }

    def handler(request):
        return httpx.Response(200, json=fresh)

    jobs = run_fetch(handler, icims.fetch, "careers.exampleco.com/exampleco")
    assert [j.title for j in jobs] == ["Product Designer"]


# --- breezy ---


def test_breezy_two_phase_and_jsonld():
    """List carries no description; title-filter the list, then pull the JSON-LD
    JobPosting description from each matched /p/{id} page."""

    def handler(request):
        path = request.url.path
        if path == "/json":
            return httpx.Response(200, content=fixture_bytes("breezy_list.json"))
        if path.startswith("/p/"):
            return httpx.Response(200, content=fixture_bytes("breezy_position.html"))
        return httpx.Response(404)

    jobs = run_fetch(handler, breezy.fetch, "exampleco")
    # the design role is kept; the backend engineer is filtered out
    assert [j.title for j in jobs] == ["Director of UX Design"]
    j = jobs[0]
    assert j.external_id == "abc123"
    assert j.url == "https://exampleco.breezy.hr/p/abc123-director-of-ux-design"
    assert j.location == "Remote, US"
    assert j.remote_type == "remote"  # location.is_remote
    assert j.description_text and "grow the practice" in j.description_text
    assert "<" not in j.description_text  # JSON-LD HTML stripped
    assert (j.salary_min, j.salary_max, j.salary_stated) == (210000, 250000, True)


def test_breezy_http_error_raises():
    def handler(request):
        return httpx.Response(502)

    with pytest.raises(AdapterError):
        run_fetch(handler, breezy.fetch, "exampleco")


# --- clearcompany ---


def test_clearcompany_maps_and_filters():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        assert request.url.path == "/v1/0a1b2c3d-4e5f-6071-8293-a4b5c6d7e8f9"
        return httpx.Response(200, content=fixture_bytes("clearcompany_jobs.json"))

    jobs = run_fetch(handler, clearcompany.fetch, "0a1b2c3d-4e5f-6071-8293-a4b5c6d7e8f9")
    # single page (totalCount met on page 1) — no second request
    assert len(calls) == 1
    assert [j.title for j in jobs] == [
        "Senior UX Designer / Researcher (Remote)",
        "Senior UX Designer (Records Modernization)",
    ]

    j = jobs[0]
    assert j.external_id == "a1b2c3d4-e5f6-0708-090a-000000000001"
    # applyLink minus the trailing /apply = the public posting page
    assert j.url == (
        "https://exampleco.clearcompany.com/careers/jobs/"
        "a1b2c3d4-e5f6-0708-090a-000000000001"
    )
    assert j.location == "Remote (US)"
    assert j.remote_type == "remote"  # locations[].isRemote hint + location text
    assert (j.salary_min, j.salary_max, j.salary_stated) == (110000, 130000, True)
    assert "<" not in j.description_text  # HTML stripped


def test_clearcompany_pagination_stops_on_repeated_page():
    # Small sites IGNORE ?p and re-serve the same page while claiming a larger
    # totalCount — the repeated-ids guard must stop the loop, not spin to
    # MAX_PAGES or duplicate postings.
    page = {
        "results": [{
            "id": "aaaa", "positionTitle": "Product Designer",
            "description": "<p>Design.</p>", "location": "Remote (US)",
            "locations": [{"isRemote": True}],
            "applyLink": "https://x.clearcompany.com/careers/jobs/aaaa/apply",
        }],
        "currentPageIndex": 0, "totalCount": 5, "currentPageCount": 1,
    }
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json=page)

    jobs = run_fetch(handler, clearcompany.fetch, "0a1b2c3d-4e5f-6071-8293-a4b5c6d7e8f9")
    assert len(jobs) == 1
    assert len(calls) == 2  # page 1, then the repeat detected on page 2


def test_clearcompany_paginates_until_total():
    pages = {
        1: {"results": [{"id": "a1", "positionTitle": "Product Designer",
                         "description": "d", "location": "Remote (US)", "locations": [],
                         "applyLink": None}],
            "totalCount": 2},
        2: {"results": [{"id": "b2", "positionTitle": "Staff Designer",
                         "description": "d", "location": "Remote (US)", "locations": [],
                         "applyLink": None}],
            "totalCount": 2},
    }

    def handler(request):
        n = int(request.url.params.get("p", 1))
        return httpx.Response(200, json=pages[n])

    jobs = run_fetch(handler, clearcompany.fetch, "0a1b2c3d-4e5f-6071-8293-a4b5c6d7e8f9")
    assert [j.external_id for j in jobs] == ["a1", "b2"]


def test_clearcompany_http_error_raises():
    def handler(request):
        return httpx.Response(503)

    with pytest.raises(AdapterError):
        run_fetch(handler, clearcompany.fetch, "0a1b2c3d-4e5f-6071-8293-a4b5c6d7e8f9")


def test_clearcompany_bad_shape_raises():
    # A wrong siteId can 200 with some other body — that's an adapter error,
    # never an empty board (which would silently decay every tracked job).
    def handler(request):
        return httpx.Response(200, json={"message": "not found"})

    with pytest.raises(AdapterError):
        run_fetch(handler, clearcompany.fetch, "0a1b2c3d-4e5f-6071-8293-a4b5c6d7e8f9")


# --- apple (jobs.apple.com hydration) ---


def test_apple_paginates_filters_and_fetches_details_only_for_matches():
    pages, detail_paths = [], []

    def handler(request):
        if request.url.path == "/en-us/search":
            assert request.url.params["team"] == "human-interface-design-DESGN-HID"
            page = request.url.params.get("page", "1")
            pages.append(page)
            name = "apple_search_p1.html" if page == "1" else "apple_search_p2.html"
            return httpx.Response(200, content=fixture_bytes(name))
        if request.url.path.startswith("/en-us/details/"):
            detail_paths.append(request.url.path)
            return httpx.Response(200, content=fixture_bytes("apple_detail.html"))
        return httpx.Response(404)

    jobs = run_fetch(handler, apple.fetch, "team=human-interface-design-DESGN-HID")
    # the PIPE Product Designer (pipeline post, not an opening) and the
    # non-matching titles never ingest
    assert [j.title for j in jobs] == ["Director, Product Design", "Senior UX Researcher"]

    assert pages == ["1", "2"]  # stopped at totalRecords, no page 3
    assert detail_paths == [  # detail fetched only for the two matches
        "/en-us/details/100001-1/director-product-design",
        "/en-us/details/100004-1/senior-ux-researcher",
    ]

    j = jobs[0]
    assert j.external_id == "100001-1"
    assert j.url == "https://jobs.apple.com/en-us/details/100001-1/director-product-design"
    assert j.location == "Cupertino, California"
    assert j.remote_type == "onsite"
    # range comes from the Pay & Benefits posting footer ("between $X and $Y")
    assert (j.salary_min, j.salary_max, j.salary_stated) == (135400, 250600, True)
    assert "Human Interface design practice" in j.description_text
    assert "leading design teams" in j.description_text  # qualifications joined
    assert "<" not in j.description_text  # HTML stripped

    assert jobs[1].remote_type == "remote"  # homeOffice hint wins


def test_apple_stops_on_repeated_page():
    pages = []

    def handler(request):
        if request.url.path == "/en-us/search":
            pages.append(request.url.params.get("page", "1"))
            # totalRecords says 5 but the site keeps serving the same page
            return httpx.Response(200, content=fixture_bytes("apple_search_p1.html"))
        return httpx.Response(200, content=fixture_bytes("apple_detail.html"))

    jobs = run_fetch(handler, apple.fetch, "team=design-DESGN")
    assert pages == ["1", "2"]  # repeated ids -> stop, no infinite loop
    assert [j.title for j in jobs] == ["Director, Product Design"]


def test_apple_http_error_raises():
    def handler(request):
        return httpx.Response(502)

    with pytest.raises(AdapterError):
        run_fetch(handler, apple.fetch, "team=design-DESGN")


def test_apple_missing_hydration_raises():
    # A frontend deploy that drops/renames the hydration blob must fail loudly
    # (failing-adapter banner), never read as an empty board.
    def handler(request):
        return httpx.Response(200, content=b"<html><body>new apple frontend</body></html>")

    with pytest.raises(AdapterError):
        run_fetch(handler, apple.fetch, "team=design-DESGN")


def test_apple_detail_missing_hydration_raises():
    def handler(request):
        if request.url.path == "/en-us/search":
            return httpx.Response(200, content=fixture_bytes("apple_search_p1.html"))
        return httpx.Response(200, content=b"<html><body>no blob</body></html>")

    with pytest.raises(AdapterError):
        run_fetch(handler, apple.fetch, "team=design-DESGN")


# --- atlassian (www.atlassian.com/endpoint/careers/listings) ---


def test_atlassian_maps_filters_and_dedupes():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        assert request.url.path == "/endpoint/careers/listings"
        return httpx.Response(200, content=fixture_bytes("atlassian_listings.json"))

    jobs = run_fetch(handler, atlassian.fetch, "www.atlassian.com")
    assert len(calls) == 1  # whole board in one call
    # 4 fixture rows: 24872 twice (feed repeats postings — deduped), one
    # non-design role (filtered), one Loom designer.
    assert [j.title for j in jobs] == [
        "Senior Design Manager, Jira AI",
        "Lead Product Designer, Loom",
    ]

    j = jobs[0]
    assert j.external_id == "24872"
    # canonical details page, never the icims apply portal (dead SPA)
    assert j.url == "https://www.atlassian.com/company/careers/details/24872"
    assert j.location == "San Francisco, United States"
    assert j.remote_type == "remote"  # "Remote - Remote" among locations
    # comp zones mined from the qualifications HTML (Zone A wins)
    assert (j.salary_min, j.salary_max, j.salary_stated) == (221400, 289050, True)
    assert "<" not in j.description_text  # HTML stripped

    # 25349 has a `compensation` field with no numbers and no range anywhere
    # else — the field must not short-circuit the (empty) description fallback.
    loom = jobs[1]
    assert (loom.salary_min, loom.salary_max, loom.salary_stated) == (None, None, False)


def test_atlassian_stale_feed_raises():
    # The join.atlassian.com lesson: a feed whose newest update is months old
    # is an abandoned snapshot serving ghost listings — fail loudly (failing
    # banner), never ingest it.
    stale = [{
        "id": 21680, "title": "Senior Design Manager, Design Technology",
        "locations": ["San Francisco - United States"],
        "overview": "<p>x</p>", "responsibilities": "<p>x</p>", "qualifications": "<p>x</p>",
        "portalJobPost": {"id": 21680, "updatedDate": "2025-11-04 09:00 PM"},
    }]

    def handler(request):
        return httpx.Response(200, json=stale)

    with pytest.raises(AdapterError):
        run_fetch(handler, atlassian.fetch, "www.atlassian.com")


def test_atlassian_unparseable_dates_stand_down():
    # If Atlassian reshapes the timestamp, the staleness guard must stand down
    # (defense-in-depth only), not false-fail a healthy board.
    rows = [{
        "id": 1, "title": "Product Designer",
        "locations": ["Remote - Remote"],
        "overview": "<p>x</p>", "responsibilities": "", "qualifications": "",
        "portalJobPost": {"id": 1, "updatedDate": "2026-07-21T10:05:00Z"},
    }]

    def handler(request):
        return httpx.Response(200, json=rows)

    jobs = run_fetch(handler, atlassian.fetch, "www.atlassian.com")
    assert [j.title for j in jobs] == ["Product Designer"]
    assert jobs[0].location == "Remote"
    assert jobs[0].remote_type == "remote"


def test_atlassian_http_error_raises():
    def handler(request):
        return httpx.Response(503)

    with pytest.raises(AdapterError):
        run_fetch(handler, atlassian.fetch, "www.atlassian.com")


def test_atlassian_bad_shape_raises():
    # A moved endpoint can 200 with HTML/JSON noise, or an empty list —
    # Atlassian always has openings, so both are shape failures, never a
    # quiet board (which would silently decay every tracked job).
    def handler(request):
        return httpx.Response(200, json={"message": "not found"})

    with pytest.raises(AdapterError):
        run_fetch(handler, atlassian.fetch, "www.atlassian.com")

    def handler_empty(request):
        return httpx.Response(200, json=[])

    with pytest.raises(AdapterError):
        run_fetch(handler_empty, atlassian.fetch, "www.atlassian.com")


# --- recruitee ---


def test_recruitee_maps_and_filters():
    def handler(request):
        assert request.url.path == "/api/offers/"
        return httpx.Response(200, content=fixture_bytes("recruitee_offers.json"))

    jobs = run_fetch(handler, recruitee.fetch, "exampleco")
    # design roles kept; the risk officer is filtered out
    assert [j.title for j in jobs] == ["Director of Product Design", "UX Researcher"]

    j = jobs[0]
    assert j.external_id == "2704001"
    assert j.url == "https://careers.exampleco.com/o/director-of-product-design"
    assert j.location == "Remote, United States"
    assert j.remote_type == "remote"  # structured `remote` boolean
    # description + the separate `requirements` block both feed the JD text
    assert "Lead our design org" in j.description_text
    assert "10+ years in product design" in j.description_text
    assert "<" not in j.description_text
    assert (j.salary_min, j.salary_max, j.salary_stated) == (180000, 220000, True)

    # hybrid boolean maps even when the location text doesn't say so
    assert jobs[1].remote_type == "hybrid"
    assert (jobs[1].salary_min, jobs[1].salary_max, jobs[1].salary_stated) == (
        None, None, False,
    )


def test_recruitee_http_error_raises():
    def handler(request):
        return httpx.Response(404, json={"error": "Not Found"})

    with pytest.raises(AdapterError):
        run_fetch(handler, recruitee.fetch, "exampleco")


# --- workable ---


def test_workable_maps_and_filters():
    def handler(request):
        assert request.url.path == "/api/v1/widget/accounts/exampleco"
        assert request.url.params.get("details") == "true"
        return httpx.Response(200, content=fixture_bytes("workable_widget.json"))

    jobs = run_fetch(handler, workable.fetch, "exampleco")
    # the designer is kept; the business director is filtered out
    assert [j.title for j in jobs] == ["Senior Product Designer"]

    j = jobs[0]
    assert j.external_id == "A755C605B8"
    assert j.url == "https://apply.workable.com/j/A755C605B8"
    # empty city/state fall out of the joined location
    assert j.location == "United States"
    assert j.remote_type == "remote"  # telecommuting flag
    assert "Design end to end" in j.description_text
    assert "<" not in j.description_text
    assert (j.salary_min, j.salary_max, j.salary_stated) == (150000, 190000, True)


def test_workable_http_error_raises():
    def handler(request):
        return httpx.Response(503)

    with pytest.raises(AdapterError):
        run_fetch(handler, workable.fetch, "exampleco")


# --- rippling ---


def test_rippling_two_phase_and_unlisted_skip():
    """List carries no descriptions; title-filter the list, fetch details only
    for matches, and skip details flagged unlistedFromSearch."""
    listed_uuid = "65d89c21-65ea-4259-bb9b-db41dcb007d3"
    unlisted_uuid = "77e90d32-76fb-5360-cc0c-ec52edc118e4"
    detail_calls = []

    def handler(request):
        path = request.url.path
        if path == "/platform/api/ats/v1/board/exampleco/jobs":
            return httpx.Response(200, content=fixture_bytes("rippling_list.json"))
        if path.endswith(f"/jobs/{listed_uuid}"):
            detail_calls.append(path)
            return httpx.Response(200, content=fixture_bytes("rippling_detail.json"))
        if path.endswith(f"/jobs/{unlisted_uuid}"):
            detail_calls.append(path)
            return httpx.Response(200, json={
                "uuid": unlisted_uuid, "name": "Design Systems Lead",
                "unlistedFromSearch": True,
                "workLocations": ["New York, NY"],
                "description": {"company": "<p>x</p>", "role": "<p>y</p>"},
            })
        return httpx.Response(404)

    jobs = run_fetch(handler, rippling.fetch, "exampleco")
    # two titles matched -> two detail fetches; the construction manager got none
    assert len(detail_calls) == 2
    # the unlisted match is dropped after its detail read
    assert [j.title for j in jobs] == ["Principal UX Designer"]

    j = jobs[0]
    assert j.external_id == listed_uuid
    assert j.url == f"https://ats.rippling.com/exampleco/jobs/{listed_uuid}"
    assert j.location == "Remote (United States)"  # detail workLocations
    assert j.remote_type == "remote"
    # description dict sections joined: company blurb then role
    assert "ExampleCo builds infrastructure" in j.description_text
    assert "Own the design system" in j.description_text
    assert "<" not in j.description_text
    assert (j.salary_min, j.salary_max, j.salary_stated) == (170000, 210000, True)


def test_rippling_http_error_raises():
    def handler(request):
        return httpx.Response(502)

    with pytest.raises(AdapterError):
        run_fetch(handler, rippling.fetch, "exampleco")
