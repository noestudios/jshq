"""Structured fit-criteria editor endpoints (Phase 7h).

The endpoints read/write the user's criteria doc; the shared criteria_doc
fixture (conftest.py) redirects CRITERIA_PATH to a temp copy so the seeded
doc is never touched, and resets the mtime cache so each test reads its own
copy.
"""

import re

from jshq.scoring import criteria as criteria_mod


def _tier2_block(doc: str) -> str:
    """The rendered Tier 2 list region between the markers (the prose explainer
    outside them legitimately mentions `[w: …]`, so weight-suffix assertions must
    look only here)."""
    return doc.split("<!-- tier2:start -->")[1].split("<!-- tier2:end -->")[0]


def _tier2_items(doc: str) -> list[str]:
    """The numbered list items inside the markers. The criteria doc is LIVE user
    config (the user edits it via Settings), so tests derive expected counts from
    the doc copy instead of hardcoding them — criteria tuning must never break
    the suite."""
    return [line for line in _tier2_block(doc).splitlines() if re.match(r"\d+\. ", line)]


_TRAILING_TOKEN = re.compile(r"\s*\[(?:craft|bonus|w:\s*([0-9.]+))\]\s*$")


def _parse_item(line: str) -> tuple[str, float]:
    """Independent re-parse of one doc criterion line: strip the `N. ` prefix,
    then peel trailing `[w: X]` / `[craft]` / `[bonus]` tokens (rightmost
    weight wins, no suffix defaults to 1.0). Mirrors the documented contract so
    expectations come from the doc copy, never from pinned content."""
    text = re.sub(r"^\d+\.\s+", "", line)
    weight, seen_weight = 1.0, False
    while True:
        m = _TRAILING_TOKEN.search(text)
        if not m:
            return text, weight
        if m.group(1) and not seen_weight:
            weight, seen_weight = float(m.group(1)), True
        text = text[: m.start()]


def test_get_returns_params_and_tier2(client, criteria_doc):
    body = client.get("/api/scoring/criteria").json()
    assert body["tier1_params"]["comp_floor"] == 160000
    assert "location_allowlist" in body["tier1_params"]
    doc_items = _tier2_items(criteria_doc.read_text(encoding="utf-8"))
    assert len(body["tier2_criteria"]) == len(doc_items) > 0
    # Each item is {text, weight}: the markdown bullet text is preserved, the
    # trailing tokens are parsed off it, and a missing weight suffix defaults
    # to 1.0 — all asserted against the doc copy itself, item by item.
    expected = [_parse_item(line) for line in doc_items]
    assert [(i["text"], i["weight"]) for i in body["tier2_criteria"]] == expected
    # The shipped example must exercise both paths: parsed suffixes and the default.
    assert any(w != 1.0 for _t, w in expected)
    assert any(w == 1.0 for _t, w in expected)
    assert not any(i["text"].endswith("]") for i in body["tier2_criteria"])
    assert all(item["weight"] > 0 for item in body["tier2_criteria"])


def test_put_round_trips_and_persists(client, criteria_doc):
    current = client.get("/api/scoring/criteria").json()
    params = current["tier1_params"]
    params["comp_floor"] = 175000
    params["location_allowlist"] = params["location_allowlist"] + ["naperville"]
    tier2 = [{"text": "Reordered top item", "weight": 1.0}] + current["tier2_criteria"][1:]

    resp = client.put(
        "/api/scoring/criteria",
        json={"tier1_params": params, "tier2_criteria": tier2},
    )
    assert resp.status_code == 200
    out = resp.json()
    assert out["tier1_params"]["comp_floor"] == 175000
    assert "naperville" in out["tier1_params"]["location_allowlist"]
    assert out["tier2_criteria"][0]["text"] == "Reordered top item"
    assert len(out["tier2_criteria"]) == len(current["tier2_criteria"])

    # a fresh GET sees the same, and the doc still loads cleanly for scoring
    again = client.get("/api/scoring/criteria").json()
    assert again["tier1_params"]["comp_floor"] == 175000
    assert again["tier2_criteria"][0]["text"] == "Reordered top item"
    crit = criteria_mod.load_criteria(criteria_doc)
    assert crit.params["comp_floor"] == 175000
    assert "tier2:start" not in crit.prose  # markers never reach the prompt


