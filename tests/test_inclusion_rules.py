"""Human-readable inclusion-rules compiler (Phase 7i, decision C).

The endpoints compile rules down to title_keywords / title_exclude_keywords
(settings table) + location_allowlist (fit_criteria.md). The shared
criteria_doc fixture (conftest.py) redirects CRITERIA_PATH to a temp copy so
the seeded doc is never touched; the client fixture's DB is a fresh temp file.
"""

import json

from jshq.scoring import criteria as criteria_mod
from jshq.scoring.rules import compile_rules

_ARRAYS = ("title_keywords", "title_exclude_keywords", "location_allowlist")


def _by_source(compiled, arr, source):
    return [e["value"] for e in compiled[arr] if e["source"] == source]


def _full_manual(compiled):
    """The manual echo a well-behaved client sends back across all arrays."""
    return {arr: _by_source(compiled, arr, "manual") for arr in _ARRAYS}


# --- compile_rules (pure) ----------------------------------------------------


def test_compile_maps_targets_verbs_dedupe_and_casing():
    rules = [
        {"id": "1", "verb": "include", "target": "title", "terms": ["Brandco", "brandco", " design "]},
        {"id": "2", "verb": "exclude", "target": "title", "terms": ["intern"]},
        {"id": "3", "verb": "include", "target": "location", "terms": ["Boston", "NYC", "boston"]},
    ]
    out = compile_rules(rules)
    # title: trimmed, case preserved, deduped case-insensitively (first wins)
    assert out["title_keywords"] == ["Brandco", "design"]
    assert out["title_exclude_keywords"] == ["intern"]
    # location: lowercased to match the stored-doc convention, deduped
    assert out["location_allowlist"] == ["boston", "nyc"]


def test_compile_ignores_location_exclude_combo():
    # the model rejects it upstream, but compile must never emit for it
    out = compile_rules(
        [{"id": "x", "verb": "exclude", "target": "location", "terms": ["boston"]}]
    )
    assert out == {"title_keywords": [], "title_exclude_keywords": [], "location_allowlist": []}


# --- GET on a fresh DB: lossless reconcile -----------------------------------


def test_get_fresh_db_compiles_an_empty_gate(client, criteria_doc):
    # Phase 5b: title_keywords ships EMPTY and empty means no gate — a fresh
    # install ingests unfiltered until the wizard's field step (or a Sourcing
    # rule) writes the first include. This pin is the regression guard for the
    # flipped TitleFilter semantics: a seed sneaking back in would silently
    # re-scope every fresh install to one field.
    body = client.get("/api/inclusion-rules").json()
    assert body["rules"] == []
    assert body["compiled"]["title_keywords"] == []
    # doc-owned values (the example doc's allowlist) still surface as manual
    assert all(e["source"] == "manual" for e in body["compiled"]["location_allowlist"])


# --- round trip + provenance -------------------------------------------------


def test_rule_marks_provenance_and_rewrites_live_arrays(client, criteria_doc):
    before = client.get("/api/inclusion-rules").json()
    manual = _full_manual(before["compiled"])
    rules = [{"id": "r1", "verb": "include", "target": "title", "terms": ["brandco"]}]

    resp = client.put("/api/inclusion-rules", json={"rules": rules, "manual": manual})
    assert resp.status_code == 200
    compiled = resp.json()["compiled"]
    assert "brandco" in _by_source(compiled, "title_keywords", "rule")
    # the pre-existing seeds are preserved as manual
    assert set(manual["title_keywords"]).issubset(set(_by_source(compiled, "title_keywords", "manual")))
    # the actual settings array (what ingestion reads) reflects it
    settings_tk = client.get("/api/settings/title_keywords").json()["value"]
    assert "brandco" in settings_tk
    assert set(manual["title_keywords"]).issubset(set(settings_tk))


def test_manual_survives_recompile(client, criteria_doc):
    # add a one-off manual exclude keyword (the Advanced add-input path)
    r1 = client.put(
        "/api/inclusion-rules",
        json={"rules": [], "manual": {"title_exclude_keywords": ["sales"]}},
    )
    assert "sales" in _by_source(r1.json()["compiled"], "title_exclude_keywords", "manual")

    # author an unrelated exclude rule; the client echoes the manual it shows
    rules = [{"id": "r1", "verb": "exclude", "target": "title", "terms": ["intern"]}]
    r2 = client.put(
        "/api/inclusion-rules",
        json={"rules": rules, "manual": {"title_exclude_keywords": ["sales"]}},
    )
    compiled = r2.json()["compiled"]
    assert "intern" in _by_source(compiled, "title_exclude_keywords", "rule")
    assert "sales" in _by_source(compiled, "title_exclude_keywords", "manual")


