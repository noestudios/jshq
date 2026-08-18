"""Fit scoring orchestrator (Phase 4).

Pending = active jobs with tier1_results IS NULL: every evaluated job gets
tier1_results written (hard fails included), so NULL is the "needs scoring"
marker and the refresh pipeline clears it when a description changes.

Failure contract mirrors the adapters: a job that errors stays NULL and is
retried next refresh; a missing key or broken criteria doc skips scoring
entirely and reports why — the refresh itself is never blocked.
"""

import asyncio
import json
import logging
import math
import re
import sqlite3

from .. import apikey, usage
from . import boilerplate, consistency, haiku
from .criteria import BAND_CAP_KEYS, CriteriaError, load_criteria
from .digest import build_dismissal_digest
from .geo import read_drive_times
from .tier1 import evaluate_tier1

log = logging.getLogger("jshq.scoring")

# Deliberately low: Anthropic Tier-1 caps at ~50 req/min and a full rescore
# would otherwise burst past it. The injected client also carries max_retries=6
# so a transient 429 recovers (SDK honors retry-after) instead of dropping a job.
SCORE_CONCURRENCY = 2
COMMIT_EVERY = 10

_PENDING_SQL = """
    SELECT jobs.*, companies.name AS company_name, companies.sector_flags
    FROM jobs JOIN companies ON companies.id = jobs.company_id
    WHERE jobs.status IN ({statuses}){pending}
    ORDER BY jobs.id
"""

# The refresh pipeline and --rescore-all both mean "the live board" by default.
# Other statuses are reachable (2026-08-08: the applied/dismissed rows still
# carried old-rubric scores after a rescore, and 82/72 — the OLD model's two
# habitual values — showed as clusters because of it), but they are never the
# default: a refresh must not spend calls re-scoring jobs that are done with.
DEFAULT_STATUSES = ("active",)


def thin_threshold(count: int) -> int:
    """A posting evidencing no more than this many of the rubric's criteria is
    too thin to rank honestly. Proportional (ceil(count/3)) rather than the old
    absolute 4, which was sized to the example doc's eleven criteria — on a
    three-item wizard list an absolute 4 flagged every job ever scored. 11 ⇒ 4,
    so the example doc's behavior (and its tests) are unchanged. Flagged, never
    deducted — the silence values already priced it."""
    return max(1, math.ceil(count / 3))

# The positive-fit threshold. Mirrors frontend/js/lib/ui.js (the "maybe" gate
# and the fit bands key on it) — change BOTH or the pipeline and the UI will
# disagree about what counts as a positive fit.
POSITIVE_FIT = 70

# A final within this of POSITIVE_FIT escalates to a second read: the measured
# temp-0 run-to-run variance is ±1–8 (2026-08-08, 15 of 38 rows moved), so this
# band is where one sample can put a job on the wrong side of the decision line.
ESCALATION_BAND = 8


def aggregate(subscores: dict, tier2_items: list, scale: dict) -> tuple[int, int]:
    """(0-100 score, evidenced count) from the model's per-criterion reads.

    The heart of the sub-score redesign (2026-08): the model no longer emits a
    score, so there is no single integer for it to habituate to — the spread
    falls out of this arithmetic instead. score = slope * sum(w_i * v_i) +
    intercept, clamped 1..100, where an unevidenced criterion contributes the
    silence value declared for it in the doc's score_scale block (default 0).

    Floors at 1, not 0: fit_score = 0 is the Tier-1 hard-fail sentinel that
    active_job_count and the frontend's isHardFailFit both key on, so an
    AI-scored job must never land there.

    Shared verbatim by the pipeline and scripts/calibrate_scoring.py.
    """
    # The .get defaults mirror criteria.DEFAULT_SCALE for block-less legacy
    # docs; per-install blocks are emitted by criteria.derive_scale/sync_scale.
    slope = float(scale.get("slope", 1.6))
    intercept = float(scale.get("intercept", 55.0))
    silence = scale.get("silence") or {}
    total = 0.0
    evidenced = 0
    for index, item in enumerate(tier2_items, 1):
        weight = float(item["weight"] if isinstance(item, dict) else 1.0)
        value = subscores.get(index)
        if value is None:
            # .get on both int and str keys: the doc block parses to ints, but a
            # hand-built scale dict in a test may carry JSON's string keys.
            value = silence.get(index, silence.get(str(index), 0.0))
        else:
            evidenced += 1
        total += weight * float(value)
    return max(1, min(100, round(slope * total + intercept))), evidenced


