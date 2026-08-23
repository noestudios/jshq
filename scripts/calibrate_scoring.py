"""Live calibration check for the fit scorer (scoring redesign 2026-07).

Scores the fixture JDs in tests/fixtures/calibration/manifest.json (2 known-good
people-leader roles, 3 known-bad IC/evangelism/convert-sell roles, 1 genuinely
mixed, 1 capped — the craft-heavy-IC regression: an IC posting that must read ic
and hit the score cap — and three function-check cases: a wrong-function
Director of Product managing PMs that once read as design leadership, a Senior
Director of Content Design that escaped the cap when DISCIPLINES had no
"content" token, and the design-sibling control that must read leads design,
unflagged) against the
CURRENT fit_criteria.md and asserts they separate by a wide margin. Run it after
editing the criteria doc or the scoring prompt; ~10 Haiku calls ≈ $0.08.

A fixture labelled wrong_function or control names the discipline it must
produce in its manifest entry's `expect_leads` — the label covers more than one
wrong function now, so the expectation cannot live in this script.

Opt-in live: exits 2 if ANTHROPIC_API_KEY is unset. Uses an empty dismissal
digest and no learned rules on purpose — calibration measures the DOC, not
DB state.

Jitter tolerance (2026-08-10): temp-0 reads vary run to run, which made two
assertions coin flips — strict all-distinct aggregates (the two weakest bads
sit 1 point apart in the baseline) and the per-fixture evidence checks. Now a
fixture whose read fails its own checks is re-read ONCE (a persistent failure
fails both reads and still reds the gate), and one 2-way aggregate tie is
tolerated — a value shared by 3+ fixtures, or two tied pairs, still fails.
The drift layer confirms the same way: a fixture whose diff vs the baseline
flags drift is re-read once, and drift is reported only if the re-read still
disagrees — a model-id change is always drift, no re-read can clear it.

Doubles as the MODEL-DRIFT SENTINEL (2026-08-08): every run also diffs the
per-fixture results against tests/fixtures/calibration/baseline.json (when it
exists). Drift (exit 3) is decision-relevant change only: a model-id change,
a missing fixture, a model_score move beyond DRIFT_SCORE_TOLERANCE, or a
mgmt/leads flip between two DEFINITE values; flips involving `unclear`,
flags churn and func_flag churn are soft-field variance and only inform. Run this after any model change (default scoring-model bump
or a provider-side shift); after a DELIBERATE rubric edit the baseline is
expected to move — review the diff, then re-save with --save-baseline (refused
while calibration is failing). Spend is appended to data/harness_spend.jsonl;
the DB is never touched.

Usage: .venv/bin/python scripts/calibrate_scoring.py [--save-baseline]
Exit codes: 0 = calibrated, 1 = margin assertions failed, 2 = no API key,
3 = calibrated but drifted from the baseline.
"""

import argparse
import asyncio
import json
import os
import statistics
import sys
from datetime import datetime
from pathlib import Path


from dotenv import load_dotenv  # noqa: E402

from jshq import aicfg, db, paths, usage  # noqa: E402
from jshq.ats.normalize import derive_level_band  # noqa: E402
from jshq.scoring import boilerplate, derive, haiku  # noqa: E402
from jshq.scoring.criteria import load_criteria  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(REPO_ROOT / ".env")

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "calibration"
BASELINE = FIXTURES / "baseline.json"

# Deliberately the DEFAULT scoring model, never a per-install ai_models
# override: this sentinel blesses the shipped baseline. Scoring on any other
# model is uncalibrated by definition (Settings says so via
# aicfg.CALIBRATED_SCORING_MODEL) and score_job falls back to this same
# default when called without a model.
MODEL = aicfg.DEFAULTS["scoring"]

# |Δmodel_score| beyond this against the baseline is drift; within it is the
# measured temp-0 run-to-run variance (±1–8 on real JDs, 2026-08-08) and only
# informs. Definite↔definite categorical flips are always drift; soft-field
# churn (unclear flips, flags, func_flag) informs — see diff_baseline.
DRIFT_SCORE_TOLERANCE = 8

