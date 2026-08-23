"""Pure-function tests for ATS signature extraction and slug guessing.

No HTTP, no DB (per testing rules: adapters/detection never hit live
endpoints in tests).
"""

import json

from jshq.ats import patterns as p


def test_greenhouse_board_url():
    assert p.extract_ats_candidates("https://boards.greenhouse.io/exampleco") == [
        (p.GREENHOUSE, "exampleco")
    ]


def test_greenhouse_embed_and_api():
    html = """
      <iframe src="https://boards.greenhouse.io/embed/job_board?for=acmeco&b=1"></iframe>
      <script src="https://boards-api.greenhouse.io/v1/boards/acmeco/jobs"></script>
    """
    assert p.extract_ats_candidates(html) == [(p.GREENHOUSE, "acmeco")]


def test_greenhouse_job_boards_domain():
    assert p.extract_ats_candidates("https://job-boards.greenhouse.io/acmeco") == [
        (p.GREENHOUSE, "acmeco")
    ]


def test_greenhouse_embedded_board_token_var():
    # A branded careers page whose only greenhouse signal is a JS/JSON var
    # (Next.js public-env style) — the board URL redirects to this same page.
    html = '<script>window.__ENV={"PUBLIC_GREENHOUSE_BOARD":"acmeco","X":1}</script>'
    assert p.extract_ats_candidates(html) == [(p.GREENHOUSE, "acmeco")]


def test_greenhouse_board_token_var_camel_and_assign():
    for html in (
        'greenhouseBoard: "acmeco"',
        'const GREENHOUSE_BOARD_TOKEN = "acmeco";',
        '"greenhouse_board":"acmeco"',
    ):
        assert p.extract_ats_candidates(html) == [(p.GREENHOUSE, "acmeco")], html


def test_greenhouse_board_prose_does_not_match():
    # "greenhouse board" with a space is not the var form and must not match.
    assert p.extract_ats_candidates("We use a greenhouse board: it is great") == []


def test_lever():
    html = '<a href="https://jobs.lever.co/exampleco/12345-abc">Open roles</a>'
    assert p.extract_ats_candidates(html) == [(p.LEVER, "exampleco")]


def test_lever_api_url():
    assert p.extract_ats_candidates("https://api.lever.co/v0/postings/acmeco?mode=json") == [
        (p.LEVER, "acmeco")
    ]


def test_ashby():
    html = '<a href="https://jobs.ashbyhq.com/exampleco">Careers</a>'
    assert p.extract_ats_candidates(html) == [(p.ASHBY, "exampleco")]


def test_smartrecruiters():
    html = '<a href="https://careers.smartrecruiters.com/Exampleco">Jobs at ExampleCo</a>'
    assert p.extract_ats_candidates(html) == [(p.SMARTRECRUITERS, "Exampleco")]


def test_workday_with_locale_and_site():
    url = "https://exampleco.wd1.myworkdayjobs.com/en-US/ExamplecoCareers"
    assert p.extract_ats_candidates(url) == [(p.WORKDAY, "exampleco.wd1/ExamplecoCareers")]


def test_workday_without_site_is_skipped():
    # Tenant alone can't address the CXS endpoint -> not a usable candidate.
    assert p.extract_ats_candidates("https://acmeco.wd5.myworkdayjobs.com") == []


def test_workday_slug_roundtrip():
    slug = p.workday_slug("acmeco", "wd1", "AcmecoJobs")
    assert slug == "acmeco.wd1/AcmecoJobs"
    assert p.split_workday_slug(slug) == ("acmeco", "wd1", "AcmecoJobs")
    assert p.workday_cxs_url(slug) == (
        "https://acmeco.wd1.myworkdayjobs.com/wday/cxs/acmeco/AcmecoJobs/jobs"
    )


def test_oracle_hcm_careers_html():
    # The branded careers page embeds the Oracle pod host + siteNumber.
    html = (
        '<script>window.cfg = {"ceUrl":'
        '"https://exco.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/requisitions"};'
        "</script>"
    )
    assert p.extract_ats_candidates(html) == [
        (p.ORACLE_HCM, "exco.fa.us2.oraclecloud.com/CX")
    ]


