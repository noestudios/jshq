"""Haiku fit scorer. Prompt is generated from DATA_DIR/fit_criteria.md.

The Anthropic client is injected by the caller — this module never creates
one, so tests pass a fake and can never hit the live API. Structured outputs
constrain the response to SCHEMA; the numeric bounds SCHEMA cannot express are
enforced in _parse.

The model does NOT emit a fit score (sub-score redesign 2026-08). It returns a
per-criterion read of the Tier 2 list and the pipeline weights and totals it —
see scoring.aggregate. Asking for one integer produced ten distinct values
across 69 jobs, near-deterministic on craft_lean (lean +4 -> 82 in 7/7 cases),
with seven different companies tying on every emitted field while their notes
differed richly. The judgement was there; the number discarded it.
"""

import json
from collections.abc import Mapping

from .criteria import (
    LEGACY_CRAFT_CRITERION,
    LEGACY_NO_NEGATIVE_CRITERIA,
    DEFAULT_TAXONOMY,
    LEGACY_TIER2_COUNT,
    Criteria,
    CriteriaError,
)

MODEL = "claude-haiku-4-5"
# 1024 was sized for one score plus prose notes. Eleven sub-scores with quoted
# evidence run ~520 typical / ~750 worst case; under structured outputs a
# truncation is a parse failure, which costs a full retry call, so headroom is
# cheaper than the truncation it prevents.
MAX_TOKENS = 1536
TEMPERATURE = 0.0
# Retry-only. A temp-0 failure is deterministic for that request, so the retry
# must vary or it re-buys the same bad output (see score_job's docstring).
RETRY_TEMPERATURE = 0.3
JD_CHAR_LIMIT = 12_000

# Fixed vocabulary: these tokens are STORED (jobs.fit_quadrant,
# score_detail.management_type) and parsed back out, so they are not config —
# renaming one would orphan every existing row. Their display labels are
# configurable via the taxonomy block; these keys are not.
QUADRANTS = ["energizing_strength", "energizing_growth", "draining_growth", "draining_strength"]
MANAGEMENT_TYPES = ["people_leader", "ic", "unclear"]
CONFIDENCES = ["low", "medium", "high"]

# `function` and `leads_discipline` ARE config (Phase 2): they name a specific
# field of work, so a non-design search cannot use the shipped vocabulary. See
# criteria.DEFAULT_DISCIPLINES / DEFAULT_FUNCTIONS for the defaults and the
# regressions their glosses encode.


def _disciplines(criteria: Criteria | None) -> dict:
    return (criteria.taxonomy if criteria else DEFAULT_TAXONOMY)["disciplines"]


def _functions(criteria: Criteria | None) -> dict:
    return (criteria.taxonomy if criteria else DEFAULT_TAXONOMY)["functions"]


def _schema_parts(criteria: Criteria | None) -> dict:
    """Which optional classification keys this doc's schema (and prompt)
    carries. A doc that never declared a functions map, quadrant labels, or an
    in-band discipline must not force the model to answer with the design
    defaults it silently inherited — mirror of the `if craft:` conditionality
    below. criteria=None keeps the full legacy shape (the fixture call sites
    and any pre-Phase-2 doc)."""
    if criteria is None:
        return {"function": True, "fit_quadrant": True, "leads_discipline": True}
    return {
        "function": criteria.functions_declared,
        "fit_quadrant": criteria.quadrants_declared,
        "leads_discipline": not criteria.taxonomy_is_default,
    }

# Per-criterion sub-score bounds. craft_lean is no longer emitted — it is
# derived from criterion 5 in the write path (same reasoning that removed
# central_tension in 2026-07: two fields that must agree eventually disagree).
SUB_MIN, SUB_MAX = -2, 2


def tier2_contract(criteria: Criteria | None) -> tuple[int, int | None, frozenset, bool]:
    """(criterion count, craft criterion, bonus-only positions, craft explicit)
    for the prompt and the parser, so the two can never disagree about the
    rubric's shape.

    All three used to be module constants that had to be kept in step with the
    doc by hand; they are now read off it (Phase 2). `criteria` is optional
    because the parser's own tests build fixtures rather than docs — None means
    the legacy 11-criterion contract those fixtures were written against. Every
    production path passes the real criteria.
    """
    if criteria is None:
        return (
            LEGACY_TIER2_COUNT,
            LEGACY_CRAFT_CRITERION,
            LEGACY_NO_NEGATIVE_CRITERIA,
            False,
        )
    return (
        criteria.tier2_count,
        criteria.craft_criterion,
        criteria.no_negative_criteria,
        criteria.craft_explicit,
    )