# Margin assertions — the contract this script enforces.
MIN_SEPARATION = 20  # min(good finals) − max(bad finals)
GOOD_FLOOR = 70
BAD_CEILING = 55

# Spread assertions (sub-score redesign 2026-08). The defect this redesign
# exists to fix was invisible to the margin checks above: nine fixtures all
# scoring 82 would have passed every one of them. Distinctness is the direct
# test, and it is deliberately strict — every fixture is written to differ on
# the criteria, so none of them should collide.
#
# Asserted on the AGGREGATE, not the final. Two fixtures now share the
# wrong_function ceiling of 20, so finals CANNOT all differ — and that is the
# caps working, not the spread failing. The aggregate is where the redesign's
# resolution lives and it is immune to cap collisions.
ALL_AGGREGATES_DISTINCT = True
# A criterion answered `null` is honest; a run where the model nulls almost
# everything means the prompt stopped extracting, and the silence values would
# then be doing all the scoring. Guards the opposite failure from clustering.
MIN_MEDIAN_EVIDENCED = 6


def fixture_job(entry: dict, criteria=None) -> dict:
    return {
        "title": entry["title"],
        # bands come from the doc being calibrated, not the shipped defaults
        "level_band": derive_level_band(
            entry["title"],
            criteria.level_bands if criteria else None,
            criteria.level_band_fallback if criteria else None,
        ),
        "company_name": "Calibration Fixtures Inc",
        "location": "Remote - US",
        "remote_type": "remote",
        "salary_min": 200000,
        "salary_max": 240000,
        "salary_stated": 1,
        "description_text": (FIXTURES / entry["file"]).read_text(encoding="utf-8"),
    }


class _NoTier1:
    """Fixtures skip Tier 1 entirely, so derive() sees no flags from it."""

    near_miss_flags = ()


def build_result(entry: dict, job: dict, data: dict, criteria) -> dict:
    """One fixture's result row. scoring.derive IS _write's pipeline
    (aggregate → IC override → caps → deductions); this script used to mirror
    it inline and drift the same way the prompt construction once did. One
    difference from the old inline copy, deliberate: derive also flags
    thin_posting, which is display-only here (never a deduction) and more
    faithful to the write."""
    d = derive(job, _NoTier1(), data, criteria)
    return {
        "entry": entry, "data": data, "mgmt": d["mgmt"], "cap": d["cap"],
        "func_flag": d["func_flag"], "flags": d["flags"],
        "model_score": d["model_score"],
        "evidenced": d["evidenced"], "lean": d["lean"],
        "capped": d["capped"], "final": d["final"],
        "deductions": d["deductions"],
    }


def read_failures(r: dict) -> list[str]:
    """Per-fixture failures that hinge on the MODEL READ — the flake surface a
    single re-read can clear (temp-0 outputs jitter run to run). Criteria-shape
    checks (cap presence, cap arithmetic) stay in run(): they are deterministic,
    so a re-read cannot change them and must not trigger one."""
    fails: list[str] = []
    d, e = r["data"], r["entry"]
    file = e["file"]
    # Evidence discipline: the sub-scores that move the total most must quote
    # the posting. Unquoted ±2s are where hallucinated fit would hide.
    quotes = d["tier2_quotes"]
    unquoted = sorted(
        n for n, v in d["tier2"].items()
        if v is not None and abs(v) == 2 and not quotes.get(n)
    )
    if unquoted:
        fails.append(f"{file}: ±2 sub-scores without quoted evidence: {unquoted}")
    # Raw model reads on purpose (pre-override): these test the PROMPT; the
    # code override would make them vacuous.
    if e["label"] == "bad" and d["management_type"] == "people_leader":
        fails.append(f"{file}: bad fixture read as people_leader")
    if e["label"] == "capped" and d["management_type"] != "ic":
        fails.append(f"{file}: model read {d['management_type']}, expected raw ic")
    if e["label"] == "wrong_function":
        # Read the expected discipline from the manifest: this label now covers
        # more than one wrong function (a PM director and a content director),
        # so hard-coding "product" here would fail the content fixture for
        # being correct.
        want = e.get("expect_leads", "product")
        if d["leads_discipline"] != want:
            fails.append(
                f"{file}: model read leads {d['leads_discipline']}, expected {want}"
            )
        if "wrong_function" not in r["flags"]:
            fails.append(f"{file}: wrong_function flag missing")
    if e["label"] == "control":
        want = e.get("expect_leads", "design")
        if d["leads_discipline"] != want:
            fails.append(
                f"{file}: model read leads {d['leads_discipline']}, expected {want}"
            )
        if r["func_flag"] is not None:
            fails.append(f"{file}: control fixture flagged {r['func_flag']}")
    # Notes carry only what the sub-scores cannot: the management_type and
    # leads_discipline evidence. Criterion evidence lives in tier2's quotes,
    # checked above. Skipped when both reads are "unclear" — there is then
    # nothing to quote, and demanding a quote invites fabrication.
    if not (d["management_type"] == "unclear" and d["leads_discipline"] == "unclear"):
        if d["scoring_notes"].count('"') < 2:  # ≥1 quoted phrase
            fails.append(f"{file}: notes lack quoted evidence")
    return fails


