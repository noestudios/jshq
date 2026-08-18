"""Measure the score distribution the CURRENT rubric would produce — READ-ONLY.

Re-scores real jobs and reports what the arithmetic does to the spread, WITHOUT
writing anything to the database. This is the proof step for a rubric change:
score_backfill.py --rescore-all commits the result, so if the distribution is
wrong the board is wrong and it takes a second full rescore to undo. Run first.

**The measurement must use the pipeline's own inputs or it proves nothing.**
An earlier (2026-08) measurement did not: it built its prompt with an empty
dismissal digest and no learned rules, sent unstripped JDs (several losing
most of their text), and passed sector_flags through as
the raw JSON string so the sector filter could never fail. Every model-facing
input now comes from scoring.build_prompt_inputs / scoring.prompt_job, and the
population from scoring.tier1_partition — the same functions run_scoring calls.

Populations (--population):
  write   (default) exactly the rows --rescore-all writes: active jobs that pass
          Tier 1. This is the population a rescore decision is about. AI-scored
          rows that are closed/dismissed/applied keep their old-rubric score;
          the report names how many.
  scored  every row carrying an AI score whatever its status — a bigger n for
          rubric-SHAPE questions (per-criterion anchor usage), but it includes
          jobs no rescore can write. --ids implies this, so any scored row can
          be spot-checked.

The baseline is computed from the population's own stored scores, not
hardcoded: a fixed constant silently compares against a different set of jobs
the moment the population changes. For the record, the pre-redesign scorer
collapsed most of the board onto a handful of scores — a mid-band pile-up
with ties on every emitted field — which is the defect this report exists
to catch.

Opt-in live: exits 2 if ANTHROPIC_API_KEY is unset. ~38 Haiku calls ≈ $0.25 on
the write set, ~69 ≈ $0.45 on the scored set.

Usage: .venv/bin/python scripts/score_distribution.py
           [--population write|scored] [--limit N] [--ids a,b,c] [--dump PATH]
Exit codes: 0 = report printed, 2 = no API key or empty population.
"""

import argparse
import asyncio
import json
import os
import statistics
import sys
from collections import Counter
from pathlib import Path


from dotenv import load_dotenv  # noqa: E402

from jshq import db, usage  # noqa: E402
from jshq.scoring import (  # noqa: E402
    POSITIVE_FIT,
    SCORE_CONCURRENCY,
    build_prompt_inputs,
    consistency,
    derive,
    escalate,
    function_check_flag,
    haiku,
    is_ic_designated,
    prompt_job,
    tier1_partition,
)
from jshq.scoring.criteria import load_criteria  # noqa: E402
from jshq.scoring.geo import read_drive_times  # noqa: E402
from jshq.scoring.tier1 import evaluate_tier1  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(REPO_ROOT / ".env")

MIDDLE = (45, 55)
# Below this the baseline comparison and the anchor check are noise: a criterion
# cannot show 3 distinct values across 3 jobs however well its anchors work.
MIN_MEANINGFUL_N = 20
# fit_score 0 is the Tier-1 hard-fail sentinel (frontend isHardFailFit), and
# aggregate() now floors at 1 — so any AI-scored row sitting on 0 today leaves
# the sentinel on a rescore and re-enters Today, the active count and the top of
# the list. That is a visible board change and the report names it.
SENTINEL = 0

_SCORED_SQL = """
    SELECT jobs.*, companies.name AS company_name, companies.sector_flags
    FROM jobs JOIN companies ON companies.id = jobs.company_id
    WHERE jobs.score_detail IS NOT NULL
      AND jobs.description_text IS NOT NULL
    ORDER BY jobs.id
"""


def histogram(values, width=48):
    counts = Counter(values)
    top = max(counts.values())
    for value in sorted(counts, reverse=True):
        n = counts[value]
        print(f"  {value:>3}  {'#' * max(1, round(n * width / top)):<{width}} {n}")