def test_oracle_hcm_slug_and_urls():
    slug = p.oracle_slug("exco.fa.us2.oraclecloud.com", "CX")
    assert slug == "exco.fa.us2.oraclecloud.com/CX"
    assert p.split_oracle_slug(slug) == ("exco.fa.us2.oraclecloud.com", "CX")
    assert p.oracle_job_url(slug, "12345") == (
        "https://exco.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/job/12345/"
    )
    list_url = p.oracle_list_url(slug, 200, 0)
    assert "recruitingCEJobRequisitions" in list_url
    assert "siteNumber=CX,limit=200,offset=0" in list_url
    assert 'Id="12345"' in p.oracle_detail_url(slug, "12345")


def test_icims_page_slug_from_jibe_html():
    # The cid is in the page's window._jibe bootstrap; the API host is the
    # page's own host, recovered from the final URL (not the page text).
    html = '<script>window._jibe = {"cid":"exampleco"};</script>'
    assert p.icims_page_slug("https://careers.exampleco.com/careers-home/", html) == (
        "careers.exampleco.com/exampleco"
    )


def test_icims_page_slug_jibecdn_fallback():
    html = '<link href="https://cms.jibecdn.com/prod/acmeco/app.css">'
    assert p.icims_page_slug("https://join.acmeco.com/", html) == "join.acmeco.com/acmeco"


def test_icims_page_slug_none_without_fingerprint():
    assert p.icims_page_slug("https://careers.exampleco.com/", "<html>no jibe here</html>") is None
    # A fingerprint with no final URL yields no host -> no usable slug.
    assert p.icims_page_slug(None, '<script>window._jibe = {"cid":"exampleco"}</script>') is None


def test_icims_slug_and_urls():
    slug = p.icims_slug("careers.exampleco.com", "exampleco")
    assert slug == "careers.exampleco.com/exampleco"
    assert p.split_icims_slug(slug) == ("careers.exampleco.com", "exampleco")
    assert p.icims_list_url(slug, 1, 100) == (
        "https://careers.exampleco.com/api/jobs?page=1&limit=100"
    )


def test_dedupes_and_preserves_order():
    html = """
      https://boards.greenhouse.io/exampleco
      https://boards.greenhouse.io/exampleco
      https://jobs.lever.co/other
    """
    assert p.extract_ats_candidates(html) == [
        (p.GREENHOUSE, "exampleco"),
        (p.LEVER, "other"),
    ]


def test_stopword_slugs_ignored():
    assert p.extract_ats_candidates("https://boards.greenhouse.io/embed/x") == []


def test_no_match():
    assert p.extract_ats_candidates("<html><body>We hire via email.</body></html>") == []


def test_candidate_slugs_basic():
    slugs = p.candidate_slugs("Acme Academy")
    assert slugs[0] == "acmeacademy"
    assert "acme-academy" in slugs
    assert "acme" in slugs


def test_candidate_slugs_noise_words():
    slugs = p.candidate_slugs("Exampleco of America")
    assert "exampleco" in slugs
    assert "examplecoofamerica" in slugs


def test_candidate_slugs_unicode():
    assert p.candidate_slugs("Ørbital")[0] == "orbital"


def test_host_slug_candidates_strips_www_and_tld():
    assert p.host_slug_candidates("https://www.exampleco.com/careers") == ["exampleco"]
    assert p.host_slug_candidates("https://exampleco.com") == ["exampleco"]
    assert p.host_slug_candidates("exampleco.com/careers") == ["exampleco"]  # scheme-less


def test_host_slug_candidates_drops_careers_subdomain():
    # careers.acmeco.io -> the registrable label "acmeco", not "careers".
    assert p.host_slug_candidates("https://careers.acmeco.io") == ["acmeco"]
    assert p.host_slug_candidates("https://jobs.example.io/roles") == ["example"]


def test_host_slug_candidates_hyphenated_and_empty():
    slugs = p.host_slug_candidates("https://www.acme-corp.com")
    assert "acmecorp" in slugs and "acme-corp" in slugs
    assert p.host_slug_candidates("") == []
    assert p.host_slug_candidates(None) == []