def apply_adjustments(
    model_score: int, flags: list[str], adjustments: dict
) -> tuple[int, dict]:
    """(final score, applied deductions) — the deduction table arithmetic, shared
    verbatim by the pipeline and scripts/calibrate_scoring.py. Set semantics:
    each flag deducts once; flags not in the table deduct nothing."""
    deductions = {f: adjustments[f] for f in flags if adjustments.get(f)}
    return max(0, min(100, model_score - sum(deductions.values()))), deductions


def _in_band(criteria) -> frozenset:
    """The discipline(s) this search is FOR, from the doc's taxonomy block."""
    return frozenset(criteria.taxonomy["in_band_disciplines"])


def function_check_flag(
    leads_discipline: str,
    management_type: str,
    in_band: frozenset = frozenset({"design"}),
    taxonomy_is_default: bool = False,
) -> str | None:
    """Code-derived near-miss flag from the model's leads_discipline read
    (function check, 2026-07): a role in a non-design discipline is
    wrong_function; an unclear read is function_unclear (manual review, never
    a pass); design derives nothing. An ic role's unclear read also derives
    nothing — an IC seat has no led discipline and the IC cap already owns it
    (the first rescore put a noise chip on all 27 IC designer postings) —
    though wrong_function still applies to ics: an IC product-manager seat is
    still the wrong function. Deriving in code — rather than letting the
    model emit the flag — means it can never be forgotten or misnamed.

    On a blank-slate install (no taxonomy block yet, taxonomy_is_default) the
    user has not declared their field, so no role is the "wrong function":
    short-circuit to None. The wizard's field step / a Settings taxonomy re-arms
    it. Shared with scripts/calibrate_scoring.py."""
    if taxonomy_is_default:
        return None
    if leads_discipline in in_band:
        return None
    if leads_discipline == "unclear":
        return None if management_type == "ic" else "function_unclear"
    return "wrong_function"


def effective_cap(
    caps: dict, mgmt: str, func_flag: str | None, band: str | None = None,
    cappable_bands: frozenset = BAND_CAP_KEYS,
) -> int | None:
    """Lowest applicable score ceiling: the management-type cap, the
    function-check cap, and/or the level-band cap, whichever is lowest; None
    when none applies. The band lookup is gated on cappable_bands — the doc's
    own emittable bands minus the management-type/function namespaces
    (criteria.cappable_bands), because the caps table shares one namespace and
    an "ic" LEVEL band must never read the "ic" MANAGEMENT-type key as a band
    cap. Callers pass criteria.cappable_bands; the default is the legacy set
    so a bare call still behaves."""
    applicable = [
        c
        for c in (
            caps.get(mgmt),
            caps.get(func_flag) if func_flag else None,
            caps.get(band) if band in cappable_bands else None,
        )
        if c is not None
    ]
    return min(applicable) if applicable else None


_IC_TITLE = re.compile(r"\bindividual[\s-]+contributor\b", re.I)


def is_ic_designated(title: str | None, level_band: str | None) -> bool:
    """Categorical IC designation (2026-07 verdict): an ic-banded title or an
    explicit "individual contributor" phrase in the title makes the role ic
    regardless of JD language — the model's read never promotes a designated
    IC seat to people_leader. Shared with scripts/calibrate_scoring.py."""
    return level_band == "ic" or bool(_IC_TITLE.search(title or ""))


def _tension_label(craft_lean: int) -> str:
    """Display label derived from the committed lean. Deriving (rather than
    letting the model emit a separate enum) makes lean/tension contradictions
    structurally impossible; the lean is kept in score_detail.
    |lean| <= 1 reads as genuinely both threads -> mixed."""
    if craft_lean >= 2:
        return "teach_craft"
    if craft_lean <= -2:
        return "convert_sell"
    return "mixed"


def derive_craft_lean(subscores: dict, craft_criterion: int | None) -> int:
    """craft_lean from the `[craft]` criterion, the craft-versus-convert axis.

    Was a separately emitted -5..+5 field; it drove the score almost entirely
    (lean +4 -> 82 in 7 of 7 cases) and, now that criterion 5 scores the same
    axis, emitting both would measure it twice and eventually contradict itself.
    Doubling maps {-2..+2} onto {-4..+4}, which keeps _tension_label's +/-2
    thresholds — and therefore the "[tension: x] " prefix the frontend parses —
    working unchanged. The craft criterion is never null (haiku._parse_tier2
    enforces it); 0 is the defensive default.

    craft_criterion is passed explicitly rather than defaulted because None is
    meaningful: a doc may declare no craft axis at all, and the lean is then 0.
    _tension_label reads 0 as "mixed", so the "[tension: x] " prefix the
    frontend parses still renders.
    """
    if craft_criterion is None:
        return 0
    return 2 * (subscores.get(craft_criterion) or 0)