def select_population(conn, criteria, drive_times, population, ids):
    """[(job, tier1)] for the requested population, Tier-1 hard fails removed —
    the pipeline never sends those to the model either.

    `write` delegates to the pipeline's own selector so it cannot drift from
    what --rescore-all writes. `scored` needs its own query (it deliberately
    includes non-active rows) and therefore its own Tier-1 replay; note the
    json.loads on sector_flags — passing the raw JSON string makes
    `{norm(s) for s in sector_flags}` iterate characters, so no sector ever
    matches and the filter silently cannot fail.
    """
    if ids or population == "scored":
        jobs = [dict(r) for r in conn.execute(_SCORED_SQL).fetchall()]
        if ids:
            wanted = set(ids)
            missing = wanted - {j["id"] for j in jobs}
            if missing:
                print(f"!! ids not found or not AI-scored: {sorted(missing)}")
            jobs = [j for j in jobs if j["id"] in wanted]
        pairs = []
        for job in jobs:
            flags = json.loads(job["sector_flags"]) if job["sector_flags"] else []
            pairs.append((job, evaluate_tier1(job, flags, criteria.params, drive_times)))
    else:
        pairs = tier1_partition(conn, criteria, drive_times, only_pending=False)[0]
        pairs = [(dict(job), tier1) for job, tier1 in pairs]
    return [(job, t1) for job, t1 in pairs if not t1.hard_fail]


def baseline(results) -> dict:
    """The population's own stored (old-rubric) numbers.

    Computed, never hardcoded: a fixed constant compares the run against a
    different set of jobs the moment the population changes, which is exactly
    how the 2026-08-08 run ended up measuring 69 rows to approve a write of 38.

    An old row was capped iff its stored final plus its stored deductions still
    falls short of its stored model_score — apply_adjustments deducts from the
    CAPPED score, so that arithmetic is exact, and unlike reading the cap/
    function_cap/band_cap keys it does not depend on which key an older _write
    happened to record. (Verified: the two agree 4/4 on today's write set.)
    """
    rows = [r for r in results if r["old"] is not None and r["old_model"] is not None]
    finals = [r["old"] for r in rows]
    uncapped = [
        r["old"] for r in rows
        if r["old"] + sum(r["old_deductions"].values()) >= r["old_model"]
    ]
    tie = Counter(uncapped).most_common(1)[0] if uncapped else (0, 0)
    return {
        "n": len(rows),
        "distinct_finals": len(set(finals)),
        "distinct_model": len({r["old_model"] for r in rows}),
        "max": max(finals) if finals else 0,
        "middle": sum(1 for f in uncapped if MIDDLE[0] <= f <= MIDDLE[1]),
        "positive": sum(1 for f in finals if f >= POSITIVE_FIT),
        "tie": tie,
        "sentinel": sum(1 for f in finals if f == SENTINEL),
    }


async def score_one(client, system, criteria, job, tier1, sem):
    async with sem:
        data, used = await haiku.score_job(client, system, job, criteria)
        usages = [used] if used is not None else []

        async def ask():
            return await haiku.score_job(client, system, job, criteria)

        # The write path escalates ambiguous rows (near threshold / cap flip),
        # so the measurement escalates identically — same triggers, same extra
        # calls, same cost, or it approves a different operation.
        data, extra = await escalate(job, tier1, data, criteria, ask)
        usages += extra
    # scoring.derive is the pipeline's own arithmetic — this script used to
    # hand-mirror it (aggregate → IC override → caps → deductions) and the
    # mirror is exactly the drift class build_prompt_inputs closed on the
    # prompt side, so it consumes the shared function now.
    d = derive(job, tier1, data, criteria)
    # A `write`-population job may never have been AI-scored (a brand-new
    # posting), so there is no old row to compare against — baseline() skips
    # those rather than inventing a zero.
    old_detail = json.loads(job["score_detail"]) if job["score_detail"] else {}
    return {
        "id": job["id"], "title": job["title"], "company": job["company_name"],
        "status": job["status"],
        "old": job["fit_score"] if job["score_detail"] else None,
        "old_model": old_detail.get("model_score"),
        "old_deductions": old_detail.get("deductions") or {},
        "old_leads": old_detail.get("leads_discipline"),
        "old_mgmt": old_detail.get("management_type"),
        "model_score": d["model_score"], "final": d["final"],
        "capped": d["capped"] != d["model_score"],
        "cap": d["cap"], "mgmt": d["mgmt"], "leads": d["leads"],
        "func_flag": d["func_flag"], "flags": d["flags"],
        "deductions": d["deductions"],
        "evidenced": d["evidenced"], "lean": d["lean"],
        "subscores": data["tier2"], "usages": usages,
        # the raw model payload, kept so the sibling pass can re-derive a
        # corrected row exactly as _write would; never dumped
        "data": data,
    }