# central_tension was removed from the model contract (scoring redesign
# 2026-07): its `mixed` enum option was the measured hedge — 72% of scored
# jobs. The display label is now DERIVED from craft_lean in the write path,
# so lean/tension contradictions are structurally impossible.
#
# tier2 sits BEFORE scoring_notes deliberately: structured outputs generate in
# schema order, so the per-criterion extraction happens first and the summary
# is written against it — rather than the model composing a narrative and then
# back-filling sub-scores to agree with what it already said.
def build_schema(criteria: Criteria | None = None) -> dict:
    """The structured-output schema, built per call so its enums are always the
    doc's own vocabulary. It used to be an import-time constant, which is
    exactly the drift this phase exists to remove: the prompt would name one set
    of disciplines while the grammar constrained the model to another."""
    parts = _schema_parts(criteria)
    properties = {
        "tier2": {
            "type": "array",
            # NOT length-pinned: structured outputs reject minItems values
            # other than 0/1 (verified live 2026-08-10, 400 invalid_request),
            # so the grammar cannot force 11 entries. The parser's count
            # check + the warmed retry in score_job are the enforcement.
            "items": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer"},
                    "v": {"type": ["integer", "null"]},
                    "q": {"type": "string"},
                },
                "required": ["n", "v", "q"],
                "additionalProperties": False,
            },
        },
        "fit_quadrant": {"type": "string", "enum": QUADRANTS},
        "management_type": {"type": "string", "enum": MANAGEMENT_TYPES},
        "function": {"type": "string", "enum": list(_functions(criteria))},
        "leads_discipline": {"type": "string", "enum": list(_disciplines(criteria))},
        "confidence": {"type": "string", "enum": CONFIDENCES},
        "near_miss_flags": {"type": "array", "items": {"type": "string"}},
        "scoring_notes": {"type": "string"},
    }
    for key, keep in parts.items():
        if not keep:
            del properties[key]
    return {
    "type": "object",
    "properties": properties,
    "required": list(properties),
    "additionalProperties": False,
    }


class ScoringError(Exception):
    """The model returned unusable output after a retry. Carries the per-attempt
    `usages` so the caller can still bill the tokens the failed attempts spent —
    mirrors RefineError/ComposeError. Without it, a job that fails parsing on both
    attempts (two billable calls) never reaches the usage ledger, so the cost
    total silently under-reports."""

    def __init__(self, message, usages=None):
        super().__init__(message)
        self.usages = usages or []


