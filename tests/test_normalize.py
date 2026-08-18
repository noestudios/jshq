"""Pure-function tests for app.ats.normalize — no network, no DB."""

from jshq.ats.normalize import (
    NormalizedJob,
    classify_remote,
    compile_title_filter,
    derive_level_band,
    extract_salary,
    make_dedupe_key,
    norm,
    strip_html,
)


def _job(**overrides) -> NormalizedJob:
    base = dict(
        external_id=None, title="Product Designer", url=None, location=None,
        remote_type="unknown", salary_min=None, salary_max=None,
        salary_stated=False, description_text=None,
    )
    base.update(overrides)
    return NormalizedJob(**base)


# --- dedupe keys ---


def test_dedupe_key_prefers_external_id():
    assert make_dedupe_key(7, _job(external_id="12345")) == "7:12345"


def test_dedupe_key_falls_back_to_title_location():
    j = _job(title="Senior Product Designer", location="New York, NY")
    assert make_dedupe_key(7, j) == "7:senior product designer|new york ny"


def test_dedupe_key_normalization_equivalence():
    a = _job(title="Sr. Product  Designer!", location="Remote — US")
    b = _job(title="sr product designer", location="remote  us")
    assert make_dedupe_key(3, a) == make_dedupe_key(3, b)


def test_norm_strips_punct_and_collapses_space():
    assert norm("  Head of Design, Web & Mobile  ") == "head of design web mobile"


# --- title filter ---


def test_title_filter_matches_keywords():
    f = compile_title_filter(["design", "ux", "user research"])
    assert f.search("Director of Design")
    assert f.search("UX Researcher")  # case-insensitive
    assert f.search("Head of User Research")


def test_title_filter_word_boundaries():
    f = compile_title_filter(["design", "ux"])
    assert not f.search("Designated Broker")
    assert not f.search("Flux Engineer")
    assert not f.search("Software Engineer")


def test_title_filter_punctuation_edged_keywords():
    # \b anchors on a word char INSIDE the keyword, so "c++" or ".net" could
    # never match anything — the keyword saved fine and silently never gated.
    # User-typed wizard keywords make this a real input class.
    f = compile_title_filter(["c++", ".net"])
    assert f.search("Senior C++ Engineer")
    assert f.search(".NET Developer")
    assert not f.search("ASP.NETX Developer")
    ex = compile_title_filter(["engineer"], exclude=["c++"])
    assert not ex.search("C++ Engineer")


def test_title_filter_exclude_wins_over_include():
    f = compile_title_filter(["design", "researcher"], exclude=["machine learning"])
    assert f.search("Director of Design")
    assert f.search("UX Researcher")
    assert not f.search("Machine Learning Researcher")
    assert not f.search("Senior MACHINE LEARNING Designer")  # case-insensitive


def test_title_filter_empty_exclude_back_compat():
    f = compile_title_filter(["design"])  # one-arg call form unchanged
    assert f.search("Design Lead")
    assert not f.search("Sales Rep")


def test_title_filter_no_include_ingests_everything():
    # Phase 5b: the seed ships empty, and empty means NO GATE. The old
    # "nothing ingests" reading presented as a healthy green refresh with
    # zero jobs stored on every fresh install.
    f = compile_title_filter([])
    assert f.search("Design Lead")
    assert f.search("Registered Nurse")


def test_title_filter_no_include_still_honors_excludes():
    f = compile_title_filter([], exclude=["machine learning"])
    assert f.search("Registered Nurse")
    assert not f.search("Machine Learning Engineer")


# --- salary ---


def test_salary_comma_range():
    assert extract_salary("Pay range: $150,000 - $190,000 annually") == (150000, 190000, True)


def test_salary_k_range_en_dash():
    assert extract_salary("Comp: $150K–$190k DOE") == (150000, 190000, True)


def test_salary_to_range():
    assert extract_salary("$120,000 to $160,000 plus equity") == (120000, 160000, True)


def test_salary_and_range():
    # Apple's Pay & Benefits phrasing.
    assert extract_salary(
        "The base pay range for this role is between $135,400 and $250,600"
    ) == (135400, 250600, True)


def test_salary_single_value():
    assert extract_salary("Base salary of $145,000 per year") == (145000, 145000, True)


def test_salary_range_with_cents():
    # iCIMS/Jibe tags8 form — the .00 cents must not truncate the range.
    assert extract_salary("$150,000.00 - $190,000.00 Yearly") == (150000, 190000, True)
    assert extract_salary("$18.40 - $28.00 Hourly") == (None, None, False)


def test_salary_plain_ungrouped_range():
    # Greenhouse plain-ungrouped form (caught live, 2026-08): no comma grouping at all.
    assert extract_salary(
        "The salary range for this position is $185000 - $242000 annually."
    ) == (185000, 242000, True)
    assert extract_salary("$185000.00 - $242000.00 Yearly") == (185000, 242000, True)
    assert extract_salary("Base of $145000 per year") == (145000, 145000, True)