def derive(job, tier1, score: dict, criteria) -> dict:
    """The complete per-job scoring arithmetic — pure, no I/O, no writes.

    Aggregate the per-criterion sub-scores, then the categorical IC override,
    then the score-caps ceiling — the lowest of the management-type cap (keyed
    on the FINAL management_type, so a correctly-read ic on a non-ic-banded
    title caps too) and the function-check cap (keyed on the code-derived
    wrong_function / function_unclear flag) — then deductions. Cap-before-deduct
    keeps every named flag visible in the arithmetic instead of silently
    absorbed by the ceiling.

    Extracted from _write (2026-08-08) for two reasons: escalation needs the
    final score BEFORE anything is written, and both harness scripts used to
    hand-mirror this block — the same drift class build_prompt_inputs closed on
    the prompt side. _write, scripts/score_distribution.py and
    scripts/calibrate_scoring.py all consume this one implementation.

    `job` needs only "title" and "level_band"; `tier1` only .near_miss_flags.
    """
    caps = criteria.caps
    subscores = score["tier2"]
    model_score, evidenced = aggregate(subscores, criteria.tier2, criteria.scale)
    model_mgmt = score["management_type"]
    mgmt = "ic" if is_ic_designated(job["title"], job["level_band"]) else model_mgmt
    leads = score["leads_discipline"]
    func_flag = function_check_flag(leads, mgmt, _in_band(criteria), criteria.taxonomy_is_default)
    flags = sorted(
        set(tier1.near_miss_flags)
        | set(score["near_miss_flags"])
        | ({func_flag} if func_flag else set())
        # Not a deduction: the silence values already priced the missing
        # evidence. This only marks the score as resting on very little. The
        # full-coverage guard matters at tiny rubric sizes: on a one-item list
        # threshold(1)=1 equals the whole rubric, so without it every job —
        # including one whose single criterion is evidenced with a +2 quote —
        # carried the flag forever.
        | (
            {"thin_posting"}
            if evidenced <= thin_threshold(len(criteria.tier2))
            and evidenced < len(criteria.tier2)
            else set()
        )
        # The model's own uncertainty about its criterion-5 and management
        # reads, made consequential (2026-08-08: it was stored and read by
        # nothing). Derived-in-code like thin_posting; visibility only — the
        # key never enters the adjustments table, so it deducts nothing.
        | ({"low_confidence"} if score["confidence"] == "low" else set())
    )
    func_cap = caps.get(func_flag) if func_flag else None
    band_cap = (
        caps.get(job["level_band"])
        if job["level_band"] in criteria.cappable_bands
        else None
    )
    cap = effective_cap(
        caps, mgmt, func_flag, job["level_band"], criteria.cappable_bands
    )
    capped = min(model_score, cap) if cap is not None else model_score
    final, deductions = apply_adjustments(capped, flags, criteria.adjustments)
    return {
        "model_score": model_score, "evidenced": evidenced,
        "mgmt": mgmt, "model_mgmt": model_mgmt, "leads": leads,
        "func_flag": func_flag, "func_cap": func_cap, "band_cap": band_cap,
        "cap": cap, "capped": capped, "final": final,
        "deductions": deductions, "flags": flags,
        "lean": derive_craft_lean(subscores, criteria.craft_criterion),
    }


def _stored_reads(job) -> dict | None:
    """The stored categorical reads a rescore compares against — None for a
    first-time score or a stored row missing either read."""
    raw = job["score_detail"]
    if not raw:
        return None
    detail = json.loads(raw)
    leads, mgmt = detail.get("leads_discipline"), detail.get("management_type")
    if not leads or not mgmt:
        return None
    return {"leads_discipline": leads, "management_type": mgmt}