def build_system_prompt(criteria: Criteria, digest: str, learned_rules=()) -> str:
    parts = [
        f"You score {criteria.domain_label} job postings for one specific "
        f"person, {criteria.display_name}. Their criteria document follows; "
        "apply it literally — it is the rubric, not background reading.",
        criteria.prose.strip(),
    ]
    if digest:
        parts.append(digest)
    if learned_rules:
        parts.append(
            "Learned role-mismatch rules (the user's accepted exclusions — apply "
            "them as strong negative signals: when a posting clearly matches one, "
            "score it low, add a `role_mismatch` near-miss flag, and name the rule "
            "in scoring_notes):\n" + "\n".join(f"- {t}" for t in learned_rules)
        )
    flag_vocab = sorted(
        set(criteria.adjustments)
        | {"comp_below_target", "comp_unknown", "location_unknown", "below_band", "scope_gap"}
    )
    count, craft, no_negative, _explicit = tier2_contract(criteria)
    if not count:
        # Every reply would fail the count check and burn the warm retry, on
        # every job, forever. Loud here beats a silent money leak.
        raise CriteriaError(
            "the criteria doc has no Tier 2 criteria — nothing to score against. "
            "Check the <!-- tier2:start --> / <!-- tier2:end --> markers."
        )
    # A rubric need not have a craft axis or a bonus-only criterion. When the
    # doc declares neither, these sentences are omitted rather than pointing the
    # model at a criterion number that does not exist.
    rules = []
    if craft:
        rules.append(
            f"Criterion {craft} is never null — every posting has responsibility "
            "verbs to read the craft-versus-convert axis from."
        )
    if no_negative:
        # Every bonus criterion is named, not just the lowest: the parser clamps
        # all of them, so naming one would hide the rest of the doc's config.
        which = ", ".join(str(n) for n in sorted(no_negative))
        plural = "Criteria" if len(no_negative) > 1 else "Criterion"
        verb = "are" if len(no_negative) > 1 else "is"
        rules.append(f"{plural} {which} {verb} never negative (0 when absent).")
    never_line = f"  * {' '.join(rules)}\n" if rules else ""
    quote_line = (
        '  * q: quote the posting\'s exact words whenever v is +2 or -2, and '
        f"for criterion {craft} always — those judgements move the "
        'total most. Otherwise "" is fine.\n'
        if craft
        else "  * q: quote the posting's exact words whenever v is +2 or -2 — "
        'those judgements move the total most. Otherwise "" is fine.\n'
    )
    # The schema and the prompt must describe the same shape: every line here
    # is gated on the same _schema_parts flags that add or drop the key, so
    # the model is never instructed about a field the grammar won't accept
    # (or forced to emit one the doc never defined — the design defaults a
    # wizard-written taxonomy silently inherits).
    parts_on = _schema_parts(criteria)
    # function / leads_discipline vocabularies come from the doc's taxonomy
    # block, glosses and all, so a search in another field is a doc edit rather
    # than a code change. A token with no gloss is listed bare.
    function_line = ""
    if parts_on["function"]:
        functions = _functions(criteria)
        function_line = "- function: " + ", ".join(
            f"{token} ({gloss})" if gloss else token for token, gloss in functions.items()
        ) + "\n"
    discipline_line = ""
    if parts_on["leads_discipline"]:
        disciplines = _disciplines(criteria)
        discipline_line = (
            "- leads_discipline: the discipline the role LEADS, per the function "
            "check — judge from what the direct reports are, the discipline named "
            "in the years-of-experience requirement, and whether the role owns that "
            "discipline's org or merely partners with it; quote that evidence in "
            "the notes. The tokens:\n"
            # One per line: the glosses contain sentence punctuation of their own, so
            # joining them inline runs them together and buries the distinctions the
            # wording exists to draw.
            + "".join(
                f"  * '{token}' — {gloss}\n" if gloss else f"  * '{token}'\n"
                for token, gloss in disciplines.items()
            )
        )
        # The in-band framing rides only when the doc declared its field —
        # function_check_flag skips the cap on the same condition.
        in_band = (criteria.taxonomy if criteria else DEFAULT_TAXONOMY)["in_band_disciplines"]
        discipline_line += (
            "  * This search is for "
            + " / ".join(f"'{d}'" for d in in_band)
            + ". Any other discipline is the wrong function for it.\n"
        )
    quadrant_line = (
        "- fit_quadrant: which fulfillment-matrix quadrant the job's "
        "responsibility verbs predominantly land in, judged against each "
        "quadrant's signal-verb list\n"
        if parts_on["fit_quadrant"]
        else ""
    )
    # The scoring_notes bullets mirror what the schema asks for: the
    # leads_discipline evidence bullet (and its example line) exists only when
    # the read itself does.
    if parts_on["leads_discipline"]:
        notes_scope = (
            "covering only what the sub-scores cannot say: the management_type "
            "evidence, the leads_discipline evidence, and at most one judgement "
            "call worth flagging. Quote the posting's exact words in double "
            "quotes for the first two."
        )
        notes_example = (
            "Established product company; the role owns the team's growth.\n"
            '- "hire, develop, and retain a team" — people_leader\n'
            '- "8+ years leading practitioners in the field" — leads the discipline\n'
            "- Team size never stated; treated as unevidenced, not as a small team"
        )
    else:
        notes_scope = (
            "covering only what the sub-scores cannot say: the management_type "
            "evidence and at most one judgement call worth flagging. Quote the "
            "posting's exact words in double quotes for the first."
        )
        notes_example = (
            "Established product company; the role owns the team's growth.\n"
            '- "hire, develop, and retain a team" — people_leader\n'
            "- Team size never stated; treated as unevidenced, not as a small team"
        )
    confidence_line = (
        f"- confidence: your confidence in the criterion-{craft} and "
        "management_type reads — low when the JD is generic or the evidence is "
        "thin\n"
        if craft
        else "- confidence: your confidence in the management_type read — low "
        "when the JD is generic or the evidence is thin\n"
    )
    parts.append(
        "For the job posting in the user message, return JSON with:\n"
        "- tier2: your read of EVERY ONE of the "
        f"{count} numbered Tier 2 criteria, as an array of "
        f"{count} objects, one per criterion, in order. Each is "
        '{"n": the criterion number, "v": its score, "q": quoted evidence}.\n'
        f"  * v is an integer {SUB_MIN}..{SUB_MAX}, or null when the posting "
        "does not address that criterion in either direction. +2 = a quotable "
        "phrase directly satisfies it; +1 = partial, adjacent, or inferred; "
        "0 = evidence on BOTH sides (a positive claim of balance, not a hedge); "
        "-1 = mild counter-evidence; -2 = a quotable phrase contradicts it.\n"
        "  * null is expected and correct whenever the posting is silent — a "
        f"typical posting is null on 3-5 of the {count}. Do NOT convert "
        "silence into a negative score: the pipeline prices absence itself, so "
        "scoring -1 for 'never mentioned' applies the penalty twice. Equally, "
        "do not resolve everything to +/-1 to look decisive.\n"
        f"{never_line}"
        f"{quote_line}"
        "  * You do NOT produce an overall fit score. The pipeline weights and "
        "totals these sub-scores, then applies caps and deductions. Judge the "
        f"{count} criteria honestly and independently; do not work "
        "backwards from a number you have in mind, and do not lower a "
        "sub-score for a concern you are already naming in near_miss_flags.\n"
        f"{quadrant_line}"
        "- management_type: people_leader ONLY on evidence of owning people "
        "outcomes — direct reports, performance management, being the hiring "
        "manager (quote it in the notes). Participating in interviews "
        "('help with hiring') and mentoring or guiding peers are NOT people "
        "leadership — those are ic signals. A posting "
        "designated individual contributor (in the title or JD) is "
        "categorically ic no matter what other language appears. ic when the "
        "posting lists no people-outcome responsibilities; unclear when the "
        "evidence is thin. Never infer people_leader from the title alone.\n"
        f"{function_line}"
        f"{discipline_line}"
        f"{confidence_line}"
        "- near_miss_flags: when the job is otherwise strong but fails a soft "
        f"criterion, name it. Canonical tokens: {', '.join(flag_vocab)}. Use "
        "these exact snake_case tokens when applicable; invent a new token "
        "only when nothing fits; else []\n"
        "- scoring_notes: a SCANNABLE explanation in markdown — never one prose "
        "paragraph. The tier2 array already carries the per-criterion evidence, "
        "so do NOT restate it here. Format exactly as one short summary line "
        "(under 20 words), then AT MOST 3 bullets, each on its own line "
        "starting with '- ' and under 20 words, "
        f"{notes_scope} No headings. Example:\n"
        f"{notes_example}"
    )
    return "\n\n---\n\n".join(parts)