def test_public_board_url():
    assert p.public_board_url(p.GREENHOUSE, "exampleco") == "https://boards.greenhouse.io/exampleco"
    assert p.public_board_url(p.WORKDAY, "acmeco.wd1/AcmecoJobs") == (
        "https://acmeco.wd1.myworkdayjobs.com/AcmecoJobs"
    )
    assert p.public_board_url(p.ORACLE_HCM, "exco.fa.us2.oraclecloud.com/CX") == (
        "https://exco.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX"
    )
    assert p.public_board_url(p.ICIMS, "careers.exampleco.com/exampleco") == "https://careers.exampleco.com"


def test_clearcompany_widget_embed():
    html = (
        '<script type=text/javascript src="https://careers-content.clearcompany.com'
        '/js/v1/career-site.js?siteId=0a1b2c3d-4e5f-6071-8293-a4b5c6d7e8f9"></script>'
    )
    assert p.extract_ats_candidates(html) == [
        (p.CLEARCOMPANY, "0a1b2c3d-4e5f-6071-8293-a4b5c6d7e8f9")
    ]


def test_clearcompany_api_url():
    assert p.extract_ats_candidates(
        "https://careers-api.clearcompany.com/v1/0a1b2c3d-4e5f-6071-8293-a4b5c6d7e8f9"
    ) == [(p.CLEARCOMPANY, "0a1b2c3d-4e5f-6071-8293-a4b5c6d7e8f9")]


def test_clearcompany_requires_uuid_shape():
    # API paths whose segment is a word (settings/chatbot/insights) must never
    # read as slugs, and a bare company subdomain is not addressable evidence.
    assert p.extract_ats_candidates(
        "https://careers-api.clearcompany.com/v1/settings/"
        "0a1b2c3d-4e5f-6071-8293-a4b5c6d7e8f9"
    ) == []
    assert p.extract_ats_candidates("https://acmeco.clearcompany.com/careers") == []


def test_clearcompany_list_url_pagination():
    base = "https://careers-api.clearcompany.com/v1/abc"
    assert p.clearcompany_list_url("abc") == base
    assert p.clearcompany_list_url("abc", 1) == base
    assert p.clearcompany_list_url("abc", 3) == base + "?p=3"


def test_apple_search_url_slug():
    assert p.extract_ats_candidates(
        "https://jobs.apple.com/en-us/search?team=human-interface-design-DESGN-HID"
    ) == [(p.APPLE, "team=human-interface-design-DESGN-HID")]


def test_apple_slug_drops_page_and_requires_scope():
    # page/sort are refresh noise, stripped so fetches always start at page 1.
    assert p.extract_ats_candidates(
        "https://jobs.apple.com/en-us/search?team=design-DESGN&page=3&sort=newest"
    ) == [(p.APPLE, "team=design-DESGN")]
    # An unscoped search would ingest Apple's entire board — never a candidate.
    assert p.extract_ats_candidates("https://jobs.apple.com/en-us/search?page=2") == []


def test_apple_urls():
    slug = "team=human-interface-design-DESGN-HID"
    assert p.apple_list_url(slug) == f"https://jobs.apple.com/en-us/search?{slug}"
    assert p.apple_list_url(slug, 3) == f"https://jobs.apple.com/en-us/search?{slug}&page=3"
    assert p.apple_detail_url("200612345-0001", "product-designer") == (
        "https://jobs.apple.com/en-us/details/200612345-0001/product-designer"
    )
    assert p.public_board_url(p.APPLE, slug) == f"https://jobs.apple.com/en-us/search?{slug}"


def test_apple_hydration_data():
    blob = json.dumps(json.dumps({"loaderData": {"search": {"totalRecords": 2}}}))
    html = f"<script>window.__staticRouterHydrationData = JSON.parse({blob});</script>"
    assert p.apple_hydration_data(html) == {"loaderData": {"search": {"totalRecords": 2}}}
    assert p.apple_hydration_data("<html>no hydration here</html>") is None
    assert p.apple_hydration_data(
        'window.__staticRouterHydrationData = JSON.parse("not json");'
    ) is None