def apply_sibling_pass(results, scored_jobs, stored_voters, criteria):
    """Mirror run_scoring's consistency pass on the in-memory predictions.

    Same member shape, same corrections() call, same re-derivation — the
    measured distribution must include the overrides the write path would
    apply, or the two disagree on exactly the rows the pass exists to fix.
    """
    jobs_by_id = {job["id"]: (job, tier1) for job, tier1 in scored_jobs}
    members = []
    for r in results:
        job, _ = jobs_by_id[r["id"]]
        members.append({
            "id": r["id"], "company_id": job["company_id"],
            "text": job["description_text"],
            "reads": {
                "leads_discipline": r["data"]["leads_discipline"],
                "management_type": r["data"]["management_type"],
            },
            "ic_designated": is_ic_designated(job["title"], job["level_band"]),
            "fresh": True,
        })
    members += stored_voters
    corrections = consistency.corrections(members)
    results_by_id = {r["id"]: r for r in results}
    for corr in corrections:
        r = results_by_id[corr["id"]]
        job, tier1 = jobs_by_id[corr["id"]]
        r["data"][corr["field"]] = corr["to"]
        r["data"]["near_miss_flags"] = sorted(
            set(r["data"]["near_miss_flags"]) | {"sibling_override"}
        )
        d = derive(job, tier1, r["data"], criteria)
        r.update({
            "model_score": d["model_score"], "final": d["final"],
            "capped": d["capped"] != d["model_score"],
            "cap": d["cap"], "mgmt": d["mgmt"], "leads": d["leads"],
            "func_flag": d["func_flag"], "flags": d["flags"],
            "deductions": d["deductions"],
        })
    return corrections