def _salary_line(job: Mapping) -> str:
    if job["salary_stated"] and job["salary_max"] is not None:
        lo = job["salary_min"] if job["salary_min"] is not None else job["salary_max"]
        return f"${lo:,}–${job['salary_max']:,} (stated)"
    return "not stated"


def build_user_message(job: Mapping) -> str:
    jd = job["description_text"] or "(no description available)"
    if len(jd) > JD_CHAR_LIMIT:
        jd = jd[:JD_CHAR_LIMIT] + "\n[truncated]"
    # No "Level band" line (scoring redesign 2026-07): the title already carries
    # level, and a second, more salient statement of it anchored the temp-0
    # model to band-shaped scores (the flat-72 pathology). Tier 1 still
    # consumes level_band deterministically.
    return (
        f"Title: {job['title']}\n"
        f"Company: {job['company_name']}\n"
        f"Location: {job['location'] or 'unknown'} ({job['remote_type']})\n"
        f"Salary: {_salary_line(job)}\n\n"
        f"Job description:\n{jd}"
    )


def _parse_tier2(raw, criteria: Criteria | None = None) -> tuple[dict[int, int | None], dict[int, str]]:
    """Validate the per-criterion array into ({criterion -> score|None}, {criterion -> quote}).

    Raises ValueError on anything unusable so score_job's retry fires — a short
    or duplicated array would otherwise silently score a job against fewer
    criteria than the rubric defines, which reads as a legitimate low score.

    Quotes are kept (empty ones dropped) because a stored sub-score is otherwise
    unexplainable: "-2 on criterion 2" means nothing without the phrase it came
    from, and the whole point of scoring per criterion is that the total is
    auditable.
    """
    count, craft, no_negative, craft_explicit = tier2_contract(criteria)
    if not isinstance(raw, list):
        raise ValueError("tier2 is not a list")
    if len(raw) != count:
        raise ValueError(f"tier2 has {len(raw)} entries, expected {count}")
    out: dict[int, int | None] = {}
    quotes: dict[int, str] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError(f"tier2 entry is not an object: {entry!r}")
        try:
            n = int(entry["n"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"tier2 entry has no usable 'n': {entry!r}") from exc
        if not 1 <= n <= count:
            raise ValueError(f"tier2 criterion {n} out of range 1..{count}")
        if n in out:
            raise ValueError(f"tier2 criterion {n} appears twice")
        quote = entry.get("q")
        if isinstance(quote, str) and quote.strip():
            quotes[n] = quote.strip()
        value = entry.get("v")
        if value is None:
            # Only an EXPLICITLY marked axis is worth failing a score over. When
            # the position was inferred from a legacy-shaped doc, the author
            # never designated it, and rejecting their honest null would leave
            # the job permanently unscored after the retry.
            if craft_explicit and n == craft:
                raise ValueError(
                    f"criterion {craft} may not be null — "
                    "the craft/convert axis is always readable"
                )
            out[n] = None
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"tier2 criterion {n} value is not an integer: {value!r}")
        value = max(SUB_MIN, min(SUB_MAX, value))
        # Doc rule, enforced rather than trusted: bonus-only criteria never
        # score negative, so a stray -1 reads as 0 instead of penalizing.
        if n in no_negative:
            value = max(0, value)
        out[n] = value
    return out, quotes


