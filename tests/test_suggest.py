"""suggest.py — n-gram suggestion logic, pure."""

from jshq.scoring.suggest import suggest_exclusions

INCLUDE = ["design", "researcher", "ux"]


def dismissals(*pairs):
    return [{"reason": r, "title": t} for r, t in pairs]


ML_DISMISSALS = dismissals(
    ("not my focus area", "Machine Learning Engineer"),
    ("not my focus area", "Senior Machine Learning Researcher"),
    ("not my focus area", "Machine Learning Research Scientist"),
)


def test_three_same_reason_similar_titles_suggest_bigram():
    out = suggest_exclusions(ML_DISMISSALS, INCLUDE, [], [])
    keywords = [s["keyword"] for s in out]
    assert "machine learning" in keywords
    top = next(s for s in out if s["keyword"] == "machine learning")
    assert top["count"] == 3
    assert len(top["examples"]) == 3


def test_bigram_preferred_over_component_unigrams():
    keywords = [s["keyword"] for s in suggest_exclusions(ML_DISMISSALS, INCLUDE, [], [])]
    assert "machine" not in keywords
    assert "learning" not in keywords


def test_below_threshold_suggests_nothing():
    out = suggest_exclusions(ML_DISMISSALS[:2], INCLUDE, [], [])
    assert out == []


def test_different_reasons_do_not_pool():
    mixed = dismissals(
        ("not my focus area", "Machine Learning Engineer"),
        ("wrong level", "Machine Learning Engineer II"),
        ("comp too low", "Machine Learning Researcher"),
    )
    assert suggest_exclusions(mixed, INCLUDE, [], []) == []


def test_include_keyword_collision_dropped():
    researchy = dismissals(
        ("not my focus area", "Quant Researcher"),
        ("not my focus area", "Market Researcher"),
        ("not my focus area", "AI Researcher"),
    )
    keywords = [s["keyword"] for s in suggest_exclusions(researchy, INCLUDE, [], [])]
    assert "researcher" not in keywords  # equals an include keyword


def test_existing_excludes_and_ignored_filtered():
    assert suggest_exclusions(ML_DISMISSALS, INCLUDE, ["machine learning"], []) == []
    assert suggest_exclusions(ML_DISMISSALS, INCLUDE, [], ["machine learning"]) == []


def test_company_name_never_suggested():
    # ATS titles embed the brand; three "Exampleco … Product …" dismissals share
    # both the company name and a real role word. The brand (and brand-paired
    # bigram) must be dropped while "product" still surfaces (QA pass 2).
    titles = dismissals(
        ("not my focus area", "Exampleco Product Designer"),
        ("not my focus area", "Exampleco Senior Product Designer"),
        ("not my focus area", "Exampleco Product Design Lead"),
    )
    out = suggest_exclusions(titles, [], [], [], company_names=["Exampleco Group"])
    keywords = [s["keyword"] for s in out]
    assert "exampleco" not in keywords
    assert "exampleco product" not in keywords
    assert "product" in keywords


def test_stopwords_never_suggested():
    titles = dismissals(
        ("wrong level", "Senior Designer of Things"),
        ("wrong level", "Senior Maker of Stuff"),
        ("wrong level", "Senior Builder of Items"),
    )
    keywords = [s["keyword"] for s in suggest_exclusions(titles, [], [], [])]
    assert "senior" not in keywords
    assert "of" not in keywords