def test_salary_plain_ungrouped_guards():
    # A 7-digit figure must not truncate-match its first six digits.
    assert extract_salary("$1000000 - $2000000 grant pool") == (None, None, False)
    # Sanity bounds and the hourly check still apply to plain amounts.
    assert extract_salary("$12000 - $18000 stipend") == (None, None, False)
    assert extract_salary("Rate: $45000 per hour nonsense") == (None, None, False)


def test_salary_hourly_rejected():
    assert extract_salary("Pay: $50,000 - $60,000 per hour") == (None, None, False)
    assert extract_salary("Rate: $45,000 per hour equivalents nonsense") == (None, None, False)


def test_salary_out_of_bounds_rejected():
    assert extract_salary("$12,000 - $18,000 stipend") == (None, None, False)


def test_salary_absent():
    assert extract_salary("Competitive compensation and benefits") == (None, None, False)
    assert extract_salary(None) == (None, None, False)


# --- remote classification ---


def test_classify_remote_hint_wins():
    assert classify_remote("New York, NY", hint="remote") == "remote"
    assert classify_remote(None, hint="hybrid") == "hybrid"


def test_classify_remote_from_location():
    assert classify_remote("Remote - US") == "remote"
    assert classify_remote("Hybrid, London") == "hybrid"
    assert classify_remote("San Francisco, CA") == "onsite"
    assert classify_remote(None) == "unknown"
    assert classify_remote("") == "unknown"


def test_classify_remote_country_scope_is_remote():
    # A bare country/region names no office — the ATS convention for a
    # location-flexible posting (Greenhouse remote-US roles say just
    # "United States"; caught live, 2026-07). Tier-1 still decides scope:
    # "Canada" classifies remote here and fails the US-scope check there.
    assert classify_remote("United States") == "remote"
    assert classify_remote("United States ") == "remote"  # trailing space, as seen live
    assert classify_remote("Canada") == "remote"
    assert classify_remote("Global") == "remote"
    # A country plus anything more specific is still an office location.
    assert classify_remote("Chicago, IL, United States") == "onsite"
    assert classify_remote("Bengaluru, India") == "onsite"


# --- level band ---


def test_level_bands():
    assert derive_level_band("VP of Design") == "vp_plus"
    assert derive_level_band("Vice President, UX") == "vp_plus"
    assert derive_level_band("Chief Design Officer") == "vp_plus"
    assert derive_level_band("Senior Director, Product Design") == "senior_director"
    assert derive_level_band("Sr. Director of UX") == "senior_director"
    assert derive_level_band("Director of Product Design") == "director"
    assert derive_level_band("Head of Design") == "director"
    assert derive_level_band("Senior Manager, Design Systems") == "senior_manager"
    assert derive_level_band("Design Manager") == "manager"
    assert derive_level_band("Design Lead") == "manager"
    assert derive_level_band("Senior Product Designer") == "ic"
    assert derive_level_band("UX Researcher") == "ic"


def test_junior_band():
    # Band caps (2026-08): program designations override seniority words;
    # junior/jr/associate only catch titles no seniority pattern claimed.
    assert derive_level_band("Product Design Intern") == "junior"
    assert derive_level_band("Design Internship, Summer 2027") == "junior"
    assert derive_level_band("UX Design Co-op") == "junior"
    assert derive_level_band("Design Apprentice") == "junior"
    assert derive_level_band("Design Manager Intern") == "junior"  # program wins
    assert derive_level_band("Junior Product Designer") == "junior"
    assert derive_level_band("Jr. Designer") == "junior"
    assert derive_level_band("Associate Product Designer") == "junior"
    assert derive_level_band("Associate Creative Director") == "director"  # ACD is senior
    assert derive_level_band("Associate Design Manager") == "manager"
    assert derive_level_band("Product Designer") == "ic"  # default untouched


def test_level_band_explicit_ic_designation_wins():
    # An explicit IC phrase overrides the seniority words around it (2026-07
    # IC hard-cap verdict — a "Director (Individual Contributor)" title seen live).
    assert derive_level_band("Product Design Director (Individual Contributor)") == "ic"
    assert derive_level_band("Individual Contributor, Design Lead") == "ic"
    assert derive_level_band("Staff Designer — individual-contributor track") == "ic"
    assert derive_level_band("Director of Design") == "director"  # no phrase, unchanged


# --- strip_html ---


def test_strip_html_plain_tags():
    assert strip_html("<p>Hello <strong>world</strong></p><p>Second</p>") == "Hello world\n\nSecond"


def test_strip_html_entity_escaped_greenhouse_style():
    escaped = "&lt;p&gt;We are hiring a &amp;quot;designer&amp;quot;.&lt;/p&gt;"
    assert strip_html(escaped) == 'We are hiring a "designer".'


def test_strip_html_lists_become_lines():
    text = strip_html("<ul><li>One</li><li>Two</li></ul>")
    assert "One" in text and "Two" in text
    assert "OneTwo" not in text


def test_strip_html_empty():
    assert strip_html(None) is None
    assert strip_html("") is None
    assert strip_html("<p></p>") is None