def test_put_round_trips_sectors_and_target_bands(client, criteria_doc):
    # #32: the wizard's filters step now sets excluded_sectors + target title
    # bands through this same endpoint. Both are part of tier1_params — a hard
    # sector filter and a band that must be emittable — validated and persisted
    # whole, so a later GET (the wizard's hydration) sees them.
    current = client.get("/api/scoring/criteria").json()
    params = dict(current["tier1_params"])
    params["excluded_sectors"] = ["gambling", "tobacco"]
    params["target_title_bands"] = ["director"]  # emittable in the fixture ladder

    resp = client.put(
        "/api/scoring/criteria",
        json={"tier1_params": params, "tier2_criteria": current["tier2_criteria"]},
    )
    assert resp.status_code == 200

    again = client.get("/api/scoring/criteria").json()["tier1_params"]
    assert again["excluded_sectors"] == ["gambling", "tobacco"]
    assert "director" in again["target_title_bands"]


def test_invalid_put_is_422_and_leaves_doc_unchanged(client, criteria_doc):
    before = criteria_doc.read_text(encoding="utf-8")
    current = client.get("/api/scoring/criteria").json()
    bad = dict(current["tier1_params"])
    bad["comp_floor"] = "lots"  # not an int -> CriteriaError at validation

    resp = client.put(
        "/api/scoring/criteria",
        json={"tier1_params": bad, "tier2_criteria": current["tier2_criteria"]},
    )
    assert resp.status_code == 422
    # Structured detail (error-audit P1): the editor anchors the inline error
    # from field/kind, never by parsing the message prose.
    detail = resp.json()["detail"]
    assert detail["field"] == "comp_floor"
    assert detail["kind"] == "int"
    assert "[JSHQ-302]" in detail["message"]
    assert criteria_doc.read_text(encoding="utf-8") == before  # live doc untouched
    assert not criteria_doc.with_name(criteria_doc.name + ".tmp").exists()