def result_snapshot(r: dict) -> dict:
    """The per-fixture fields the baseline stores and diff_baseline compares."""
    return {
        "label": r["entry"]["label"], "final": r["final"],
        "model_score": r["model_score"], "mgmt": r["mgmt"],
        "leads": r["data"]["leads_discipline"], "func_flag": r["func_flag"],
        "flags": r["flags"], "evidenced": r["evidenced"],
        "confidence": r["data"]["confidence"],
    }


def drifted_fixture_files(drift: list[str], known_files) -> list[str]:
    """The fixture files named in drift lines — the ones a re-read can confirm
    or clear. Lines not keyed by a fixture (a model-id change) pass through
    untouched; re-reading cannot clear those."""
    known = set(known_files)
    return sorted({line.split(":", 1)[0] for line in drift} & known)


def aggregate_collision(aggregates: list[int]) -> str | None:
    """The habitual-number guard, tolerant of ONE 2-way tie. The baseline's two
    weakest bads sit 1 point apart (24/25), so strict all-distinct was a coin
    flip under temp-0 jitter — it went red twice on unchanged inputs 2026-08-10
    (and needed a same-minute rerun to pass on 2026-08-08). The pathology this
    exists to catch — many fixtures piling onto one habitual number — still
    fails: a value shared by 3+ fixtures, or two tied pairs, drops distinct
    below n−1."""
    distinct = len(set(aggregates))
    if distinct >= len(aggregates) - 1:
        return None
    collisions = sorted({s for s in aggregates if aggregates.count(s) > 1})
    return (
        f"only {distinct} distinct aggregates across {len(aggregates)} fixtures "
        f"(one 2-way tie is tolerated as temp-0 jitter); collided on {collisions} "
        "— fixtures that differ on the criteria must not pile onto the same "
        "weighted total"
    )