async def escalate(job, tier1, first: dict, criteria, ask):
    """Extra reads for rows where one temp-0 sample is not enough (2026-08-08).

    Two triggers, both evaluated on the first read:

    - NEAR THRESHOLD: the derived final lands within ESCALATION_BAND of
      POSITIVE_FIT. A second read follows; if both land on the same side of
      the threshold the first stands, otherwise a third read decides — the
      read with the median final is kept whole (never a blend, so the stored
      subscores always reproduce the stored score).
    - CAP-CHANGING FLIP (rescores only): the fresh leads_discipline or final
      management_type differs from the stored row's AND the effective cap
      changes with it. A cap never flips on one read: the second read either
      agrees with the first (flip confirmed) or the stored value is kept.
      Resolution is per field, applied as an override on the kept read —
      subscores always come from one actual read.

    Best-effort: a failed extra read keeps the first read rather than failing
    the job; the failure contract (row left unscored, retried next refresh)
    belongs to read 1 alone. Returns (data, extra_usages) — data carries an
    "escalation" key (reads count + outcomes) whenever an extra read ran,
    which _write copies into score_detail.
    """
    d1 = derive(job, tier1, first, criteria)
    stored = _stored_reads(job)
    r1 = {"leads_discipline": d1["leads"], "management_type": d1["mgmt"]}
    flipped = []
    if stored:
        old_flag = function_check_flag(
            stored["leads_discipline"], stored["management_type"], _in_band(criteria),
            criteria.taxonomy_is_default,
        )
        old_cap = effective_cap(
            criteria.caps, stored["management_type"], old_flag, job["level_band"],
            criteria.cappable_bands,
        )
        if old_cap != d1["cap"]:
            flipped = [f for f in consistency.FIELDS if r1[f] != stored[f]]
    near = abs(d1["final"] - POSITIVE_FIT) <= ESCALATION_BAND
    if not flipped and not near:
        return first, []

    usages = []

    async def ask_or_none():
        try:
            data, used = await ask()
        except Exception as exc:
            log.warning(
                "escalation read failed for job %s: %s: %s — keeping the "
                "earlier read", job["id"], type(exc).__name__, exc,
            )
            usages.extend(usage.usages_of(exc))  # a failed escalation read still spent tokens
            return None
        if used is not None:
            usages.append(used)
        return data

    second = await ask_or_none()
    if second is None:
        return first, usages
    reads = 2

    outcomes = []
    overrides = {}
    if flipped:
        d2 = derive(job, tier1, second, criteria)
        r2 = {"leads_discipline": d2["leads"], "management_type": d2["mgmt"]}
        for f in flipped:
            adopted = r1[f] if r2[f] == r1[f] else stored[f]
            overrides[f] = adopted
            label = "leads" if f == "leads_discipline" else "management"
            if adopted == stored[f]:
                outcomes.append(
                    f"{label} flip {stored[f]} → {r1[f]} unconfirmed; kept {stored[f]}"
                )
            else:
                outcomes.append(f"{label} flip {stored[f]} → {adopted} confirmed")

    candidates = [dict(c, **overrides) if overrides else c for c in (first, second)]
    finals = [derive(job, tier1, c, criteria)["final"] for c in candidates]
    if near:
        outcomes.append("near threshold")
        if (finals[0] >= POSITIVE_FIT) != (finals[1] >= POSITIVE_FIT):
            third = await ask_or_none()
            if third is not None:
                reads = 3
                candidates.append(dict(third, **overrides) if overrides else third)
                finals.append(derive(job, tier1, candidates[2], criteria)["final"])
        if len(candidates) == 3:
            chosen = candidates[sorted(range(3), key=lambda i: (finals[i], i))[1]]
        else:
            chosen = candidates[0]
    else:
        chosen = candidates[0]

    chosen = dict(chosen)  # candidates may alias `first`; never mutate a read
    chosen["escalation"] = {"reads": reads, "outcomes": outcomes}
    chosen["scoring_notes"] += f"\n- Escalated: {reads} reads ({'; '.join(outcomes)})"
    return chosen, usages