def _parse(resp, criteria: Criteria | None = None) -> dict:
    text = next(b.text for b in resp.content if b.type == "text")
    data = json.loads(text)
    data["tier2"], data["tier2_quotes"] = _parse_tier2(data["tier2"], criteria)
    # Belt-and-suspenders enum checks (structured outputs should enforce these,
    # but a fake client / SDK regression must fail the retry loop, not write).
    # Keys the doc's schema omits are normalized to None so every downstream
    # consumer (derive, _write, score_detail) keeps one payload shape.
    parts = _schema_parts(criteria)
    if parts["fit_quadrant"]:
        if data["fit_quadrant"] not in QUADRANTS:
            raise ValueError(f"out-of-enum fit_quadrant: {data['fit_quadrant']}")
    else:
        data["fit_quadrant"] = None
    if data["management_type"] not in MANAGEMENT_TYPES:
        raise ValueError(f"out-of-enum management_type: {data['management_type']}")
    if parts["function"]:
        if data["function"] not in _functions(criteria):
            raise ValueError(f"out-of-enum function: {data['function']}")
    else:
        data["function"] = None
    if parts["leads_discipline"]:
        if data["leads_discipline"] not in _disciplines(criteria):
            raise ValueError(f"out-of-enum leads_discipline: {data['leads_discipline']}")
    else:
        data["leads_discipline"] = None
    if data["confidence"] not in CONFIDENCES:
        raise ValueError(f"out-of-enum confidence: {data['confidence']}")
    if not isinstance(data["near_miss_flags"], list):
        raise ValueError("near_miss_flags is not a list")
    return data


async def score_job(client, system: str, job: Mapping, criteria: Criteria | None = None):
    """One Haiku call (plus one retry on unusable output). Returns
    (data, usage) — usage is the response's usage object (or None if the fake
    client / SDK omits it), for cost accounting by the caller. Raises ScoringError.

    `criteria` supplies the rubric shape the response is validated against (how
    many criteria, which one is the craft axis, which are bonus-only). It is the
    same object `system` was built from; passing it keeps the prompt and the
    parser from disagreeing. Omitting it falls back to the legacy 11-criterion
    contract — see tier2_contract.

    The retry runs WARM (RETRY_TEMPERATURE), not at temp 0: an unusable output
    at temp 0 is deterministic for that exact request, so a verbatim retry fails
    identically. A live case (2026-08-10) proved it — a mode that emits a valid JSON
    with 2 tier2 entries and narrates the other nine in scoring_notes, four
    identical failures across two invocations (and the schema cannot pin the
    array length: structured outputs reject minItems > 1). First attempts stay
    at temp 0 for run-to-run stability; only the already-failed path varies."""
    last_exc: Exception | None = None
    usages: list = []
    for attempt in range(2):
        resp = await client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            temperature=TEMPERATURE if attempt == 0 else RETRY_TEMPERATURE,
            # cache_control: free win if the criteria prose ever clears Haiku's
            # 4096-token cache minimum; harmless below it.
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": build_user_message(job)}],
            output_config={
                "format": {"type": "json_schema", "schema": build_schema(criteria)}
            },
        )
        used = getattr(resp, "usage", None)
        if used is not None:  # collect before parsing so a failed attempt still bills
            usages.append(used)
        try:
            return _parse(resp, criteria), used
        except (json.JSONDecodeError, KeyError, ValueError, TypeError, StopIteration) as exc:
            last_exc = exc
    raise ScoringError(f"unusable model output after retry: {last_exc}", usages) from last_exc