def diff_baseline(baseline: dict, current: dict) -> tuple[list[str], list[str]]:
    """(drift, info) lines comparing per-fixture results keyed by file name.

    Pure so it is unit-testable without a model call. Drift is DECISION-
    RELEVANT change only (narrowed 2026-08-10 — the exact-match version went
    red on soft-field churn that provably flaps between consecutive temp-0
    runs): a model-id change, a fixture missing, a score move beyond
    DRIFT_SCORE_TOLERANCE, and mgmt/leads flips where BOTH sides are definite
    (ic↔people_leader, design→product). A flip into or out of `unclear`
    mirrors the pipeline's own doctrine — unclear never overrides a definite
    read — and lands in info, as do flags-set and func_flag churn (visibility
    surfaces that ride on the soft reads; a leads flip that MATTERS is already
    drift on its own line). Smaller score movement is variance, also info.
    """
    drift, info = [], []
    if baseline.get("model") != current.get("model"):
        drift.append(
            f"model changed: {baseline.get('model')} → {current.get('model')} — "
            "review the per-fixture diff, then --save-baseline"
        )
    old_r, new_r = baseline.get("results", {}), current.get("results", {})
    for name in sorted(set(old_r) | set(new_r)):
        if name not in old_r:
            info.append(f"{name}: new fixture, not in the baseline")
            continue
        if name not in new_r:
            drift.append(f"{name}: in the baseline but not scored this run")
            continue
        old, new = old_r[name], new_r[name]
        for field in ("mgmt", "leads"):
            o, n = old.get(field), new.get(field)
            if o != n:
                if "unclear" in (o, n):
                    info.append(f"{name}: {field} {o} → {n} — soft flip, not drift")
                else:
                    drift.append(f"{name}: {field} {o} → {n}")
        if old.get("func_flag") != new.get("func_flag"):
            info.append(
                f"{name}: func_flag {old.get('func_flag')} → {new.get('func_flag')} "
                "— rides the leads read, not drift by itself"
            )
        if sorted(old.get("flags", [])) != sorted(new.get("flags", [])):
            info.append(f"{name}: flags {old.get('flags')} → {new.get('flags')}")
        delta = new.get("model_score", 0) - old.get("model_score", 0)
        if abs(delta) > DRIFT_SCORE_TOLERANCE:
            drift.append(
                f"{name}: model_score {old.get('model_score')} → "
                f"{new.get('model_score')} ({delta:+d})"
            )
        elif delta:
            info.append(
                f"{name}: model_score {old.get('model_score')} → "
                f"{new.get('model_score')} ({delta:+d}) — within temp-0 variance"
            )
    return drift, info