def _write(
    conn: sqlite3.Connection, job, tier1, score: dict | None, criteria
) -> None:
    if score is None:  # hard Tier 1 fail — no AI call
        failed = [k for k in ("comp", "location", "sector") if getattr(tier1, k) == "fail"]
        conn.execute(
            """UPDATE jobs SET fit_score = 0, fit_quadrant = NULL, tier1_results = ?,
                   near_miss_flags = ?, scoring_notes = ?, score_detail = NULL
               WHERE id = ?""",
            (tier1.as_json(), json.dumps([]), f"Tier 1 fail: {', '.join(failed)}", job["id"]),
        )
        return
    d = derive(job, tier1, score, criteria)
    model_score, evidenced, final = d["model_score"], d["evidenced"], d["final"]
    mgmt, model_mgmt, leads = d["mgmt"], d["model_mgmt"], d["leads"]
    func_flag, func_cap, band_cap = d["func_flag"], d["func_cap"], d["band_cap"]
    cap, capped, deductions, flags, lean = (
        d["cap"], d["capped"], d["deductions"], d["flags"], d["lean"]
    )
    # Notes keep the "[tension: x] " prefix contract the frontend regex parses,
    # then surface the structured read + any cap/deduction arithmetic as
    # ordinary markdown bullets — no frontend changes needed. Uncapped,
    # unoverridden jobs render byte-identically to the pre-cap format.
    # The prefix exists only when the doc HAS a craft axis: a wizard-built
    # rubric with no [craft] item would otherwise stamp a permanent, meaningless
    # "mixed" tension on every job (the frontend regex simply not matching is
    # the designed no-axis rendering). criteria=None keeps the legacy contract.
    if criteria is None or criteria.craft_criterion is not None:
        notes = f"[tension: {_tension_label(lean)}] {score['scoring_notes']}"
    else:
        notes = score["scoring_notes"]
    read = mgmt if mgmt == model_mgmt else f"{mgmt} (IC-designated; model read {model_mgmt})"
    # Omitted classification reads (a doc that declares no functions map / no
    # taxonomy) drop out of the bullet instead of printing "None".
    read_bits = [read]
    if score["function"]:
        read_bits.append(score["function"])
    if leads:
        read_bits.append(f"leads {leads}")
    read_bits.append(f"lean {lean:+d} ({score['confidence']} confidence)")
    notes += "\n- Read: " + " · ".join(read_bits)
    if capped < model_score:
        # Attribution precedence on ties: function check, then level band,
        # then the management-type (IC) cap — matches the detail keys below.
        if func_cap is not None and cap == func_cap:
            label = (
                f"Wrong function (leads {leads})"
                if func_flag == "wrong_function"
                else "Function unclear"
            )
            notes += f"\n- {label}: {model_score} → {capped}"
        elif band_cap is not None and cap == band_cap:
            notes += f"\n- {job['level_band'].capitalize()} band cap: {model_score} → {capped}"
        else:
            notes += f"\n- IC cap: {model_score} → {capped}"
    if deductions:
        parts = " ".join(f"- {pts} {flag}" for flag, pts in sorted(deductions.items()))
        notes += f"\n- Adjustments: {capped} {parts} → {final}"
    detail = {
        # model_score keeps its name and its meaning — the score before caps and
        # deductions — even though the model no longer authors it. Renaming would
        # break every stored row's comparability for no gain.
        "model_score": model_score,
        "deductions": deductions,
        "craft_lean": lean,
        "confidence": score["confidence"],
        "management_type": mgmt,
        "function": score["function"],
        "leads_discipline": leads,
        # str keys: this round-trips through JSON, which has no integer keys.
        "subscores": {str(n): v for n, v in sorted(score["tier2"].items())},
        # Only the criteria the model actually quoted (±2s and criterion 5) —
        # without them a stored sub-score is an unexplainable number.
        "subscore_quotes": {
            str(n): q for n, q in sorted(score.get("tier2_quotes", {}).items())
        },
        "evidenced_count": evidenced,
    }
    if mgmt != model_mgmt:
        detail["model_management_type"] = model_mgmt
    if score.get("sibling_override"):
        # The consistency pass replaced a categorical read; the original model
        # read survives here (and in the notes bullet) or it would be gone.
        detail["sibling_override"] = score["sibling_override"]
    if score.get("escalation"):
        detail["escalation"] = score["escalation"]
    if capped < model_score:
        # function_cap when the function check bound; band_cap when the level
        # band bound; cap for the mgmt cap (pre-function-check contract) —
        # ties go func > band > mgmt, mirroring the notes labels.
        if func_cap is not None and cap == func_cap:
            detail["function_cap"] = func_cap
        elif band_cap is not None and cap == band_cap:
            detail["band_cap"] = band_cap
        else:
            detail["cap"] = cap
    conn.execute(
        """UPDATE jobs SET fit_score = ?, fit_quadrant = ?, tier1_results = ?,
               near_miss_flags = ?, scoring_notes = ?, score_detail = ? WHERE id = ?""",
        (final, score["fit_quadrant"], tier1.as_json(),
         json.dumps(flags), notes, json.dumps(detail), job["id"]),
    )