def test_atlassian_signature_and_urls():
    # The careers_url itself is the signature; slug = the matched host.
    assert p.extract_ats_candidates(
        "https://www.atlassian.com/company/careers/all-jobs"
    ) == [(p.ATLASSIAN, "www.atlassian.com")]
    # join.atlassian.com must NOT match — its Jibe feed is an abandoned
    # snapshot (frozen 2025-11) the icims adapter would happily ingest.
    assert p.extract_ats_candidates("https://join.atlassian.com") == []
    assert p.atlassian_list_url("www.atlassian.com") == (
        "https://www.atlassian.com/endpoint/careers/listings"
    )
    assert p.atlassian_job_url("24872") == (
        "https://www.atlassian.com/company/careers/details/24872"
    )
    assert p.public_board_url(p.ATLASSIAN, "www.atlassian.com") == (
        "https://www.atlassian.com/company/careers/all-jobs"
    )


def test_recruitee_subdomain():
    assert p.extract_ats_candidates("https://exampleco.recruitee.com/api/offers/") == [
        (p.RECRUITEE, "exampleco")
    ]
    assert p.public_board_url(p.RECRUITEE, "exampleco") == "https://exampleco.recruitee.com"


def test_workable_board_and_widget_urls():
    assert p.extract_ats_candidates("https://apply.workable.com/exampleco/") == [
        (p.WORKABLE, "exampleco")
    ]
    # the widget-API form yields the account slug, not the "api" path prefix
    assert p.extract_ats_candidates(
        "https://apply.workable.com/api/v1/widget/accounts/exampleco?details=true"
    ) == [(p.WORKABLE, "exampleco")]
    # job short-links carry a shortcode, not a slug — "j" is a stopword and
    # the pattern never reaches the second path segment
    assert p.extract_ats_candidates("https://apply.workable.com/j/A755C605B8") == []
    assert p.public_board_url(p.WORKABLE, "exampleco") == "https://apply.workable.com/exampleco"


def test_rippling_board_and_api_urls():
    assert p.extract_ats_candidates(
        "https://ats.rippling.com/exampleco/jobs/65d89c21-65ea-4259-bb9b-db41dcb007d3"
    ) == [(p.RIPPLING, "exampleco")]
    assert p.extract_ats_candidates(
        "https://api.rippling.com/platform/api/ats/v1/board/exampleco/jobs"
    ) == [(p.RIPPLING, "exampleco")]
    assert p.public_board_url(p.RIPPLING, "exampleco") == "https://ats.rippling.com/exampleco/jobs"


def test_rippling_urls():
    assert p.rippling_list_url("exampleco") == (
        "https://api.rippling.com/platform/api/ats/v1/board/exampleco/jobs"
    )
    assert p.rippling_detail_url("exampleco", "65d89c21") == (
        "https://api.rippling.com/platform/api/ats/v1/board/exampleco/jobs/65d89c21"
    )


def test_name_matches_full_and_partial():
    # every core token required: a stranger's board that shares one word fails
    assert p.name_matches("Marigold Workshop", "About Marigold Workshop: we make media.")
    assert not p.name_matches(
        "Marigold Workshop",
        "Marigold is a pet-insurance marketplace. Provider Success - Senior Account Manager.",
    )
    assert not p.name_matches("Marigold Workshop", None)
    assert not p.name_matches("Marigold Workshop", "")


def test_name_matches_concatenated_and_noise():
    # concatenated spelling of a multi-word name counts
    assert p.name_matches("Marigold Workshop", "Careers at MarigoldWorkshop")
    # noise words don't have to appear
    assert p.name_matches("Nintendo of America", "Nintendo builds consoles.")
    # single-token names never match as bare substrings of longer words
    assert not p.name_matches("Box", "Grab your toolbox and get building.")
    assert p.name_matches("Box", "Box is a cloud content platform.")


def test_name_matches_unicode():
    assert p.name_matches("Ørsted", "Orsted develops offshore wind farms.")
