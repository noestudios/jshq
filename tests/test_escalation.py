"""Escalation-on-ambiguity — near-threshold re-reads and cap-flip confirmation."""

import asyncio
import json

from test_scoring_run import GOOD_PAYLOAD, fake_client, run, seed_job, tier2

from jshq import scoring

# Against the live doc: {c1:+2, c5:+2, rest explicit 0} aggregates to 69 —
# inside the ±8 escalation band around POSITIVE_FIT (70) and below it; adding
# c2:+2 gives 75, inside the band and above it. All eleven evidenced, no caps
# (people_leader / design), so final == aggregate.
NEAR_LOW = dict(GOOD_PAYLOAD, tier2=tier2({i: (2 if i in (1, 5) else 0) for i in range(1, 12)}))
NEAR_HIGH = dict(GOOD_PAYLOAD, tier2=tier2({i: (2 if i in (1, 2, 5) else 0) for i in range(1, 12)}))

UNCLEAR_IC = dict(GOOD_PAYLOAD, leads_discipline="unclear", management_type="ic")
ENG_IC = dict(GOOD_PAYLOAD, leads_discipline="engineering", management_type="ic")

STORED_ENG_IC = json.dumps({
    "model_score": 40, "leads_discipline": "engineering",
    "management_type": "ic", "subscores": {},
})


def _detail(db, jid):
    return json.loads(
        db.execute("SELECT score_detail FROM jobs WHERE id = ?", (jid,)).fetchone()[0]
    )


def test_near_threshold_same_side_keeps_first_read(db, seed_company):
    jid = seed_job(db, seed_company())
    client, state = fake_client(payload=NEAR_LOW)
    report = run(db, client)
    assert state["calls"] == 2  # one escalation read, then both agree on the side
    assert report["escalated"] == 1
    row = db.execute("SELECT fit_score, scoring_notes FROM jobs WHERE id = ?", (jid,)).fetchone()
    assert row["fit_score"] == 69
    assert "- Escalated: 2 reads (near threshold)" in row["scoring_notes"]
    assert _detail(db, jid)["escalation"] == {"reads": 2, "outcomes": ["near threshold"]}


def test_near_threshold_split_sides_takes_median_of_three(db, seed_company):
    jid = seed_job(db, seed_company())
    client, state = fake_client(sequence=[NEAR_LOW, NEAR_HIGH, NEAR_LOW])
    report = run(db, client)
    assert state["calls"] == 3  # 69 vs 75 straddle the threshold -> third read
    assert report["escalated"] == 1
    assert db.execute("SELECT fit_score FROM jobs WHERE id = ?", (jid,)).fetchone()[0] == 69
    assert _detail(db, jid)["escalation"]["reads"] == 3


def test_far_from_threshold_first_time_score_is_one_call(db, seed_company):
    # GOOD_PAYLOAD lands at 85, outside the band; no stored row, so no flip
    # trigger either. Exactly the pre-escalation behavior.
    seed_job(db, seed_company())
    client, state = fake_client()
    report = run(db, client)
    assert state["calls"] == 1
    assert report["escalated"] == 0


def test_failed_escalation_read_keeps_the_first(db, seed_company):
    jid = seed_job(db, seed_company())
    client, state = fake_client(sequence=[NEAR_LOW, RuntimeError("boom")])
    report = run(db, client)
    assert state["calls"] == 2
    assert report["scored"] == 1 and report["errors"] == 0
    assert report["escalated"] == 0  # best-effort: the read failed, no escalation recorded
    assert db.execute("SELECT fit_score FROM jobs WHERE id = ?", (jid,)).fetchone()[0] == 69
    assert "escalation" not in _detail(db, jid)


def test_cap_flip_unconfirmed_keeps_stored_categorical(db, seed_company):
    # Stored read engineering/ic (wrong_function, cap 15); fresh read 1 says
    # unclear (no flag, cap 50) — a cap-changing flip. Read 2 returns to
    # engineering, so the flip is unconfirmed and the stored value is kept:
    # the row stays on the wrong_function cap.
    jid = seed_job(
        db, seed_company(), tier1_results='{"comp": "pass"}', fit_score=20,
        score_detail=STORED_ENG_IC,
    )
    client, state = fake_client(sequence=[UNCLEAR_IC, ENG_IC])
    report = asyncio.run(scoring.run_scoring(db, client=client, only_pending=False))
    assert state["calls"] == 2
    assert report["escalated"] == 1
    row = db.execute("SELECT fit_score, scoring_notes FROM jobs WHERE id = ?", (jid,)).fetchone()
    assert row["fit_score"] == 15
    detail = _detail(db, jid)
    assert detail["leads_discipline"] == "engineering"
    assert detail["escalation"]["outcomes"] == [
        "leads flip engineering → unclear unconfirmed; kept engineering"
    ]
    assert "unconfirmed; kept engineering" in row["scoring_notes"]


def test_cap_flip_confirmed_by_two_agreeing_reads(db, seed_company):
    # Both fresh reads say unclear where the stored row said engineering: the
    # flip is real, the wrong_function cap lifts, the ic cap (50) binds instead.
    jid = seed_job(
        db, seed_company(), tier1_results='{"comp": "pass"}', fit_score=20,
        score_detail=STORED_ENG_IC,
    )
    client, state = fake_client(payload=UNCLEAR_IC)
    report = asyncio.run(scoring.run_scoring(db, client=client, only_pending=False))
    assert state["calls"] == 2
    assert report["escalated"] == 1
    assert db.execute("SELECT fit_score FROM jobs WHERE id = ?", (jid,)).fetchone()[0] == 50
    detail = _detail(db, jid)
    assert detail["leads_discipline"] == "unclear"
    assert detail["escalation"]["outcomes"] == [
        "leads flip engineering → unclear confirmed"
    ]


def test_rescore_with_unchanged_reads_is_one_call(db, seed_company):
    # A same-cap rescore (stored and fresh both engineering/ic) never escalates:
    # the flip trigger needs the CAP to change, not just a rescore to happen.
    seed_job(
        db, seed_company(), tier1_results='{"comp": "pass"}', fit_score=20,
        score_detail=STORED_ENG_IC,
    )
    client, state = fake_client(payload=ENG_IC)
    report = asyncio.run(scoring.run_scoring(db, client=client, only_pending=False))
    assert state["calls"] == 1
    assert report["escalated"] == 0