def _is_rate_limit(exc) -> bool:
    """Detect an Anthropic 429 without importing the SDK error type (import-/
    version-proof): match the class name or a 429 on the attached response."""
    if exc.__class__.__name__ == "RateLimitError":
        return True
    resp = getattr(exc, "response", None)
    return getattr(resp, "status_code", None) == 429


def tier1_partition(
    conn, criteria, drive_times, *, only_pending, statuses=DEFAULT_STATUSES,
    only_scored=False, job_ids=None,
):
    """Split candidate jobs by the deterministic Tier-1 gate — no AI, no writes.
    Returns (to_score, fails) as [(job, tier1)] lists.

    Public because scripts/score_distribution.py selects its population with it:
    only_pending=False returns exactly the rows --rescore-all writes. The harness
    used to re-implement this over its own SQL and passed sector_flags through as
    the raw JSON string, so `{norm(s) for s in sector_flags}` iterated characters
    and the sector filter could never fail. Selecting with the pipeline's own
    function is the fix that cannot drift again.

    `statuses` widens the population beyond the live board — parameterized, never
    interpolated, because these values reach a WHERE clause.

    `only_scored` narrows to rows that already carry an AI score. That is the
    "get these off the old rubric" operation: widening `statuses` alone also
    picks up rows that were dismissed or applied BEFORE they were ever scored
    (16 of them, against 16 genuinely stale ones, when this was added), so it
    would double the spend to score jobs that are done with.

    `job_ids` narrows to specific rows. The details-edit endpoint needs it: it
    NULLs the fit columns of a job in ANY status, so its rescore must reach a
    non-active job WITHOUT widening the whole population to that status (which
    would sweep in the never-scored dismissed/applied rows above). Parameterized
    like `statuses` — these values reach the same WHERE clause."""
    statuses = tuple(statuses)
    extra = " AND jobs.tier1_results IS NULL" if only_pending else ""
    if only_scored:
        extra += " AND jobs.score_detail IS NOT NULL"
    params = statuses
    if job_ids:
        job_ids = tuple(job_ids)
        extra += f" AND jobs.id IN ({', '.join('?' * len(job_ids))})"
        params = statuses + job_ids
    pending = conn.execute(
        _PENDING_SQL.format(statuses=", ".join("?" * len(statuses)), pending=extra),
        params,
    ).fetchall()
    to_score, fails = [], []
    for job in pending:
        sector_flags = json.loads(job["sector_flags"]) if job["sector_flags"] else []
        tier1 = evaluate_tier1(job, sector_flags, criteria.params, drive_times)
        (fails if tier1.hard_fail else to_score).append((job, tier1))
    return to_score, fails


def build_prompt_inputs(conn, criteria, jobs) -> tuple[str, dict[int, set[str]]]:
    """(system prompt, per-company shared-boilerplate keys) — every model-facing
    input except the job row itself.

    Shared verbatim by run_scoring and scripts/score_distribution.py. It exists
    because the harness re-implemented this by hand and drifted: it measured a
    prompt with no dismissal digest and no learned rules, against unstripped JDs
    on 19 of the 38 jobs a rescore writes (six of them Applied Researcher
    postings that lose ~85% of their text to the strip AND carry a learned rule
    telling the scorer to down-rank them). **A measurement taken against a prompt
    the pipeline will not send cannot approve a rescore** — so there is one
    construction now, not two that have to be kept in step by eye.

    scripts/calibrate_scoring.py deliberately does NOT use this: it scores
    synthetic fixtures, and a calibration that shifted with whatever was
    dismissed last week would not be a calibration.
    """
    # Learned scoring-layer rules (Phase 7i) ride alongside the dismissal digest
    # as soft negative signals. Lazy import: learned.py imports this package's
    # submodules, so a top-level import would risk a cycle.
    from .learned import read_scoring_rules

    learned_rules = [r["text"] for r in read_scoring_rules(conn) if r.get("text")]
    system = haiku.build_system_prompt(criteria, build_dismissal_digest(conn), learned_rules)

    # Shared-boilerplate fingerprints per company with jobs to score, built from
    # ALL active stored descriptions (scored siblings included) so a single new
    # posting still strips against the full board. DB text is never modified —
    # prompt_job hands the model a stripped copy.
    shared_by_company: dict[int, set[str]] = {}
    for cid in {job["company_id"] for job in jobs}:
        texts = [
            r[0]
            for r in conn.execute(
                "SELECT description_text FROM jobs WHERE company_id = ?"
                " AND status = 'active' AND description_text IS NOT NULL",
                (cid,),
            )
        ]
        if len(texts) >= boilerplate.MIN_SIBLINGS:
            shared_by_company[cid] = boilerplate.shared_block_keys(texts)
    return system, shared_by_company


