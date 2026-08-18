"""Sibling-consistency pass — clustering, voting asymmetries, write scope."""

import json

from test_scoring_run import GOOD_PAYLOAD, fake_client, run, seed_job

from jshq.scoring import consistency

# Two blocks, each comfortably over boilerplate.MIN_BLOCK_CHARS after
# normalization — identical across siblings, so their Jaccard is 1.0.
LONG_JD = (
    "Lead applied research across the platform organization, partnering with "
    "engineering to ship models that improve the customer experience end to end "
    "and measurably move the core product metrics quarter over quarter."
    "\n\n"
    "You will design experiments, publish findings, and mentor a growing "
    "community of researchers while working closely with product and "
    "engineering leadership on the long-term technical roadmap."
)

# A same-length but entirely different posting — Jaccard 0.0 against LONG_JD.
OTHER_JD = (
    "Own the visual design language for the flagship mobile application, from "
    "early concept sketches through polished production interfaces shipped to "
    "millions of customers around the world every single day."
    "\n\n"
    "Partner with brand, marketing and accessibility specialists to keep the "
    "design system coherent as the product surface grows across platforms and "
    "form factors year after year."
)

UNCLEAR_IC = dict(GOOD_PAYLOAD, leads_discipline="unclear", management_type="ic")
ENG_IC = dict(GOOD_PAYLOAD, leads_discipline="engineering", management_type="ic")


def _member(i, text, leads, mgmt="ic", fresh=True, ic_designated=False, company=1):
    return {
        "id": i, "company_id": company, "text": text,
        "reads": {"leads_discipline": leads, "management_type": mgmt},
        "ic_designated": ic_designated, "fresh": fresh,
    }


def test_exact_half_is_not_a_majority():
    members = [
        _member(1, LONG_JD, "engineering"), _member(2, LONG_JD, "engineering"),
        _member(3, LONG_JD, "design"), _member(4, LONG_JD, "design"),
    ]
    assert consistency.corrections(members) == []


def test_votes_never_cross_companies():
    # Were grouping broken, ids 1+2 would outvote id 3 across the company line.
    members = [
        _member(1, LONG_JD, "engineering", company=1),
        _member(2, LONG_JD, "engineering", company=1),
        _member(3, LONG_JD, "unclear", company=2),
    ]
    assert consistency.corrections(members) == []


def test_minority_unclear_corrected_to_definite_majority(db, seed_company):
    # The sibling-majority shape: one of three near-identical postings reads `unclear`
    # where its siblings read `engineering`, so it alone escapes the
    # wrong_function cap. The pass adopts the majority and the cap applies.
    cid = seed_company()
    odd = seed_job(db, cid, title="Researcher A", description_text=LONG_JD)
    seed_job(db, cid, title="Researcher B", description_text=LONG_JD)
    seed_job(db, cid, title="Researcher C", description_text=LONG_JD)
    client, _ = fake_client(payload=ENG_IC, by_title={"Researcher A": UNCLEAR_IC})
    report = run(db, client)
    assert report["scored"] == 3
    assert report["sibling_overrides"] == 1
    rows = {
        r["title"]: r
        for r in db.execute("SELECT * FROM jobs WHERE company_id = ?", (cid,))
    }
    corrected, sibling = rows["Researcher A"], rows["Researcher B"]
    assert corrected["fit_score"] == sibling["fit_score"]  # same side of the cap
    flags = json.loads(corrected["near_miss_flags"])
    assert "sibling_override" in flags and "wrong_function" in flags
    detail = json.loads(corrected["score_detail"])
    assert detail["leads_discipline"] == "engineering"
    assert detail["sibling_override"] == {
        "leads_discipline": {"from": "unclear", "to": "engineering"}
    }
    assert (
        "- Sibling consistency: leads unclear → engineering "
        "(2 of 3 near-identical postings agree)" in corrected["scoring_notes"]
    )
    assert "sibling_override" not in json.loads(sibling["near_miss_flags"])


def test_unclear_majority_never_overrides_definite_read(db, seed_company):
    # `unclear` is absence of evidence; two absences must not erase one read.
    cid = seed_company()
    definite = seed_job(db, cid, title="Researcher A", description_text=LONG_JD)
    seed_job(db, cid, title="Researcher B", description_text=LONG_JD)
    seed_job(db, cid, title="Researcher C", description_text=LONG_JD)
    client, _ = fake_client(payload=UNCLEAR_IC, by_title={"Researcher A": ENG_IC})
    report = run(db, client)
    assert report["sibling_overrides"] == 0
    detail = json.loads(
        db.execute("SELECT score_detail FROM jobs WHERE id = ?", (definite,)).fetchone()[0]
    )
    assert detail["leads_discipline"] == "engineering"