def test_deleting_a_rule_removes_its_terms(client, criteria_doc):
    rules = [{"id": "r1", "verb": "include", "target": "title", "terms": ["brandco"]}]
    client.put("/api/inclusion-rules", json={"rules": rules, "manual": {}})
    # client drops the rule and sends no manual for it -> term gone
    after = client.put("/api/inclusion-rules", json={"rules": [], "manual": {}}).json()
    assert "brandco" not in [e["value"] for e in after["compiled"]["title_keywords"]]
    assert "brandco" not in client.get("/api/settings/title_keywords").json()["value"]


def test_rule_wins_over_manual_collision(client, criteria_doc):
    rules = [{"id": "r", "verb": "include", "target": "title", "terms": ["brandco"]}]
    resp = client.put(
        "/api/inclusion-rules",
        json={"rules": rules, "manual": {"title_keywords": ["Brandco"]}},
    )
    compiled = resp.json()["compiled"]
    brandcos = [e for e in compiled["title_keywords"] if e["value"].lower() == "brandco"]
    assert len(brandcos) == 1
    assert brandcos[0]["source"] == "rule"


# --- atomic dual-store write -------------------------------------------------


def test_atomic_writes_both_stores(client, criteria_doc):
    before = client.get("/api/inclusion-rules").json()
    manual = _full_manual(before["compiled"])
    rules = [
        {"id": "t", "verb": "include", "target": "title", "terms": ["brandco"]},
        {"id": "l", "verb": "include", "target": "location", "terms": ["Boston"]},
    ]
    resp = client.put("/api/inclusion-rules", json={"rules": rules, "manual": manual})
    assert resp.status_code == 200
    # title -> settings table
    assert "brandco" in client.get("/api/settings/title_keywords").json()["value"]
    # location -> fit_criteria.md (lowercased), and the doc still loads cleanly
    crit = client.get("/api/scoring/criteria").json()["tier1_params"]
    assert "boston" in crit["location_allowlist"]
    assert "boston" in criteria_mod.load_criteria(criteria_doc).params["location_allowlist"]


def test_other_tier1_params_untouched(client, criteria_doc):
    before = client.get("/api/scoring/criteria").json()["tier1_params"]
    manual = _full_manual(client.get("/api/inclusion-rules").json()["compiled"])
    rules = [{"id": "l", "verb": "include", "target": "location", "terms": ["boston"]}]
    client.put("/api/inclusion-rules", json={"rules": rules, "manual": manual})

    after = client.get("/api/scoring/criteria").json()["tier1_params"]
    for key in (
        "comp_floor", "comp_target", "remote_regions", "excluded_sectors",
        "company_location_overrides", "target_title_bands", "flag_title_bands",
    ):
        assert after[key] == before[key], key
    assert "boston" in after["location_allowlist"]


# --- validation --------------------------------------------------------------


def test_location_exclude_rejected_422_and_doc_untouched(client, criteria_doc):
    before = criteria_doc.read_text(encoding="utf-8")
    resp = client.put(
        "/api/inclusion-rules",
        json={
            "rules": [{"id": "x", "verb": "exclude", "target": "location", "terms": ["boston"]}],
            "manual": {},
        },
    )
    assert resp.status_code == 422
    assert "location" in json.dumps(resp.json()).lower()
    assert criteria_doc.read_text(encoding="utf-8") == before  # rejected before any write


def test_bad_verb_or_target_is_422(client, criteria_doc):
    assert client.put(
        "/api/inclusion-rules",
        json={"rules": [{"id": "x", "verb": "nope", "target": "title", "terms": ["a"]}]},
    ).status_code == 422
    assert client.put(
        "/api/inclusion-rules",
        json={"rules": [{"id": "x", "verb": "include", "target": "sector", "terms": ["a"]}]},
    ).status_code == 422


def test_empty_terms_is_422(client, criteria_doc):
    assert client.put(
        "/api/inclusion-rules",
        json={"rules": [{"id": "x", "verb": "include", "target": "title", "terms": []}]},
    ).status_code == 422
    assert client.put(
        "/api/inclusion-rules",
        json={"rules": [{"id": "x", "verb": "include", "target": "title", "terms": ["  "]}]},
    ).status_code == 422


# --- interop -----------------------------------------------------------------


def test_inclusion_rules_not_exposed_via_settings(client):
    assert client.get("/api/settings/inclusion_rules").status_code == 404


def test_external_exclude_write_surfaces_as_manual(client, criteria_doc):
    # past the fresh-DB state: author a rule first
    manual = _full_manual(client.get("/api/inclusion-rules").json()["compiled"])
    client.put(
        "/api/inclusion-rules",
        json={
            "rules": [{"id": "r", "verb": "include", "target": "title", "terms": ["brandco"]}],
            "manual": manual,
        },
    )
    # an accepted dismissal suggestion appends to title_exclude_keywords directly
    client.post("/api/suggestions/title-exclude", json={"keyword": "sales", "action": "accept"})
    compiled = client.get("/api/inclusion-rules").json()["compiled"]
    assert "sales" in _by_source(compiled, "title_exclude_keywords", "manual")