def prompt_job(job, shared_by_company: dict[int, set[str]]) -> dict:
    """A copy of the job row with shared company boilerplate stripped from
    description_text. The row and the DB text are both left untouched
    (boilerplate.py's contract: it strips at prompt-build time only)."""
    out = dict(job)
    out["description_text"] = boilerplate.strip_shared(
        job["description_text"], shared_by_company.get(job["company_id"], set())
    )
    return out


def estimate_rescore(conn: sqlite3.Connection) -> dict:
    """Tier-1-only dry run over all active jobs (no AI calls) — powers the
    rescore confirm modal. Raises CriteriaError if the doc won't load."""
    criteria = load_criteria()
    to_score, fails = tier1_partition(
        conn, criteria, read_drive_times(conn), only_pending=False
    )
    return {
        "active": len(to_score) + len(fails),
        "to_score": len(to_score),
        "tier1_failed": len(fails),
    }


# The skip message when the wish list (Tier 2) is empty — mirrors
# apikey.MISSING_MESSAGE, surfaced in last_scoring_report.skipped (System tab).
NO_CRITERIA_MESSAGE = (
    "No ranked criteria yet — add your wish list in the setup wizard "
    "(or Settings → Scoring) to turn on job scoring."
)


async def run_scoring(
    conn: sqlite3.Connection, *, only_pending: bool = True, client=None, on_progress=None,
    statuses=DEFAULT_STATUSES, only_scored: bool = False, job_ids=None,
) -> dict:
    """Score pending jobs. Returns a report dict; never raises for per-job
    errors. on_progress(done, total, errors) fires after each batch commit.

    `statuses` defaults to the live board. Widening it rescores rows that are
    done with (applied/dismissed/closed) — deliberate and occasionally right,
    but never automatic: the refresh pipeline must not spend calls on them.
    `job_ids` scopes the run to specific rows (the details-edit rescore, which
    must reach a non-active job without widening the population). Only fit
    columns are written either way; `status`, `manually_elevated` and the
    application records are untouched."""
    if client is None:
        if not apikey.is_configured():
            return {"skipped": apikey.MISSING_MESSAGE}
        from anthropic import AsyncAnthropic  # lazy: app must run without the package

        client = AsyncAnthropic(max_retries=6)

    try:
        criteria = load_criteria()
    except CriteriaError as exc:
        return {"skipped": f"criteria error: {exc}"}

    if not criteria.tier2:
        # Blank-slate / not-yet-built wish list: nothing to score against. Skip
        # cleanly (jobs stay listed, unscored) rather than letting
        # build_system_prompt raise on the empty Tier 2 — that raise is not caught
        # in the rescore path, so the task would die silently and the System tab
        # would show stale success.
        return {"skipped": NO_CRITERIA_MESSAGE}

    drive_times = read_drive_times(conn)  # measured per-town commute overrides (7i)
    to_score, fails = tier1_partition(
        conn, criteria, drive_times, only_pending=only_pending, statuses=statuses,
        only_scored=only_scored, job_ids=job_ids,
    )
    if not to_score and not fails:
        return {"scored": 0, "tier1_failed": 0, "errors": 0, "rate_limited": 0,
                "escalated": 0, "sibling_overrides": 0, "cost": 0.0}

    for job, tier1 in fails:  # hard Tier-1 fails — written immediately, no AI call
        _write(conn, job, tier1, None, criteria)
    conn.commit()
    tier1_failed = len(fails)
    # Progress covers every job the run touches, not just the AI-scored subset:
    # the instant gated writes count as done up front, AI scores tick in below.
    # A rescore over 181 active jobs starts at 146/181, not a confusing 0/35.
    total_all = tier1_failed + len(to_score)
    if on_progress:
        on_progress(tier1_failed, total_all, 0)
    if not to_score:
        return {"scored": 0, "tier1_failed": tier1_failed, "errors": 0, "rate_limited": 0,
                "escalated": 0, "sibling_overrides": 0, "cost": 0.0}

    system, shared_by_company = build_prompt_inputs(
        conn, criteria, [job for job, _ in to_score]
    )

    sem = asyncio.Semaphore(SCORE_CONCURRENCY)

    async def score_one(job, tier1):
        async with sem:
            try:
                data, used = await haiku.score_job(
                    client, system, prompt_job(job, shared_by_company), criteria
                )
                usages = [used] if used is not None else []

                async def ask():
                    return await haiku.score_job(
                        client, system, prompt_job(job, shared_by_company), criteria
                    )

                data, extra = await escalate(job, tier1, data, criteria, ask)
                return job, tier1, data, usages + extra, None
            except Exception as exc:  # stays unscored, retried next refresh
                log.warning(
                    "scoring failed for job %s: %s: %s", job["id"], type(exc).__name__, exc
                )
                # Bill the tokens the failed attempts spent (ScoringError carries
                # them) — a failed-parse job still made real, billable calls.
                return job, tier1, None, usage.usages_of(exc), exc

    scored = errors = rate_limited = escalated = 0
    total, done = len(to_score), 0
    acc = {
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
    }
    acc_calls = 0
    fresh = []  # (job, tier1, data) written this run — the consistency pass's input
    for start in range(0, total, COMMIT_EVERY):
        chunk = to_score[start : start + COMMIT_EVERY]
        results = await asyncio.gather(*(score_one(j, t) for j, t in chunk))
        for job, tier1, data, usages, exc in results:
            done += 1
            # Bill spent tokens regardless of outcome: a job that failed parsing
            # on both attempts still made two billable calls (their usages ride
            # on the error via usage.usages_of), so those must hit the ledger too
            # or the cost total under-reports the failed-parse subset.
            for used in usages:
                for k in acc:
                    acc[k] += getattr(used, k, 0) or 0
                acc_calls += 1
            if exc is not None:
                errors += 1
                if _is_rate_limit(exc):
                    rate_limited += 1
                continue
            _write(conn, job, tier1, data, criteria)
            scored += 1
            if data.get("escalation"):
                escalated += 1
            fresh.append((job, tier1, data))
        conn.commit()  # crash-safe batches, mirrors per-company commits
        if on_progress:
            on_progress(tier1_failed + done, total_all, errors)

    # Sibling-consistency pass (consistency.py): near-identical postings must
    # agree on the categorical reads that derive the caps. Stored active
    # siblings vote too; only rows scored THIS run are ever rewritten.
    overridden = set()
    if fresh:
        members = [
            {
                "id": job["id"], "company_id": job["company_id"],
                "text": job["description_text"],
                "reads": {
                    "leads_discipline": data["leads_discipline"],
                    "management_type": data["management_type"],
                },
                "ic_designated": is_ic_designated(job["title"], job["level_band"]),
                "fresh": True,
            }
            for job, _, data in fresh
        ]
        members += consistency.stored_members(
            conn, {job["company_id"] for job, _, _ in fresh}, {m["id"] for m in members}
        )
        by_id = {job["id"]: (job, tier1, data) for job, tier1, data in fresh}
        for corr in consistency.corrections(members):
            job, tier1, data = by_id[corr["id"]]
            data[corr["field"]] = corr["to"]
            data["near_miss_flags"] = sorted(
                set(data["near_miss_flags"]) | {"sibling_override"}
            )
            label = "leads" if corr["field"] == "leads_discipline" else "management"
            data["scoring_notes"] += (
                f"\n- Sibling consistency: {label} {corr['from']} → {corr['to']} "
                f"({corr['agree']} of {corr['size']} near-identical postings agree)"
            )
            data.setdefault("sibling_override", {})[corr["field"]] = {
                "from": corr["from"], "to": corr["to"]
            }
            overridden.add(corr["id"])
        for jid in sorted(overridden):
            job, tier1, data = by_id[jid]
            _write(conn, job, tier1, data, criteria)
        if overridden:
            conn.commit()

    if acc_calls:
        usage.record_usage(conn, haiku.MODEL, acc, calls=acc_calls)
        conn.commit()

    return {
        "scored": scored,
        "tier1_failed": tier1_failed,
        "errors": errors,
        "rate_limited": rate_limited,
        "escalated": escalated,
        "sibling_overrides": len(overridden),
        "cost": round(usage.cost_of(haiku.MODEL, acc), 6),
    }