def test_missing_key_is_422(client, criteria_doc):
    current = client.get("/api/scoring/criteria").json()
    bad = dict(current["tier1_params"])
    del bad["location_allowlist"]
    resp = client.put(
        "/api/scoring/criteria",
        json={"tier1_params": bad, "tier2_criteria": current["tier2_criteria"]},
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["field"] == "location_allowlist"
    assert detail["kind"] == "missing"
    assert "[JSHQ-302]" in detail["message"]


# --- location radius (Phase 7i) -----------------------------------------


def test_geocode_resolves_known_place(client):
    body = client.get("/api/scoring/geocode", params={"q": "Naperville, IL"}).json()
    assert body["label"] == "Naperville, IL"
    assert round(body["lat"], 2) == 41.75


def test_geocode_unresolvable_is_404(client):
    assert client.get("/api/scoring/geocode", params={"q": "Nowhereville ZZ"}).status_code == 404


def test_geocode_requires_q(client):
    assert client.get("/api/scoring/geocode").status_code == 422  # missing required param


def test_criteria_get_carries_location_radius(client, criteria_doc):
    body = client.get("/api/scoring/criteria").json()
    assert body["tier1_params"]["location_radius"]["center"]["label"] == "Evanston, IL"


def test_put_round_trips_location_radius(client, criteria_doc):
    current = client.get("/api/scoring/criteria").json()
    params = current["tier1_params"]
    params["location_radius"]["radius_minutes"] = 45
    resp = client.put(
        "/api/scoring/criteria",
        json={"tier1_params": params, "tier2_criteria": current["tier2_criteria"]},
    )
    assert resp.status_code == 200
    assert resp.json()["tier1_params"]["location_radius"]["radius_minutes"] == 45
    again = client.get("/api/scoring/criteria").json()
    assert again["tier1_params"]["location_radius"]["radius_minutes"] == 45


def test_put_invalid_radius_is_422_and_leaves_doc_unchanged(client, criteria_doc):
    before = criteria_doc.read_text(encoding="utf-8")
    current = client.get("/api/scoring/criteria").json()
    bad = dict(current["tier1_params"])
    bad["location_radius"] = {"center": {"lat": 39.1}, "radius_miles": 30}  # no lng
    resp = client.put(
        "/api/scoring/criteria",
        json={"tier1_params": bad, "tier2_criteria": current["tier2_criteria"]},
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["field"] == "location_radius"
    assert detail["kind"] == "radius"
    assert "[JSHQ-302]" in detail["message"]
    assert criteria_doc.read_text(encoding="utf-8") == before


def test_tier2_add_and_remove_renumbers(client, criteria_doc):
    current = client.get("/api/scoring/criteria").json()
    n = len(current["tier2_criteria"])
    new_t2 = current["tier2_criteria"][:-1] + [{"text": "Brand-new criterion line", "weight": 1.0}]
    resp = client.put(
        "/api/scoring/criteria",
        json={"tier1_params": current["tier1_params"], "tier2_criteria": new_t2},
    )
    assert resp.status_code == 200
    out = resp.json()["tier2_criteria"]
    assert len(out) == n
    assert out[-1]["text"] == "Brand-new criterion line"
    # default-weight items render WITH NO `[w:]` suffix — byte-stable
    assert f"\n{n}. Brand-new criterion line" in criteria_doc.read_text(encoding="utf-8")


def test_tier2_weight_round_trips_inline(client, criteria_doc):
    current = client.get("/api/scoring/criteria").json()
    weighted = [dict(current["tier2_criteria"][0], weight=2.0)] + current["tier2_criteria"][1:]
    resp = client.put(
        "/api/scoring/criteria",
        json={"tier1_params": current["tier1_params"], "tier2_criteria": weighted},
    )
    assert resp.status_code == 200
    assert resp.json()["tier2_criteria"][0]["weight"] == 2.0
    # the weight is encoded inline on the list item so the scorer prose carries it
    assert "[w: 2]" in _tier2_block(criteria_doc.read_text(encoding="utf-8"))
    again = client.get("/api/scoring/criteria").json()
    assert again["tier2_criteria"][0]["weight"] == 2.0


def test_tier2_default_weight_leaves_doc_unweighted(client, criteria_doc):
    # Force every weight to the default — the live doc may carry real [w:]
    # weights (user config), and this test's contract is only "default weights
    # render with no suffix".
    current = client.get("/api/scoring/criteria").json()
    all_default = [dict(item, weight=1.0) for item in current["tier2_criteria"]]
    resp = client.put(
        "/api/scoring/criteria",
        json={"tier1_params": current["tier1_params"], "tier2_criteria": all_default},
    )
    assert resp.status_code == 200
    # all default ⇒ no suffix on any list item (the prose explainer mentions
    # `[w: …]`, so scope the check to the rendered list region)
    assert "[w:" not in _tier2_block(criteria_doc.read_text(encoding="utf-8"))


def test_tier2_weight_out_of_range_is_422(client, criteria_doc):
    before = criteria_doc.read_text(encoding="utf-8")
    current = client.get("/api/scoring/criteria").json()
    bad = [dict(current["tier2_criteria"][0], weight=9.0)] + current["tier2_criteria"][1:]
    resp = client.put(
        "/api/scoring/criteria",
        json={"tier1_params": current["tier1_params"], "tier2_criteria": bad},
    )
    assert resp.status_code == 422  # Tier2Item bounds reject it before the doc is touched
    assert criteria_doc.read_text(encoding="utf-8") == before


def test_editor_put_preserves_score_adjustments_block(client, criteria_doc):
    # The Settings editor rewrites only the tier1_params block + tier2 region;
    # the machine-read score_adjustments block must survive a round-trip.
    current = client.get("/api/scoring/criteria").json()
    resp = client.put(
        "/api/scoring/criteria",
        json={"tier1_params": current["tier1_params"], "tier2_criteria": current["tier2_criteria"]},
    )
    assert resp.status_code == 200
    assert "```json score_adjustments" in criteria_doc.read_text(encoding="utf-8")
    assert len(criteria_mod.load_criteria(criteria_doc).adjustments) > 0


def test_editor_put_preserves_score_caps_block(client, criteria_doc):
    # Same round-trip contract as score_adjustments: the editor must never
    # drop the IC hard-cap config.
    current = client.get("/api/scoring/criteria").json()
    resp = client.put(
        "/api/scoring/criteria",
        json={"tier1_params": current["tier1_params"], "tier2_criteria": current["tier2_criteria"]},
    )
    assert resp.status_code == 200
    assert "```json score_caps" in criteria_doc.read_text(encoding="utf-8")
    assert "ic" in criteria_mod.load_criteria(criteria_doc).caps


def test_editor_put_preserves_score_scale_block(client, criteria_doc):
    # Same round-trip contract: dropping score_scale would silently reset the
    # aggregation to its defaults and shift every score on the board.
    current = client.get("/api/scoring/criteria").json()
    resp = client.put(
        "/api/scoring/criteria",
        json={"tier1_params": current["tier1_params"], "tier2_criteria": current["tier2_criteria"]},
    )
    assert resp.status_code == 200
    assert "```json score_scale" in criteria_doc.read_text(encoding="utf-8")
    assert criteria_mod.load_criteria(criteria_doc).scale["silence"]


def test_editor_put_round_trips_tier2_weights(client, criteria_doc):
    # The [w: X] suffixes ARE the aggregation weights now — an editor save that
    # dropped or reformatted them would re-scale every job.
    before = [i["weight"] for i in client.get("/api/scoring/criteria").json()["tier2_criteria"]]
    current = client.get("/api/scoring/criteria").json()
    resp = client.put(
        "/api/scoring/criteria",
        json={"tier1_params": current["tier1_params"], "tier2_criteria": current["tier2_criteria"]},
    )
    assert resp.status_code == 200
    assert [i["weight"] for i in criteria_mod.load_criteria(criteria_doc).tier2] == before


# --- Tier 2 marker round-trip (Phase 2) ---------------------------------


def test_get_criteria_reports_the_derived_rubric_shape(client, criteria_doc):
    body = client.get("/api/scoring/criteria").json()
    # The DERIVED shape, so a client can show which criterion carries the axis
    # rather than inferring it (and so a legacy doc reports what actually
    # governs scoring, not "no markers").
    assert body["craft_criterion"] == 5
    assert body["no_negative_criteria"] == [11]
    assert body["craft_explicit"] is True


def test_put_criteria_round_trips_the_markers(client, criteria_doc):
    body = client.get("/api/scoring/criteria").json()
    r = client.put(
        "/api/scoring/criteria",
        json={"tier1_params": body["tier1_params"], "tier2_criteria": body["tier2_criteria"]},
    )
    assert r.status_code == 200
    assert client.get("/api/scoring/criteria").json()["craft_criterion"] == 5


def test_put_criteria_refuses_a_payload_that_would_strip_the_markers(client, criteria_doc):
    # An older client that predates the markers would otherwise silently move
    # the craft axis and change craft_lean on every job thereafter.
    body = client.get("/api/scoring/criteria").json()
    stripped = [{"text": t["text"], "weight": t["weight"]} for t in body["tier2_criteria"]]
    r = client.put(
        "/api/scoring/criteria",
        json={"tier1_params": body["tier1_params"], "tier2_criteria": stripped},
    )
    assert r.status_code == 422
    assert "marker" in r.json()["detail"].lower()
    # and the doc is untouched
    assert client.get("/api/scoring/criteria").json()["craft_criterion"] == 5


def test_put_criteria_can_still_clear_a_marker_explicitly(client, criteria_doc):
    body = client.get("/api/scoring/criteria").json()
    items = [dict(t) for t in body["tier2_criteria"]]
    for t in items:
        t["craft"] = False
    r = client.put(
        "/api/scoring/criteria",
        json={"tier1_params": body["tier1_params"], "tier2_criteria": items},
    )
    assert r.status_code == 200
    assert client.get("/api/scoring/criteria").json()["craft_criterion"] is None


# --- vocab endpoint (Phase 2) -------------------------------------------


def test_vocab_serves_the_docs_display_vocabulary(client, criteria_doc):
    body = client.get("/api/scoring/vocab").json()
    bands = [b["value"] for b in body["level_bands"]]
    # de-duplicated in doc order: junior is listed twice in the ladder so that
    # program titles outrank the seniority words above junior/jr/associate
    assert bands == list(dict.fromkeys(bands))
    # the drift this endpoint exists to fix: the frontend's own copy had lost it
    assert "junior" in bands
    assert body["level_bands"][0]["label"]  # every band carries a label
    assert body["in_band_disciplines"] == ["design"]
    assert "design" in body["disciplines"]
    assert body["tension_labels"]["mixed"]
    assert "criteria_error" not in body


def test_vocab_still_serves_labels_when_the_criteria_doc_is_broken(
    client, criteria_doc
):
    # Labeling, not scoring: stored rows must keep rendering. Scoring fails loud
    # on the same doc, so the breakage is not hidden -- it is reported here too.
    criteria_doc.write_text("not a criteria doc", encoding="utf-8")
    body = client.get("/api/scoring/vocab").json()
    assert body["criteria_error"]
    assert [b["value"] for b in body["level_bands"]]
    assert body["disciplines"]


def test_new_taxonomy_settings_are_editable(client, db):
    for key in ("workday_search_terms", "linkedin_title_defaults", "contact_sources"):
        assert client.get(f"/api/settings/{key}").status_code == 200
    r = client.put("/api/settings/contact_sources", json={"value": ["referral", "alumni"]})
    assert r.status_code == 200
    assert client.get(f"/api/settings/contact_sources").json()["value"] == ["referral", "alumni"]


# --- persona editor (Phase 3) -------------------------------------------


def test_get_persona_returns_the_shipped_example(client, criteria_doc):
    body = client.get("/api/scoring/persona").json()
    assert body == {
        "display_name": "Alex Rivera",
        "domain_label": "design-leadership",
        "domain_label_is_default": False,
    }


def test_get_persona_flags_the_neutral_default(client, criteria_doc):
    """The neutral fallback label is placeholder prose, not user content. It is
    served flagged so editors render an empty input — prefilling it once let a
    user append their real answer to it and trip the 120-char persona rail."""
    # A name-only save writes the literal default string into the doc.
    client.put(
        "/api/scoring/persona",
        json={"display_name": "Sam", "domain_label": "the roles you are searching for"},
    )
    body = client.get("/api/scoring/persona").json()
    assert body["domain_label_is_default"] is True
    # A real answer clears the flag.
    client.put(
        "/api/scoring/persona",
        json={"display_name": "Sam", "domain_label": "project management"},
    )
    assert client.get("/api/scoring/persona").json()["domain_label_is_default"] is False


def test_put_persona_round_trips_and_persists(client, criteria_doc):
    r = client.put(
        "/api/scoring/persona",
        json={"display_name": "Robin Vega", "domain_label": "data-platform"},
    )
    assert r.status_code == 200
    assert r.json() == {
        "display_name": "Robin Vega",
        "domain_label": "data-platform",
        "domain_label_is_default": False,
    }
    # Persisted to the doc and reflected on a fresh GET.
    again = client.get("/api/scoring/persona").json()
    assert again["display_name"] == "Robin Vega"
    crit = criteria_mod.load_criteria(criteria_doc)
    assert crit.display_name == "Robin Vega"
    assert crit.domain_label == "data-platform"


def test_put_persona_blank_name_is_anonymous(client, criteria_doc):
    r = client.put(
        "/api/scoring/persona",
        json={"display_name": "   ", "domain_label": "design-leadership"},
    )
    assert r.status_code == 200
    assert r.json()["display_name"] is None  # blank → name nobody
    assert '"display_name": null' in criteria_doc.read_text(encoding="utf-8")


def test_put_persona_invalid_is_422_and_leaves_doc_unchanged(client, criteria_doc):
    before = criteria_doc.read_text(encoding="utf-8")
    r = client.put(
        "/api/scoring/persona",
        json={"display_name": 'Bo " Vance', "domain_label": "role"},
    )
    assert r.status_code == 422
    assert criteria_doc.read_text(encoding="utf-8") == before
    assert not criteria_doc.with_name(criteria_doc.name + ".tmp").exists()


def test_put_persona_requires_a_domain_label(client, criteria_doc):
    r = client.put("/api/scoring/persona", json={"display_name": "Sam", "domain_label": "  "})
    assert r.status_code == 422