async def run(save_baseline: bool = False, doc: Path | None = None) -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set (.env at the repo root) — calibration needs a live key.")
        return 2

    from anthropic import AsyncAnthropic

    # The SHIPPED doc by default, not DATA_DIR's live copy. baseline.json is a
    # committed repo artifact, so it has to be reproducible from the repo
    # alone: reading whatever a developer has tuned locally (or, on a machine
    # that has never run the app, a file that does not exist yet) would make
    # the drift check meaningless. Same reason the golden prompt snapshots the
    # shipped doc. --doc calibrates a different one deliberately.
    source = doc or (paths.DEFAULTS_DIR / "fit_criteria.md")
    print(f"criteria: {source}")
    criteria = load_criteria(source)
    # Deliberately NOT scoring.build_prompt_inputs: this scores synthetic
    # fixtures against the DOC, and a calibration that shifted with whatever was
    # dismissed last week — or with a learned rule added yesterday — would not be
    # a calibration. score_distribution.py is the opposite case and must use the
    # shared helper: it measures real jobs to approve a real write.
    system = haiku.build_system_prompt(criteria, "", [])
    manifest = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
    client = AsyncAnthropic(max_retries=6)

    results = []
    acc = {
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
    }
    calls = 0

    def took(used) -> None:
        nonlocal calls
        if used is not None:
            for k in acc:
                acc[k] += getattr(used, k, 0) or 0
            calls += 1

    for entry in manifest:  # serial: 10 calls (+ flake re-reads), manifest order
        job = fixture_job(entry, criteria)
        data, used = await haiku.score_job(client, system, job, criteria)
        took(used)
        r = build_result(entry, job, data, criteria)
        flakes = read_failures(r)
        if flakes:
            # One re-read for a failing read: temp-0 outputs jitter run to run,
            # so a single flake (a missing quote, a wobbled categorical) is not
            # signal. A persistent failure fails both reads and still reds the
            # gate; the first read is kept then, so the report is the read the
            # gate saw first.
            print(f"  re-read {entry['file']}: {'; '.join(flakes)}")
            data2, used2 = await haiku.score_job(client, system, job, criteria)
            took(used2)
            r2 = build_result(entry, job, data2, criteria)
            if not read_failures(r2):
                r = r2
        results.append(r)

    print(f"{'fixture':<32} {'label':<14} {'agg':>4} {'final':>5} {'lean':>5} {'ev':>3} "
          f"{'mgmt':<14} {'leads':<12} {'conf':<7} {'cap':<5} flags")
    for r in results:
        d, e = r["data"], r["entry"]
        cap_col = f"→{r['capped']}" if r["capped"] < r["model_score"] else "-"
        print(f"{e['file']:<32} {e['label']:<14} {r['model_score']:>4} {r['final']:>5} "
              f"{r['lean']:>+5} {r['evidenced']:>3} {d['management_type']:<14} "
              f"{d['leads_discipline']:<12} "
              f"{d['confidence']:<7} {cap_col:<5} {','.join(r['flags']) or '-'}")

    # Per-criterion value usage across the nine fixtures — the diagnostic for
    # "did the model actually use the scale, or collapse one level down?"
    print("\nsub-score usage by criterion ('.' = null, one column per fixture):")
    for n in range(1, criteria.tier2_count + 1):
        values = [r["data"]["tier2"].get(n) for r in results]
        cells = " ".join(" ." if v is None else f"{v:+d}" for v in values)
        print(f"  c{n:<3} {cells}   distinct={len(set(values))}")

    goods = [r["final"] for r in results if r["entry"]["label"] == "good"]
    bads = [r["final"] for r in results if r["entry"]["label"] == "bad"]
    failures = []
    if min(goods) - max(bads) < MIN_SEPARATION:
        failures.append(f"separation {min(goods) - max(bads)} < {MIN_SEPARATION}")
    if min(goods) < GOOD_FLOOR:
        failures.append(f"a good fixture scored {min(goods)} < {GOOD_FLOOR}")
    if max(bads) > BAD_CEILING:
        failures.append(f"a bad fixture scored {max(bads)} > {BAD_CEILING}")

    # Spread: the checks above all passed while every good fixture scored 82.
    finals = [r["final"] for r in results]
    aggregates = [r["model_score"] for r in results]
    distinct = len(set(aggregates))
    if ALL_AGGREGATES_DISTINCT:
        collision = aggregate_collision(aggregates)
        if collision:
            failures.append(collision)
    median_evidenced = statistics.median(r["evidenced"] for r in results)
    if median_evidenced < MIN_MEDIAN_EVIDENCED:
        failures.append(
            f"median evidenced criteria {median_evidenced} < {MIN_MEDIAN_EVIDENCED} — "
            "the prompt is not extracting, so silence values are doing the scoring"
        )
    for r in results:
        # Read-dependent checks live in read_failures (shared with the in-loop
        # flake re-read); a failure here means BOTH reads of that fixture failed.
        failures.extend(read_failures(r))
        # Criteria-shape checks — deterministic given the doc, so they sit
        # outside read_failures and never trigger a re-read.
        if r["entry"]["label"] == "capped":
            ic_cap = criteria.caps.get("ic")
            if ic_cap is None:
                failures.append("score_caps has no 'ic' entry — the cap is disabled")
            elif r["final"] > ic_cap:
                failures.append(
                    f"{r['entry']['file']}: final {r['final']} > ic cap {ic_cap}"
                )
        if r["entry"]["label"] == "wrong_function":
            wf_cap = criteria.caps.get("wrong_function")
            if wf_cap is None:
                failures.append(
                    "score_caps has no 'wrong_function' entry — the cap is disabled"
                )
            elif r["final"] > wf_cap:
                failures.append(
                    f"{r['entry']['file']}: final {r['final']} > wrong_function cap {wf_cap}"
                )

    # sanity: fixtures must not accidentally trip the boilerplate machinery
    assert boilerplate.strip_shared("x", set()) == "x"

    # Spend ledger first — the run cost real dollars whether or not it
    # calibrated, and the DB deliberately never sees harness spend.
    if calls:
        cost = usage.cost_of(MODEL, acc)
        ledger = usage.append_harness_ledger(
            "calibrate_scoring", MODEL, acc, calls=calls, cost=cost,
            args=" ".join(sys.argv[1:]),
        )
        print(f"\ncost: ${cost:.4f} over {calls} calls — appended to {ledger}")

    current = {
        "model": MODEL,
        "saved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "results": {r["entry"]["file"]: result_snapshot(r) for r in results},
    }

    if failures:
        print("\nNOT CALIBRATED:")
        for f in failures:
            print(f"  - {f}")
        if save_baseline:
            print("\n--save-baseline refused: a failing calibration is not a baseline.")
        return 1

    drifted = False
    if save_baseline:
        BASELINE.write_text(json.dumps(current, indent=1) + "\n", encoding="utf-8")
        print(f"\nbaseline saved to {BASELINE}")
    elif BASELINE.exists():
        baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
        drift, info = diff_baseline(baseline, current)
        if drift:
            # Confirm before reporting: categorical reads on boundary fixtures
            # flap run to run (2026-08-10: consecutive runs disagreed with each
            # other on the very fields the diff flagged), so ONE disagreeing
            # run is not a provider shift. Each drifted fixture is re-read once
            # and re-diffed; drift stands only if the re-read still disagrees
            # with the baseline. A model-id change has no fixture key and is
            # never cleared this way. The re-read replaces the fixture's entry
            # in `current` (and in a --save-baseline that never runs here), not
            # the table printed above — the drift block reports what changed.
            by_file = {e["file"]: e for e in manifest}
            retry_files = drifted_fixture_files(drift, by_file)
            if retry_files:
                acc2 = dict.fromkeys(acc, 0)
                calls2 = 0
                for file in retry_files:
                    print(f"  drift re-read: {file}")
                    job = fixture_job(by_file[file], criteria)
                    data2, used2 = await haiku.score_job(client, system, job, criteria)
                    if used2 is not None:
                        for k in acc2:
                            acc2[k] += getattr(used2, k, 0) or 0
                        calls2 += 1
                    current["results"][file] = result_snapshot(
                        build_result(by_file[file], job, data2, criteria)
                    )
                # Second ledger line for the re-read spend — the first entry
                # was appended before this branch could know it needed more
                # calls, and harness spend must never go unrecorded.
                if calls2:
                    cost2 = usage.cost_of(MODEL, acc2)
                    usage.append_harness_ledger(
                        "calibrate_scoring", MODEL, acc2, calls=calls2,
                        cost=cost2, args="drift-re-read",
                    )
                    print(f"  re-read cost: ${cost2:.4f} over {calls2} calls")
                drift, info = diff_baseline(baseline, current)
        for line in info:
            print(f"  variance: {line}")
        if drift:
            print(f"\nDRIFT vs the baseline saved {baseline.get('saved_at')}:")
            for line in drift:
                print(f"  - {line}")
            print("Deliberate (model or rubric change)? Review, then --save-baseline.")
            drifted = True
        else:
            print(f"\nno drift vs the baseline saved {baseline.get('saved_at')}")
    else:
        print(f"\nno baseline on file — save one with --save-baseline ({BASELINE})")

    capped_finals = [r["final"] for r in results if r["entry"]["label"] == "capped"]
    wf_finals = [r["final"] for r in results if r["entry"]["label"] == "wrong_function"]
    print(f"\nCALIBRATED: goods {goods}, bads {bads}, capped {capped_finals}, "
          f"wrong-function {wf_finals}, separation {min(goods) - max(bads)} "
          f"(≥{MIN_SEPARATION}), {distinct}/{len(aggregates)} distinct aggregates, "
          f"median evidenced {median_evidenced}.")
    return 3 if drifted else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--save-baseline", action="store_true",
        help="write the per-fixture results to tests/fixtures/calibration/"
             "baseline.json as the drift baseline (refused while calibration "
             "is failing)",
    )
    parser.add_argument(
        "--doc", type=Path, default=None,
        help="criteria doc to calibrate (default: the shipped example in "
             "src/jshq/defaults/, which is what the committed baseline was "
             "blessed against). Point this at DATA_DIR/fit_criteria.md to "
             "calibrate your own tuned doc.",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(run(save_baseline=args.save_baseline, doc=args.doc)))