def test_stored_siblings_vote_but_are_never_rewritten(db, seed_company):
    # Two stored rows carry `engineering`; the one pending job reads `unclear`.
    # The stored majority corrects the fresh row; the stored rows are untouched.
    cid = seed_company()
    stored_detail = json.dumps({
        "model_score": 40, "leads_discipline": "engineering",
        "management_type": "ic", "subscores": {},
    })
    stored = [
        seed_job(
            db, cid, title=f"Stored {i}", description_text=LONG_JD,
            tier1_results='{"comp": "pass"}', fit_score=20,
            score_detail=stored_detail, near_miss_flags='["wrong_function"]',
        )
        for i in (1, 2)
    ]
    fresh = seed_job(db, cid, title="Researcher A", description_text=LONG_JD)
    client, state = fake_client(payload=UNCLEAR_IC)
    report = run(db, client)
    assert state["calls"] == 1  # stored rows are not rescored, only consulted
    assert report["sibling_overrides"] == 1
    detail = json.loads(
        db.execute("SELECT score_detail FROM jobs WHERE id = ?", (fresh,)).fetchone()[0]
    )
    assert detail["leads_discipline"] == "engineering"
    for jid in stored:
        row = db.execute("SELECT score_detail FROM jobs WHERE id = ?", (jid,)).fetchone()
        assert row["score_detail"] == stored_detail


def test_ic_designated_management_read_is_never_corrected(db, seed_company):
    # The IC-designation override owns management_type for a designated title;
    # a majority of people_leader siblings must not falsify its model read.
    cid = seed_company()
    designated = seed_job(
        db, cid, title="Design Director (Individual Contributor)",
        description_text=LONG_JD,
    )
    seed_job(db, cid, title="Design Director A", description_text=LONG_JD)
    seed_job(db, cid, title="Design Director B", description_text=LONG_JD)
    ic_read = dict(GOOD_PAYLOAD, management_type="ic")
    client, _ = fake_client(
        by_title={"Design Director (Individual Contributor)": ic_read}
    )
    report = run(db, client)
    assert report["sibling_overrides"] == 0
    detail = json.loads(
        db.execute(
            "SELECT score_detail FROM jobs WHERE id = ?", (designated,)
        ).fetchone()[0]
    )
    assert detail["management_type"] == "ic"
    assert "sibling_override" not in detail


def test_different_texts_never_cluster(db, seed_company):
    # Same company, same-family titles, genuinely different postings (the
    # Apple Product Designer cohort): no cluster, so no vote reaches the odd
    # read even though a title-based grouping would have flipped it.
    cid = seed_company()
    odd = seed_job(db, cid, title="Researcher A", description_text=OTHER_JD)
    seed_job(db, cid, title="Researcher B", description_text=LONG_JD)
    seed_job(db, cid, title="Researcher C", description_text=LONG_JD)
    client, _ = fake_client(payload=ENG_IC, by_title={"Researcher A": UNCLEAR_IC})
    report = run(db, client)
    assert report["sibling_overrides"] == 0
    detail = json.loads(
        db.execute("SELECT score_detail FROM jobs WHERE id = ?", (odd,)).fetchone()[0]
    )
    assert detail["leads_discipline"] == "unclear"


def test_management_read_corrected_for_undesignated_title(db, seed_company):
    # management_type is voted on too: an `unclear` mgmt read among definite
    # people_leader siblings adopts the majority, and the correction changes
    # the derived cap (people_leader has none; unclear+design derives none
    # either — so here the visible effect is the detail read and the flag).
    cid = seed_company()
    odd = seed_job(db, cid, title="Design Director A", description_text=LONG_JD)
    seed_job(db, cid, title="Design Director B", description_text=LONG_JD)
    seed_job(db, cid, title="Design Director C", description_text=LONG_JD)
    unclear_mgmt = dict(GOOD_PAYLOAD, management_type="unclear")
    client, _ = fake_client(by_title={"Design Director A": unclear_mgmt})
    report = run(db, client)
    assert report["sibling_overrides"] == 1
    detail = json.loads(
        db.execute("SELECT score_detail FROM jobs WHERE id = ?", (odd,)).fetchone()[0]
    )
    assert detail["management_type"] == "people_leader"
    assert detail["sibling_override"] == {
        "management_type": {"from": "unclear", "to": "people_leader"}
    }
