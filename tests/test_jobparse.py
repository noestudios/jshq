"""URL → job-posting parser (Add-job prefill). No network: JSON-LD extraction is
a pure function, and the LinkedIn/bad-URL refusals happen before any fetch. The
endpoint is exercised with parse_job_url monkeypatched."""

import asyncio

import pytest

from jshq import jobparse


def test_from_json_ld_extracts_fields():
    html = """
    <html><head>
    <script type="application/ld+json">
    {"@context":"https://schema.org","@type":"JobPosting",
     "title":"Senior Product Designer",
     "description":"<p>Design <b>things</b> &amp; ship.</p><ul><li>Lead</li></ul>",
     "jobLocationType":"TELECOMMUTE",
     "jobLocation":{"@type":"Place","address":{"@type":"PostalAddress",
        "addressLocality":"Evanston","addressRegion":"IL"}},
     "baseSalary":{"@type":"MonetaryAmount","currency":"USD",
        "value":{"@type":"QuantitativeValue","minValue":150000,"maxValue":190000,"unitText":"YEAR"}}}
    </script></head><body>ignored</body></html>"""
    out = jobparse._from_json_ld(html)
    assert out["title"] == "Senior Product Designer"
    assert out["remote_type"] == "remote"  # TELECOMMUTE
    assert out["location"] == "Evanston, IL"
    assert out["salary_min"] == 150000 and out["salary_max"] == 190000
    assert "Design things & ship" in out["description_text"]  # tags stripped, entities unescaped
    assert out["source"] == "json-ld"


def test_from_json_ld_handles_graph_list_type_and_hourly_salary():
    html = """
    <script type="application/ld+json">
    {"@context":"https://schema.org","@graph":[
      {"@type":"Organization","name":"Exampleco"},
      {"@type":["JobPosting"],"title":"UX Researcher","description":"Research things.",
       "baseSalary":{"@type":"MonetaryAmount",
         "value":{"@type":"QuantitativeValue","value":50,"unitText":"HOUR"}}}
    ]}
    </script>"""
    out = jobparse._from_json_ld(html)
    assert out["title"] == "UX Researcher"  # found inside @graph, @type as a list
    assert out["salary_min"] == out["salary_max"] == 50 * 2080  # hourly → annualized


def test_from_json_ld_none_when_no_jobposting():
    assert jobparse._from_json_ld('<script type="application/ld+json">{"@type":"WebPage"}</script>') is None
    assert jobparse._from_json_ld("<html>no structured data here</html>") is None


def test_parse_url_rejects_linkedin_and_non_urls():
    with pytest.raises(jobparse.JobParseError, match="LinkedIn"):
        asyncio.run(jobparse.parse_job_url("https://www.linkedin.com/jobs/view/123"))
    with pytest.raises(jobparse.JobParseError):
        asyncio.run(jobparse.parse_job_url("not a url"))
    with pytest.raises(jobparse.JobParseError):
        asyncio.run(jobparse.parse_job_url("ftp://example.com/x"))


def _fake_fetch(monkeypatch, html):
    """Stand in for the httpx GET so parse_job_url runs offline."""

    class FakeResp:
        status_code = 200
        text = html

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, _url):
            return FakeResp()

    monkeypatch.setattr(jobparse.httpx, "AsyncClient", FakeClient)


def test_parse_url_without_key_says_add_a_key_not_javascript(monkeypatch):
    """A page with no JSON-LD and no key must blame the missing key, not the
    JavaScript-render fallback that would otherwise misdiagnose it. The autouse
    fixture removed the key and no client is injected, so the LLM can't run."""
    from jshq import apikey

    _fake_fetch(monkeypatch, "<html><body>a careers page, no structured data</body></html>")
    out = asyncio.run(jobparse.parse_job_url("https://example.com/careers/1"))
    assert out["title"] is None
    assert "Settings" in out["detail"]
    assert "JavaScript" not in out["detail"]
    assert out["detail"].startswith(apikey.MISSING_MESSAGE)


def test_parse_url_json_ld_works_without_a_key(monkeypatch):
    """JSON-LD extraction needs no model, so a keyless install still prefills from
    a well-marked-up posting."""
    html = (
        '<script type="application/ld+json">'
        '{"@context":"https://schema.org","@type":"JobPosting",'
        '"title":"Staff Designer","description":"Design."}</script>'
    )
    _fake_fetch(monkeypatch, html)
    out = asyncio.run(jobparse.parse_job_url("https://example.com/careers/2"))
    assert out["title"] == "Staff Designer"
    assert out["source"] == "json-ld"


def test_parse_url_llm_failure_degrades_not_500(monkeypatch):
    """A keyed Haiku pass that raises (bad key, rate limit, dropped connection,
    odd response) must return the manual-paste fallback, never propagate an
    exception that would 500 the endpoint -- the never-crashes invariant covers
    a present-but-failing key, and every sibling AI endpoint wraps its call."""

    class BoomClient:
        class messages:
            @staticmethod
            async def create(*a, **k):
                raise RuntimeError("upstream 529 overloaded")

    _fake_fetch(monkeypatch, "<html><body>a careers page, no structured data</body></html>")
    out = asyncio.run(
        jobparse.parse_job_url("https://example.com/careers/9", client=BoomClient())
    )
    assert out["title"] is None
    assert "paste" in out["detail"].lower()


def test_parse_url_endpoint_returns_fields(client, monkeypatch):
    async def fake_parse(url, *, client=None, model=None):
        return {
            "title": "Staff Designer", "location": "Remote", "remote_type": "remote",
            "salary_min": None, "salary_max": None, "description_text": "JD body",
            "source": "json-ld",
        }

    monkeypatch.setattr("jshq.jobparse.parse_job_url", fake_parse)
    r = client.post("/api/jobs/parse-url", json={"url": "https://careers.example.com/1"})
    assert r.status_code == 200
    body = r.json()
    assert body["title"] == "Staff Designer" and body["source"] == "json-ld"


def test_parse_url_endpoint_422_on_parse_error(client, monkeypatch):
    async def boom(url, *, client=None, model=None):
        raise jobparse.JobParseError("LinkedIn postings can't be auto-pulled.")

    monkeypatch.setattr("jshq.jobparse.parse_job_url", boom)
    r = client.post("/api/jobs/parse-url", json={"url": "https://linkedin.com/x"})
    assert r.status_code == 422
    assert "LinkedIn" in r.json()["detail"]