async def run(limit: int | None, ids: list[int] | None, dump: str | None,
              population: str) -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set (.env in the data dir, or your shell) — this needs a live key.")
        return 2

    from anthropic import AsyncAnthropic

    criteria = load_criteria()
    # --ids reaches into the scored set so closed/dismissed rows are checkable;
    # normalize here so the header and the residue line say what actually ran.
    if ids:
        population = "scored"
    conn = db.connect()
    try:
        # Same commute data the pipeline uses, so the Tier-1 replay can only
        # reach the same verdict it did in production.
        drive_times = read_drive_times(conn)
        scored_jobs = select_population(conn, criteria, drive_times, population, ids)
        if limit:
            scored_jobs = scored_jobs[:limit]
        if not scored_jobs:
            # e.g. --ids naming only never-AI-scored rows (Tier-1 hard-fail
            # sentinels have fit_score=0 and no score_detail), or rows whose
            # Tier-1 replay hard-fails them out of the population.
            print("nothing to score — population is empty")
            return 2
        # Every model-facing input from the pipeline's own construction: the
        # dismissal digest, the learned rules, and the per-company shared-
        # boilerplate fingerprints. Fingerprints are keyed on the jobs actually
        # being scored, exactly as run_scoring keys them.
        system, shared = build_prompt_inputs(conn, criteria, [j for j, _ in scored_jobs])
        # Rows a rescore cannot reach: AI-scored but not in the write set, so
        # they keep their old-rubric score and the board runs two rubrics.
        residue = conn.execute(
            "SELECT count(*) FROM jobs WHERE score_detail IS NOT NULL AND status != 'active'"
        ).fetchone()[0]
        # Stored active siblings that vote in the consistency pass but are
        # outside the population — fetched now, the connection closes below.
        stored_voters = consistency.stored_members(
            conn,
            {j["company_id"] for j, _ in scored_jobs},
            {j["id"] for j, _ in scored_jobs},
        )
    finally:
        conn.close()

    label = "the rows --rescore-all writes" if population == "write" else "every AI-scored row"
    print(f"scoring {len(scored_jobs)} jobs — population '{population}' ({label})")
    print("READ-ONLY — nothing is written\n")
    client = AsyncAnthropic(max_retries=6)
    sem = asyncio.Semaphore(SCORE_CONCURRENCY)
    settled = await asyncio.gather(
        *(score_one(client, system, criteria, prompt_job(j, shared), t1, sem)
          for j, t1 in scored_jobs),
        return_exceptions=True,
    )
    results = [r for r in settled if not isinstance(r, Exception)]
    errors = [r for r in settled if isinstance(r, Exception)]
    if errors:
        print(f"!! {len(errors)} job(s) failed to score: {errors[0]}\n")

    escalated = [r for r in results if r["data"].get("escalation")]
    if escalated:
        extra_calls = sum(len(r["usages"]) - 1 for r in results)
        print(f"escalation: {len(escalated)} row(s) re-read (+{extra_calls} calls)")
        for r in escalated:
            esc = r["data"]["escalation"]
            print(f"  id {r['id']}: {esc['reads']} reads — {'; '.join(esc['outcomes'])}")
        print()

    corrections = apply_sibling_pass(results, scored_jobs, stored_voters, criteria)
    if corrections:
        print(f"sibling consistency: {len(corrections)} read(s) corrected")
        for c in corrections:
            field = "leads" if c["field"] == "leads_discipline" else "management"
            print(f"  id {c['id']}: {field} {c['from']} -> {c['to']} "
                  f"({c['agree']} of {c['size']} agree)")
        print()

    finals = [r["final"] for r in results]
    models = [r["model_score"] for r in results]

    print("=" * 64)
    print(f"FINAL SCORES (n={len(finals)})")
    print("=" * 64)
    histogram(finals)

    # The pile-up test must EXCLUDE capped jobs. A cap pins every job above it
    # onto one integer, so counting capped rows measures the ceiling, not the
    # model — and it is gameable in the wrong direction: LOWERING a cap pins
    # MORE jobs (#{u >= c} is non-increasing in c) while driving this metric to
    # a perfect zero. Uncapped rows are the only ones whose position the
    # aggregation chose.
    uncapped = [r for r in results if not r["capped"]]
    capped = [r for r in results if r["capped"]]
    middle = [r["final"] for r in uncapped if MIDDLE[0] <= r["final"] <= MIDDLE[1]]
    positive = [f for f in finals if f >= POSITIVE_FIT]

    def verdict(new, base, higher_is_better=True):
        better = new > base if higher_is_better else new < base
        return "better" if better else ("same" if new == base else "WORSE")

    if len(results) < MIN_MEANINGFUL_N:
        print(f"\n  NOTE: n={len(results)} is a smoke test. The baseline comparison and "
              f"the\n  anchor check below only mean anything at n >= {MIN_MEANINGFUL_N}.")

    base = baseline(results)
    if base["n"] < len(results):
        print(f"\n  NOTE: {len(results) - base['n']} job(s) carry no stored score — "
              "they are excluded from\n  the baseline column but counted in 'now'.")

    print(f"\n{'metric':<34} {'baseline':>9} {'now':>6}   verdict")
    print(f"{'-' * 34} {'-' * 9} {'-' * 6}   {'-' * 7}")
    uncapped_finals = [r["final"] for r in uncapped]
    biggest_uncapped = Counter(uncapped_finals).most_common(1)[0] if uncapped else (0, 0)
    rows = [
        ("distinct final scores", base["distinct_finals"], len(set(finals)), True),
        ("distinct pre-cap scores", base["distinct_model"], len(set(models)), True),
        ("max score produced", base["max"], max(finals), True),
        (f"UNCAPPED pile-up in [{MIDDLE[0]},{MIDDLE[1]}]", base["middle"], len(middle), False),
        (f"jobs >= {POSITIVE_FIT} (positive fit)", base["positive"], len(positive), True),
    ]
    for label, was, now, higher in rows:
        print(f"{label:<34} {was:>9} {now:>6}   {verdict(now, was, higher)}")
    was_tie = f"{base['tie'][1]} at {base['tie'][0]}"
    now_tie = f"{biggest_uncapped[1]} at {biggest_uncapped[0]}"
    print(f"{'largest UNCAPPED tie':<34} {was_tie:>9} {now_tie:>6}")
    print(f"{'min / median / max':<34} {'':>9} "
          f"{f'{min(finals)} / {statistics.median(finals):g} / {max(finals)}':>6}")

    # What a rescore would change about the BOARD, as opposed to the spread.
    leaving = [r for r in results if r["old"] == SENTINEL and r["final"] > SENTINEL]
    if leaving:
        print(f"\n  {len(leaving)} job(s) leave the fit_score={SENTINEL} hard-fail sentinel "
              "(aggregate floors at 1).")
        print("  These re-enter Today, the active count and the top of the list:")
        for r in sorted(leaving, key=lambda r: -r["final"]):
            print(f"    {r['final']:>3}  {r['company'][:20]:<20} {r['title'][:44]}")
    if population == "write" and residue:
        print(f"\n  {residue} AI-scored job(s) are not active — --rescore-all leaves them "
              "on the OLD\n  rubric, so the board would run two rubrics until they close out.")

    # The capped cohort separately: fit_score deliberately collapses it, so its
    # resolution lives in model_score (exposed on the jobs list as "Fit (pre-cap)").
    if capped:
        cap_models = [r["model_score"] for r in capped]
        by_cap = Counter(r["cap"] for r in capped)
        print(f"\n  capped cohort: {len(capped)} jobs on "
              f"{len({r['final'] for r in capped})} distinct fit_score(s) "
              f"{sorted({r['final'] for r in capped})}")
        print(f"    ...but {len(set(cap_models))} distinct pre-cap scores "
              f"({min(cap_models)}-{max(cap_models)}) — the ordering fit_score discards")
        print(f"    ceilings hit: {dict(sorted(by_cap.items()))}")

    # leads_discipline confusion vs the stored baseline — the function check is
    # the one thing here that is correctness rather than resolution.
    shifts = Counter(
        (r["old_leads"] or "(absent)", r["leads"]) for r in results if r["old_leads"] != r["leads"]
    )
    if shifts:
        print("\n  leads_discipline changes vs baseline:")
        for (old, new), n in shifts.most_common():
            print(f"    {old:>10} -> {new:<10} {n}")
    wrong = [r for r in results if r["func_flag"] == "wrong_function"]
    # Baseline from the population's own stored reads, for the same reason the
    # score baseline is computed: a hardcoded count silently compares against a
    # different set of jobs.
    had_read = [r for r in results if r["old_leads"]]
    was_wrong = [
        r for r in had_read
        if function_check_flag(r["old_leads"], r["old_mgmt"]) == "wrong_function"
    ]
    print(f"  wrong_function reads: {len(wrong)}/{len(results)} "
          f"(baseline: {len(was_wrong)} of the {len(had_read)} rows carrying a read)")

    print("\n" + "=" * 64)
    print("PER-CRITERION VALUE USAGE  — the risk this design cannot argue away")
    print("=" * 64)
    print("A criterion using fewer than 3 of its 5 values has broken anchors:")
    print("the model is habituating one level down instead of judging.\n")
    weights = [i["weight"] for i in criteria.tier2]
    broken = []
    for n in range(1, criteria.tier2_count + 1):
        vals = [r["subscores"].get(n) for r in results]
        counts = Counter("null" if v is None else v for v in vals)
        used = len({v for v in vals if v is not None})
        cells = "  ".join(
            f"{k if k == 'null' else f'{k:+d}'}:{counts[k]:<3}"
            for k in sorted(counts, key=lambda x: (x == "null", x))
        )
        thin = used < 3 and len(results) >= MIN_MEANINGFUL_N
        print(f"  c{n:<3} w={weights[n - 1]:<5} {cells}{'  <-- BROKEN' if thin else ''}")
        if thin:
            broken.append(n)

    ev = [r["evidenced"] for r in results]
    print(f"\n  evidenced criteria per job: median {statistics.median(ev):g}, "
          f"min {min(ev)}, max {max(ev)}")
    if broken:
        print(f"\n  !! criteria {broken} used fewer than 3 distinct values — "
              "their anchors need rewriting.")

    print("\n" + "=" * 64)
    print("THE SEVEN 82s — previously tied on every emitted field")
    print("=" * 64)
    eighty_twos = sorted(
        (r for r in results if r["old_model"] == 82), key=lambda r: -r["final"]
    )
    if eighty_twos:
        for r in eighty_twos:
            print(f"  {r['final']:>3}  (was {r['old']:>3})  "
                  f"{r['company'][:22]:<22} {r['title'][:40]}")
        spread = eighty_twos[0]["final"] - eighty_twos[-1]["final"]
        print(f"\n  spread: {spread} points across {len(eighty_twos)} jobs "
              f"({len({r['final'] for r in eighty_twos})} distinct), was 0")

    print("\n" + "=" * 64)
    print("BIGGEST MOVERS")
    print("=" * 64)
    for r in sorted(results, key=lambda r: -abs(r["final"] - (r["old"] or 0)))[:10]:
        delta = r["final"] - (r["old"] or 0)
        print(f"  {r['old']:>3} -> {r['final']:>3}  ({delta:+4d})  "
              f"{r['company'][:20]:<20} {r['title'][:38]}")

    acc = {}
    calls = 0
    for r in results:
        for u in r["usages"]:
            calls += 1
            for field in ("input_tokens", "output_tokens",
                          "cache_read_input_tokens", "cache_creation_input_tokens"):
                value = getattr(u, field, None)
                if value:
                    acc[field] = acc.get(field, 0) + value
    if acc:
        cost = usage.cost_of(haiku.MODEL, acc)
        print(f"\ncost: ${cost:.4f} over {calls} calls "
              f"({acc.get('output_tokens', 0) // max(1, calls)} output tokens/call)")
        ledger = usage.append_harness_ledger(
            "score_distribution", haiku.MODEL, acc, calls=calls, cost=cost,
            args=" ".join(sys.argv[1:]),
        )
        print(f"spend appended to {ledger} (the DB ledger never sees harness runs)")
    if dump:
        payload = [{k: v for k, v in r.items() if k not in ("usages", "data")} for r in results]
        # int keys -> str: subscores round-trips through JSON like score_detail does
        for row in payload:
            row["subscores"] = {str(n): v for n, v in sorted(row["subscores"].items())}
        Path(dump).write_text(json.dumps(payload, indent=1), encoding="utf-8")
        print(f"\nper-job detail written to {dump} ({len(payload)} rows)")

    print("\nNothing was written to the DB. To apply: score_backfill.py --rescore-all")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--population", choices=("write", "scored"), default="write",
        help="write (default) = exactly what --rescore-all writes; "
             "scored = every AI-scored row, bigger n for rubric-shape questions",
    )
    parser.add_argument("--limit", type=int, help="score only the first N jobs (a cheap smoke test)")
    parser.add_argument(
        "--ids",
        help="comma-separated job ids — a targeted re-check, cents not dollars. "
             "Implies --population scored, so closed/dismissed rows are reachable",
    )
    parser.add_argument("--dump", help="write per-job detail (subscores, caps, flags) to this JSON path")
    args = parser.parse_args()
    wanted = [int(i) for i in args.ids.split(",")] if args.ids else None
    sys.exit(asyncio.run(run(args.limit, wanted, args.dump, args.population)))
