"""Load fit criteria from DATA_DIR/fit_criteria.md.

The doc is the source of truth (CLAUDE.md convention): the Tier 1 parameters
live in a fenced ```json tier1_params``` block, and everything else is prose
sent verbatim to the Haiku scorer. Criteria edits must never require code
changes — and a broken doc must fail loudly at scoring time, never silently
fall back to defaults.
"""

import copy
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from jshq import paths

# The user's live copy (seeded from defaults/fit_criteria.md on first run).
# The app writes this file — it must live in DATA_DIR, not the package.
CRITERIA_PATH = paths.DATA_DIR / "fit_criteria.md"

_PARAMS_BLOCK = re.compile(r"```json tier1_params\n(.*?)```", re.DOTALL)

# Optional persona block (Phase 2): who the AI prompts are written for. The
# display name reaches api.anthropic.com on every scoring/compose/tailor call,
# so it belongs in the user's own doc rather than in code. Absent (or an
# explicit null name) ⇒ neutral third-person phrasing, which is what an
# un-personalized install should send.
_PERSONA_BLOCK = re.compile(r"```json persona\n(.*?)```", re.DOTALL)

# Used wherever a name would go when the doc names nobody. Third person, so the
# surrounding prompt text reads correctly either way.
NEUTRAL_DISPLAY_NAME = "the candidate"

# domain_label defaults to the starter doc's neutral phrase, so a doc without
# the block prompts the same as a fresh install rather than presuming a field.
DEFAULT_PERSONA = {"display_name": None, "domain_label": "the roles you are searching for"}

# Sanity rail, not a naming policy: these strings are pasted into every prompt,
# so a pasted paragraph would quietly cost tokens on every call.
PERSONA_MAX_LEN = 120

# Optional taxonomy block (Phase 2): the vocabulary the scorer classifies a
# posting with. Every value here used to be a code constant in haiku.py, which
# meant a non-design job search could not be expressed without editing source.
_TAXONOMY_BLOCK = re.compile(r"```json taxonomy\n(.*?)```", re.DOTALL)

# leads_discipline: the discipline a role LEADS. Each token carries a gloss that
# is rendered into the prompt, so the design-specific guidance that used to be
# ~15 lines of hardcoded prose is stated once, here, as config. The two named
# cases are regressions the wording exists to prevent: a Director of Product
# managing PMs reading as design leadership, and a content-design leadership
# role having no honest token and landing on "design" (both scored 79-83).
DEFAULT_DISCIPLINES = {
    "design": (
        "roles leading product/UX designers or researchers. For an "
        "individual-contributor posting (no reports), read the discipline the "
        "role itself sits in: a product/UX designer IC is 'design'"
    ),
    "product": (
        "product MANAGEMENT, a role leading PMs. NOT product design"
    ),
    "engineering": "roles leading engineers",
    "content": (
        "a role leading CONTENT designers / UX writers. Content designers are "
        "designers, so this case must be called by name: 'Director/Head of "
        "Content Design' is 'content' however much design craft the JD describes"
    ),
    "other": "program/delivery/ops leadership, or anything else",
    "unclear": (
        "the evidence is genuinely thin. This flags the job for manual review, "
        "it does not pass"
    ),
}

# The discipline(s) the search is FOR. Anything else derives wrong_function and
# is capped in code — see scoring.function_check_flag.
DEFAULT_IN_BAND_DISCIPLINES = ("design",)

# function: the sub-discipline the role sits in, within the searched field.
DEFAULT_FUNCTIONS = {
    "product": "product/UX design",
    "content": "content design/UX writing",
    "research": "",
    "service": "service design",
    "platform": "design systems/internal tooling as the role's center",
    "other": "",
}

# Display labels only. The KEYS are stored in jobs.fit_quadrant and parsed back
# out of the scoring notes, so they are fixed vocabulary and validated as such;
# only the human-facing strings are editable.
DEFAULT_QUADRANT_LABELS = {
    "energizing_strength": "energizing · strength",
    "energizing_growth": "energizing · growth",
    "draining_growth": "draining · growth",
    "draining_strength": "draining · strength",
}
DEFAULT_TENSION_LABELS = {
    "teach_craft": "teach craft",
    "convert_sell": "convert / sell",
    "mixed": "mixed",
}

DEFAULT_TAXONOMY = {
    "disciplines": DEFAULT_DISCIPLINES,
    "in_band_disciplines": list(DEFAULT_IN_BAND_DISCIPLINES),
    "functions": DEFAULT_FUNCTIONS,
    "quadrant_labels": DEFAULT_QUADRANT_LABELS,
    "tension_labels": DEFAULT_TENSION_LABELS,
}

# Optional level-bands block (Phase 2): the seniority patterns that produce
# jobs.level_band. The band NAMES were always config (tier1_params
# .target_title_bands / flag_title_bands) but the patterns were not, so a doc
# could name a band derive_level_band could never emit — a filter that silently
# matched nothing. Phrases, not regexes: see normalize.compile_level_bands.
_BANDS_BLOCK = re.compile(r"```json level_bands\n(.*?)```", re.DOTALL)


def _parse_level_bands(text: str) -> tuple[dict, tuple[int, int] | None]:
    """Parse the optional ```json level_bands``` block. Absent ⇒ the shipped
    defaults, which are the pre-Phase-2 patterns restated in this shape."""
    from jshq.ats.normalize import DEFAULT_LEVEL_BANDS

    match = _BANDS_BLOCK.search(text)
    if match is None:
        return copy.deepcopy(DEFAULT_LEVEL_BANDS), None
    try:
        cfg = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise CriteriaError(f"level_bands block is not valid JSON: {exc}") from exc
    if not isinstance(cfg, dict) or not isinstance(cfg.get("bands"), list) or not cfg["bands"]:
        raise CriteriaError("level_bands must be an object with a non-empty 'bands' list")
    for entry in cfg["bands"]:
        if not isinstance(entry, dict):
            raise CriteriaError("each level_bands entry must be an object")
        band = entry.get("band")
        if not isinstance(band, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", band):
            raise CriteriaError(f"level_bands band {band!r} must be snake_case")
        phrases = entry.get("phrases")
        if not isinstance(phrases, list) or not phrases:
            raise CriteriaError(f"level_bands[{band!r}] needs a non-empty 'phrases' list")
        for phrase in phrases:
            if not isinstance(phrase, str) or not phrase.strip():
                raise CriteriaError(f"level_bands[{band!r}] phrases must be non-empty strings")
        label = entry.get("label")
        if label is not None and (not isinstance(label, str) or not label.strip()):
            raise CriteriaError(f"level_bands[{band!r}] label must be a non-empty string")
    fallback = cfg.get("fallback", "ic")
    if not isinstance(fallback, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", fallback):
        raise CriteriaError("level_bands['fallback'] must be a snake_case band name")
    cfg["fallback"] = fallback
    return cfg, match.span()


# Optional score-adjustments table (scoring redesign 2026-07): named near-miss
# flags -> point deductions applied IN CODE after the model scores. Machine
# config like tier1_params — stripped from the prose so the model never sees
# the point values (it would pre-apply them; it learns the flag VOCABULARY via
# the output spec instead).
_ADJUST_BLOCK = re.compile(r"```json score_adjustments\n(.*?)```", re.DOTALL)

# Optional score-caps table (IC hard cap, 2026-07): management_type -> absolute
# score ceiling applied IN CODE before deductions. Stripped from the prose for
# the same reason as the adjustments — the model must never see the cap value.
_CAPS_BLOCK = re.compile(r"```json score_caps\n(.*?)```", re.DOTALL)

# management_type values a cap may key on. people_leader is deliberately
# absent — capping the target management type is always a config mistake.
CAPPABLE_TYPES = {"ic", "unclear"}

# Function-check cap keys (2026-07): keyed on the model's leads_discipline
# read rather than management_type — wrong_function for roles leading a
# non-design discipline, function_unclear for an unclear read (flagged for
# manual review, never a pass).
FUNCTION_CAP_KEYS = {"wrong_function", "function_unclear"}

# Level-band cap keys (2026-08): keyed on the job's deterministic level_band
# (derive_level_band), applied in code like the management-type cap. Only
# junior is cappable — intern/junior/associate titles have no honest path
# into a target band, so a hard ceiling beats relying on a learned rule.
# Legacy default, used only when caps are parsed without a compiled band set
# (older callers). load_criteria derives the real set from level_bands.
BAND_CAP_KEYS = {"junior"}

# Optional score-scale block (sub-score redesign 2026-08): the affine map from
# the weighted Tier 2 sub-score total onto 0..100, plus the per-criterion value
# substituted when the model reports a criterion unevidenced. Stripped from the
# prose for the same reason as the caps and adjustments — shown the silence
# values, the model would pre-apply them instead of honestly reporting null.
_SCALE_BLOCK = re.compile(r"```json score_scale\n(.*?)```", re.DOTALL)

# Defaults when the block is absent: identity-ish map with no silence penalty.
# Chosen so an older doc still produces a usable 0..100 score rather than
# raising — the block is config, not a required contract.
DEFAULT_SCALE = {"slope": 1.6, "intercept": 55.0, "silence": {}}

# A silence value is a half-step on the -2..+2 sub-score axis, so the same
# bounds apply. Wider would let one absent criterion outweigh a scored one.
SILENCE_MIN, SILENCE_MAX = -2.0, 2.0

# Tier 2 ranked criteria live as a numbered list fenced by HTML-comment markers
# so the Settings editor (Phase 7h) can rewrite exactly that region; the list
# itself stays in the prose sent to the scorer. The markers are stripped from
# prose so they never reach the model.
_TIER2_BLOCK = re.compile(r"(<!-- tier2:start -->\n)(.*?)(\n<!-- tier2:end -->)", re.DOTALL)

# Synthesized reflection prose (fulfillment-matrix quadrants, central tension,
# away/toward) lives between its own comment fences so a re-synthesis replaces
# exactly this region and hand-written prose outside it is never touched. Like
# the tier2 markers: the fences are stripped from prose, the body ships to the
# scorer.
_SYNTHESIS_BLOCK = re.compile(
    r"(<!-- synthesis:start -->\n)(.*?)(\n<!-- synthesis:end -->)", re.DOTALL
)
_RUBRIC_HEADING = re.compile(r"^## Scoring rubric[ \t]*$", re.M)

# HTML comments are author guidance, never model input — the tier2 region
# markers, and the how-to notes that sit beside the machine blocks explaining
# what to edit. Stripped from the prose wholesale so a note addressed to the
# USER can never be read as an instruction by the scorer. (A note next to the
# persona block saying a null name "keeps the prompts anonymous" would
# otherwise tell the model the name it was just handed is a placeholder.)
_COMMENTS = re.compile(r"[ \t]*<!--.*?-->[ \t]*\n?", re.DOTALL)

_REQUIRED_KEYS = {
    "comp_floor": int,
    "comp_target": int,
    "location_allowlist": list,
    "company_location_overrides": dict,
    "remote_regions": list,
    "excluded_sectors": list,
    "target_title_bands": list,
    "flag_title_bands": dict,
}


class CriteriaError(Exception):
    """fit_criteria.md is missing, malformed, or incomplete.

    ``field``/``kind`` are optional structured context for the Settings
    editor: which tier1_params key failed and how ("missing", "int", "list",
    "dict", "radius"), so the frontend can compose an inline field error
    without parsing this exception's prose (error-audit P1). The long tail
    of doc-shape errors (taxonomy, bands, scale) deliberately leaves them
    None — that audience is someone hand-editing the doc, and the precise
    technical message IS the friendly one."""

    def __init__(self, message: str, *, field: str | None = None, kind: str | None = None):
        super().__init__(message)
        self.field = field
        self.kind = kind


@dataclass(frozen=True)
class Criteria:
    params: dict
    prose: str  # full doc minus the machine blocks — goes into the prompt
    adjustments: dict = field(default_factory=dict)  # flag -> point deduction (0..25)
    caps: dict = field(default_factory=dict)  # management_type -> score ceiling (0..100)
    # {slope, intercept, silence{criterion index (1-based, as int) -> value}}
    scale: dict = field(default_factory=lambda: dict(DEFAULT_SCALE))
    # Ordered Tier 2 criteria as {text, weight} — the aggregation weights. Parsed
    # here rather than re-derived at each call site so the scorer, the write path
    # and the calibration script all read one list.
    tier2: list = field(default_factory=list)
    # {display_name (str|None), domain_label} — who the prompts are written for.
    persona: dict = field(default_factory=lambda: dict(DEFAULT_PERSONA))
    # 1-based position of the `[craft]` criterion, or None when the doc declares
    # no craft/convert axis. craft_lean and the tension label derive from it.
    craft_criterion: int | None = None
    # 1-based positions of `[bonus]` criteria: never scored negative.
    no_negative_criteria: frozenset = frozenset()
    # {disciplines, in_band_disciplines, functions, quadrant_labels,
    # tension_labels} — the classification vocabulary sent to the model.
    taxonomy: dict = field(default_factory=lambda: copy.deepcopy(DEFAULT_TAXONOMY))
    # ordered [(band, compiled pattern)] + the band an unmatched title takes
    level_bands: tuple = ()
    level_band_fallback: str = "ic"
    # band -> display label, for the UI's Level filter
    level_band_labels: dict = field(default_factory=dict)
    # band names a score_caps key may use: every emittable band minus the
    # ones the management-type and function-check caps already own
    cappable_bands: frozenset = frozenset({"junior"})
    # True when a `[craft]` marker named the axis, False when it was inferred
    # from a marker-less legacy-shaped doc. An inferred axis is used for the
    # lean but never enforced as never-null: the author never designated it.
    craft_explicit: bool = False
    # True when the doc has NO taxonomy block, so the classification vocabulary is
    # the design-specific code DEFAULT rather than the user's field. A blank slate
    # has not declared what discipline it searches, so the function check is
    # neutralized (no wrong_function / function_unclear cap, no in-band framing in
    # the prompt) until a taxonomy is set. See function_check_flag + build_system_prompt.
    taxonomy_is_default: bool = False
    # Which optional classification vocabularies the doc's taxonomy block
    # actually DECLARES. A wizard-written block carries disciplines only, so a
    # non-design doc must not inherit the design functions map or a quadrant
    # read it never defined: build_schema omits `function` / `fit_quadrant`
    # (and their prompt lines) when the doc doesn't declare them. Mirrors the
    # craft-axis conditionality in build_system_prompt.
    functions_declared: bool = False
    quadrants_declared: bool = False

    @property
    def tier2_count(self) -> int:
        """How many criteria the model is asked to score. Derived from the doc,
        never asserted against a constant: criteria past the count the model was
        given would sit permanently unevidenced, silently taking their silence
        value on every job."""
        return len(self.tier2)

    @property
    def display_name(self) -> str:
        """The name to use in prompt text; NEUTRAL_DISPLAY_NAME when the doc
        names nobody. Prompts must go through this rather than reading
        persona["display_name"] directly, which may be None."""
        return self.persona.get("display_name") or NEUTRAL_DISPLAY_NAME

    @property
    def domain_label(self) -> str:
        return self.persona.get("domain_label") or DEFAULT_PERSONA["domain_label"]


# (mtime, Criteria) — re-read when the doc changes, no restart needed.
_cache: tuple[float, Criteria] | None = None


def _finite_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _validate_radius(params: dict) -> None:
    """Optional Phase 7i location-radius block. Absent or null ⇒ radius off (the
    pre-7i behavior); not in _REQUIRED_KEYS so older criteria files load
    unchanged. When present it must carry a finite center lat/lng and a positive
    radius_minutes (the commute threshold), plus an optional estimate block with
    positive detour_factor / avg_mph — so a malformed edit raises CriteriaError
    before the doc is written, the same fail-loud contract as the required keys."""
    cfg = params.get("location_radius")
    if cfg is None:
        return
    if not isinstance(cfg, dict):
        raise CriteriaError("tier1_params['location_radius'] must be an object or null", field="location_radius", kind="radius")
    center = cfg.get("center")
    if not isinstance(center, dict) or not _finite_number(center.get("lat")) or not _finite_number(
        center.get("lng")
    ):
        raise CriteriaError("location_radius.center must have finite numeric 'lat' and 'lng'", field="location_radius", kind="radius")
    minutes = cfg.get("radius_minutes")
    if not _finite_number(minutes) or minutes <= 0:
        raise CriteriaError("location_radius.radius_minutes must be a positive number", field="location_radius", kind="radius")
    est = cfg.get("estimate")
    if est is not None:
        if not isinstance(est, dict):
            raise CriteriaError("location_radius.estimate must be an object", field="location_radius", kind="radius")
        for key in ("detour_factor", "avg_mph"):
            if key in est and (not _finite_number(est[key]) or est[key] <= 0):
                raise CriteriaError(f"location_radius.estimate.{key} must be a positive number", field="location_radius", kind="radius")


def _parse_persona(text: str) -> tuple[dict, tuple[int, int] | None]:
    """Parse the optional ```json persona``` block. Absent ⇒ (DEFAULT_PERSONA, None).

    `display_name` may be an explicit null to mean "name nobody"; `domain_label`
    must be a non-empty string when present. Unknown keys fail loud like
    score_scale's — a typo such as `"name"` would otherwise silently leave the
    prompts anonymous.
    """
    match = _PERSONA_BLOCK.search(text)
    if match is None:
        return dict(DEFAULT_PERSONA), None
    try:
        table = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise CriteriaError(f"persona block is not valid JSON: {exc}") from exc
    if not isinstance(table, dict):
        raise CriteriaError("persona must be a JSON object")
    unknown = set(table) - set(DEFAULT_PERSONA)
    if unknown:
        raise CriteriaError(f"persona has unknown keys: {sorted(unknown)}")
    persona = dict(DEFAULT_PERSONA)
    for key in DEFAULT_PERSONA:
        if key not in table:
            continue
        value = table[key]
        if value is None and key == "display_name":
            continue  # explicit null ⇒ neutral phrasing
        if not isinstance(value, str) or not value.strip():
            raise CriteriaError(f"persona[{key!r}] must be a non-empty string, got {value!r}")
        value = value.strip()
        # Both values are interpolated into prompt prose, and display_name also
        # lands inside the JSON shape examples the tailoring prompts show the
        # model. A newline would break the prompt's block structure; a double
        # quote would hand the model a malformed example of the exact format it
        # is being asked to reproduce.
        if any(ch < " " for ch in value):
            raise CriteriaError(f"persona[{key!r}] must be a single line")
        if len(value) > PERSONA_MAX_LEN:
            raise CriteriaError(
                f"persona[{key!r}] must be at most {PERSONA_MAX_LEN} characters"
            )
        if key == "display_name" and '"' in value:
            raise CriteriaError("persona['display_name'] must not contain a double quote")
        persona[key] = value
    return persona, match.span()


def _token_map(raw, key: str) -> dict:
    """A {snake_case token -> gloss} map from the taxonomy block. Order is
    preserved (it becomes the schema's enum order), glosses may be empty."""
    if not isinstance(raw, dict) or not raw:
        raise CriteriaError(f"taxonomy[{key!r}] must be a non-empty JSON object")
    out = {}
    for token, gloss in raw.items():
        if not isinstance(token, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", token):
            raise CriteriaError(
                f"taxonomy[{key!r}] key {token!r} must be snake_case "
                "(it is sent to the model as an enum value and stored in the DB)"
            )
        if not isinstance(gloss, str):
            raise CriteriaError(f"taxonomy[{key!r}][{token!r}] must be a string")
        out[token] = gloss.strip()
    return out


def _fixed_labels(raw, key: str, expected: dict) -> dict:
    """Display labels whose KEYS are fixed vocabulary — they are stored in the DB
    and parsed back out, so renaming one would orphan every existing row. Only
    the human-facing strings are editable."""
    if not isinstance(raw, dict):
        raise CriteriaError(f"taxonomy[{key!r}] must be a JSON object")
    if set(raw) != set(expected):
        raise CriteriaError(
            f"taxonomy[{key!r}] must have exactly these keys: {sorted(expected)} "
            f"(got {sorted(raw)}). They are stored values, not free text."
        )
    for token, label in raw.items():
        if not isinstance(label, str) or not label.strip():
            raise CriteriaError(f"taxonomy[{key!r}][{token!r}] must be a non-empty string")
    return {k: v.strip() for k, v in raw.items()}


def _parse_taxonomy(text: str) -> tuple[dict, tuple[int, int] | None, frozenset]:
    """Parse the optional ```json taxonomy``` block. Absent ⇒ (DEFAULT_TAXONOMY,
    None, no declared keys), which reproduces the pre-Phase-2 hardcoded
    vocabulary exactly. The third element is the set of keys the block itself
    declares — the merged dict always carries every key, so it cannot answer
    "did this doc define functions, or inherit the design default?"."""
    match = _TAXONOMY_BLOCK.search(text)
    if match is None:
        return copy.deepcopy(DEFAULT_TAXONOMY), None, frozenset()
    try:
        table = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise CriteriaError(f"taxonomy block is not valid JSON: {exc}") from exc
    if not isinstance(table, dict):
        raise CriteriaError("taxonomy must be a JSON object")
    unknown = set(table) - set(DEFAULT_TAXONOMY)
    if unknown:
        raise CriteriaError(f"taxonomy has unknown keys: {sorted(unknown)}")

    tax = copy.deepcopy(DEFAULT_TAXONOMY)
    if "disciplines" in table:
        tax["disciplines"] = _token_map(table["disciplines"], "disciplines")
    if "functions" in table:
        tax["functions"] = _token_map(table["functions"], "functions")
    if "quadrant_labels" in table:
        tax["quadrant_labels"] = _fixed_labels(
            table["quadrant_labels"], "quadrant_labels", DEFAULT_QUADRANT_LABELS
        )
    if "tension_labels" in table:
        tax["tension_labels"] = _fixed_labels(
            table["tension_labels"], "tension_labels", DEFAULT_TENSION_LABELS
        )
    if "in_band_disciplines" in table:
        band = table["in_band_disciplines"]
        if not isinstance(band, list) or not band:
            raise CriteriaError("taxonomy['in_band_disciplines'] must be a non-empty list")
        tax["in_band_disciplines"] = [str(d) for d in band]

    # "unclear" is not optional: function_check_flag derives function_unclear
    # from it, and an unclear read must always have somewhere to land.
    if "unclear" not in tax["disciplines"]:
        raise CriteriaError(
            "taxonomy['disciplines'] must include 'unclear' — a thin read has to "
            "have an honest answer, and it is what function_unclear keys on"
        )
    unknown_band = [d for d in tax["in_band_disciplines"] if d not in tax["disciplines"]]
    if unknown_band:
        raise CriteriaError(
            f"taxonomy['in_band_disciplines'] names {unknown_band}, which are not "
            f"in disciplines ({sorted(tax['disciplines'])})"
        )
    if "unclear" in tax["in_band_disciplines"]:
        raise CriteriaError(
            "taxonomy['in_band_disciplines'] may not contain 'unclear' — an "
            "unclear read is flagged for manual review, it never passes"
        )
    return tax, match.span(), frozenset(table)


def _parse_adjustments(text: str) -> tuple[dict, tuple[int, int] | None]:
    """Parse the optional ```json score_adjustments``` block. Absent ⇒ ({}, None)
    (older docs load unchanged, like location_radius). When present it must be a
    JSON object of non-empty-string flag names to ints in 0..25 — fail-loud, so a
    typo like `"scope_gap": 80` can never silently gut scores."""
    match = _ADJUST_BLOCK.search(text)
    if match is None:
        return {}, None
    try:
        table = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise CriteriaError(f"score_adjustments block is not valid JSON: {exc}") from exc
    if not isinstance(table, dict):
        raise CriteriaError("score_adjustments must be a JSON object of flag -> points")
    for flag, points in table.items():
        if not isinstance(flag, str) or not flag.strip():
            raise CriteriaError("score_adjustments keys must be non-empty flag names")
        if not isinstance(points, int) or isinstance(points, bool) or not 0 <= points <= 25:
            raise CriteriaError(
                f"score_adjustments[{flag!r}] must be an integer 0..25, got {points!r}"
            )
    return table, match.span()


def _parse_caps(text: str, cappable_bands=None) -> tuple[dict, tuple[int, int] | None]:
    """Parse the optional ```json score_caps``` block. Absent ⇒ ({}, None).
    When present it must be a JSON object of management_type (CAPPABLE_TYPES),
    function-check (FUNCTION_CAP_KEYS), or level-band (any band the doc's
    level_bands can emit) keys to
    ints in 0..100 — fail-loud, so a typo like "IC" can never silently
    disable the cap."""
    match = _CAPS_BLOCK.search(text)
    if match is None:
        return {}, None
    try:
        table = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise CriteriaError(f"score_caps block is not valid JSON: {exc}") from exc
    if not isinstance(table, dict):
        raise CriteriaError("score_caps must be a JSON object of cap key -> ceiling")
    allowed = (
        CAPPABLE_TYPES
        | FUNCTION_CAP_KEYS
        | (BAND_CAP_KEYS if cappable_bands is None else set(cappable_bands))
    )
    for mtype, ceiling in table.items():
        if mtype not in allowed:
            raise CriteriaError(
                f"score_caps key {mtype!r} is not cappable (allowed: {sorted(allowed)})"
            )
        if not isinstance(ceiling, int) or isinstance(ceiling, bool) or not 0 <= ceiling <= 100:
            raise CriteriaError(
                f"score_caps[{mtype!r}] must be an integer 0..100, got {ceiling!r}"
            )
    return table, match.span()


def _parse_scale(text: str) -> tuple[dict, tuple[int, int] | None]:
    """Parse the optional ```json score_scale``` block. Absent ⇒ (DEFAULT_SCALE, None).

    Shape: {"slope": float, "intercept": float, "silence": {"<n>": float}}. The
    silence keys are Tier 2 criterion POSITIONS (1-based) and are normalized to
    ints here; they are validated against the actual length of the Tier 2 list,
    so a stale key left behind after a reorder or a deletion fails loud instead
    of silently penalizing the wrong criterion. Reordering the list in the
    Settings editor re-points these — the doc says so next to the block.
    """
    match = _SCALE_BLOCK.search(text)
    if match is None:
        return dict(DEFAULT_SCALE), None
    try:
        table = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise CriteriaError(f"score_scale block is not valid JSON: {exc}") from exc
    if not isinstance(table, dict):
        raise CriteriaError("score_scale must be a JSON object")

    def _number(key: str, default: float, lo: float, hi: float) -> float:
        value = table.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CriteriaError(f"score_scale[{key!r}] must be a number, got {value!r}")
        if not lo <= value <= hi:
            raise CriteriaError(f"score_scale[{key!r}] must be in {lo}..{hi}, got {value!r}")
        return float(value)

    # slope > 0 or the score would run backwards; the upper bounds are sanity
    # rails against a typo (a slope of 160 pins every job to the clamp).
    slope = _number("slope", DEFAULT_SCALE["slope"], 0.01, 50.0)
    intercept = _number("intercept", DEFAULT_SCALE["intercept"], 0.0, 100.0)

    raw_silence = table.get("silence", {})
    if not isinstance(raw_silence, dict):
        raise CriteriaError("score_scale['silence'] must be a JSON object of criterion -> value")
    count = len(parse_tier2(text))
    silence: dict[int, float] = {}
    for key, value in raw_silence.items():
        try:
            index = int(key)
        except (TypeError, ValueError) as exc:
            raise CriteriaError(
                f"score_scale silence key {key!r} must be a criterion number"
            ) from exc
        if count and not 1 <= index <= count:
            raise CriteriaError(
                f"score_scale silence key {index} is outside the Tier 2 list (1..{count}) — "
                "the list was probably reordered or shortened"
            )
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CriteriaError(f"score_scale silence[{key!r}] must be a number, got {value!r}")
        if not SILENCE_MIN <= value <= SILENCE_MAX:
            raise CriteriaError(
                f"score_scale silence[{key!r}] must be in "
                f"{SILENCE_MIN}..{SILENCE_MAX}, got {value!r}"
            )
        silence[index] = float(value)

    # "derived_from_total" is the machine-ownership stamp _scale_sized_text
    # writes and reads (the Σw the block was sized to); it carries no scoring
    # meaning here.
    stamp = table.get("derived_from_total", 0)
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
        raise CriteriaError("score_scale['derived_from_total'] must be a number")
    unknown = set(table) - {"slope", "intercept", "silence", "derived_from_total"}
    if unknown:
        raise CriteriaError(f"score_scale has unknown keys: {sorted(unknown)}")
    return {"slope": slope, "intercept": intercept, "silence": silence}, match.span()


def load_criteria(path: Path | None = None) -> Criteria:
    global _cache
    if path is None:
        path = CRITERIA_PATH  # resolved at call time so tests can monkeypatch it
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError as exc:
        raise CriteriaError(f"criteria doc not found: {path}") from exc

    if path == CRITERIA_PATH and _cache is not None and _cache[0] == mtime:
        return _cache[1]

    text = path.read_text(encoding="utf-8")
    match = _PARAMS_BLOCK.search(text)
    if match is None:
        raise CriteriaError(
            f"no ```json tier1_params``` block in {path.name} — "
            "the Tier 1 filters cannot run without it"
        )
    try:
        params = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise CriteriaError(f"tier1_params block is not valid JSON: {exc}") from exc

    for key, expected in _REQUIRED_KEYS.items():
        if key not in params:
            raise CriteriaError(
                f"tier1_params missing required key {key!r}", field=key, kind="missing"
            )
        if not isinstance(params[key], expected):
            raise CriteriaError(
                f"tier1_params[{key!r}] must be {expected.__name__}, "
                f"got {type(params[key]).__name__}",
                field=key,
                kind=expected.__name__,
            )
    _validate_radius(params)

    persona, persona_span = _parse_persona(text)
    taxonomy, taxonomy_span, taxonomy_declared = _parse_taxonomy(text)
    bands_cfg, bands_span = _parse_level_bands(text)
    from jshq.ats.normalize import compile_level_bands

    compiled_bands, band_fallback = compile_level_bands(bands_cfg)
    band_labels: dict = {}
    for entry in bands_cfg["bands"]:  # first occurrence wins (junior appears twice)
        band_labels.setdefault(entry["band"], entry.get("label") or entry["band"].replace("_", " "))
    emittable = {b for b, _ in compiled_bands} | {band_fallback}

    # A target/flag band nothing can emit is a filter that silently matches
    # nothing — the class of typo this block exists to make impossible.
    named = set(params["target_title_bands"]) | set(params["flag_title_bands"])
    unknown_bands = sorted(named - emittable)
    if unknown_bands:
        raise CriteriaError(
            f"tier1_params names title bands {unknown_bands}, which no level_bands "
            f"entry can produce (emittable: {sorted(emittable)}). A band that is "
            "never emitted is a filter that silently matches nothing."
        )
    # The caps table shares one namespace, so a LEVEL band must never read the
    # management-type or function-check key of the same name.
    cappable = frozenset(emittable - {"ic"} - set(CAPPABLE_TYPES) - FUNCTION_CAP_KEYS)

    adjustments, adjust_span = _parse_adjustments(text)
    caps, caps_span = _parse_caps(text, cappable)
    scale, scale_span = _parse_scale(text)

    # Splice out every machine block (params + optional persona/adjustments/
    # caps/scale), highest offset first so earlier spans stay valid, then drop
    # the tier2 markers.
    spans = [match.span()] + [
        s
        for s in (
            persona_span,
            taxonomy_span,
            bands_span,
            adjust_span,
            caps_span,
            scale_span,
        )
        if s
    ]
    body = text
    for start, end in sorted(spans, reverse=True):
        body = body[:start] + body[end:]
    prose = _COMMENTS.sub("", body)
    # Splicing a block (and its guidance comment) out leaves the blank lines
    # that surrounded it. Markdown reads 2+ blank lines as one break anyway, so
    # collapse them rather than letting every added block widen the gaps.
    prose = re.sub(r"\n{3,}", "\n\n", prose)
    tier2 = parse_tier2(text)
    craft_criterion, no_negative, craft_explicit = _derive_tier2_attributes(
        tier2,
        # Pre-Phase-2 shape: none of the machine blocks Phase 2 introduced.
        legacy_eligible=(
            persona_span is None and taxonomy_span is None and bands_span is None
        ),
    )
    criteria = Criteria(
        params=params,
        prose=prose,
        adjustments=adjustments,
        caps=caps,
        scale=scale,
        tier2=tier2,
        persona=persona,
        taxonomy=taxonomy,
        level_bands=tuple(compiled_bands),
        level_band_fallback=band_fallback,
        level_band_labels=band_labels,
        cappable_bands=cappable,
        craft_criterion=craft_criterion,
        no_negative_criteria=no_negative,
        craft_explicit=craft_explicit,
        taxonomy_is_default=taxonomy_span is None,
        functions_declared="functions" in taxonomy_declared,
        quadrants_declared="quadrant_labels" in taxonomy_declared,
    )
    if path == CRITERIA_PATH:
        _cache = (mtime, criteria)
    return criteria


def persona_display_name(path: Path | None = None) -> str:
    """The persona name for prompt builders that don't otherwise load criteria
    (compose, tailor, refine).

    A broken doc degrades to the neutral name here rather than raising: naming
    is cosmetic, and a criteria typo must not take drafting down with scoring.
    The scoring pipeline still fails loud on the same doc.
    """
    try:
        return load_criteria(path).display_name
    except CriteriaError:
        return NEUTRAL_DISPLAY_NAME


def _collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


# Tier 2 items carry an optional numeric weight (Phase 8): a 1.0-centered
# importance multiplier rendered inline as a `[w: X]` suffix, e.g.
# `4. **Mission alignment** — … [w: 2]`. The suffix is OMITTED when the weight is
# the 1.0 default, so an unweighted doc stays byte-identical — the byte-stable
# no-op-write contract the editor + tests rely on. The token rides along in the
# prose sent to the scorer, which reads it as emphasis; there is no arithmetic.
DEFAULT_TIER2_WEIGHT = 1.0
MIN_TIER2_WEIGHT = 0.25
MAX_TIER2_WEIGHT = 4.0

# Inner `\s*` before `]` tolerates a hand edit like `[w: 2 ]` (the doc is the
# editable source of truth) — the app's own renderer never emits inner spaces.
_WEIGHT_SUFFIX = re.compile(r"\s*\[w:\s*([0-9]+(?:\.[0-9]+)?)\s*\]\s*$")

# Criterion ATTRIBUTES (Phase 2), rendered as `[craft]` / `[bonus]` tokens
# beside the weight: `5. **Grow craft…** [craft] [w: 2.5]`.
#
#   [craft]  this criterion IS the craft-versus-convert axis. The scorer is told
#            it may never come back null, and craft_lean (and therefore the
#            "[tension: x] " label the frontend parses) is derived from it.
#   [bonus]  bonus-only: never scores negative, 0 when absent.
#
# These ride WITH the item rather than living in a position-keyed block like
# score_scale's silence values, because reordering the list in the Settings
# editor must not re-point them. Silence values can survive that coupling (the
# doc warns to re-check them after a reorder); a craft axis silently jumping to
# a different criterion would mis-derive every tension label on every job.
_ATTR_SUFFIX = re.compile(r"\s*\[\s*(craft|bonus)\s*\]\s*$", re.I)

# A marker the suffix pattern did NOT consume — `[craft].`, `**[bonus]**`,
# `[craft] and more`. Left alone these read as ordinary criterion text, which
# would be indistinguishable from a doc that declares no markers at all and
# would silently re-arm the legacy fallback below. Fail loud instead: the same
# contract as a malformed machine block.
_STRAY_ATTR = re.compile(r"\[\s*(?:craft|bonus)\s*\]", re.I)

# Pre-Phase-2 docs carry no markers at all. They were written against the
# shipped 11-criterion rubric, whose craft axis is criterion 5 and whose
# bonus-only criterion is 11, so a marker-less list of exactly that length keeps
# its old meaning. A marker-less list of any OTHER length gets no craft axis
# rather than a guess — everything downstream degrades cleanly.
#
# The inference additionally requires a doc with NONE of the Phase-2 machine
# blocks (persona / taxonomy / level_bands) — the shape a pre-Phase-2 doc
# necessarily has. Wizard- and starter-derived docs always carry a persona
# block, so a user who happens to rank exactly 11 wishes does not silently
# resurrect position-keyed semantics from a rubric they never saw.
LEGACY_TIER2_COUNT = 11
LEGACY_CRAFT_CRITERION = 5
LEGACY_NO_NEGATIVE_CRITERIA = frozenset({11})


def _clamp_weight(value: float) -> float:
    return max(MIN_TIER2_WEIGHT, min(MAX_TIER2_WEIGHT, value))


def _fmt_weight(value: float) -> str:
    return f"{value:g}"  # 2.0 -> "2", 1.5 -> "1.5", 0.25 -> "0.25"


def _split_tokens(line: str) -> tuple[str, float, bool, bool]:
    """Strip the trailing `[w: X]` / `[craft]` / `[bonus]` tokens off a criterion
    line ⇒ (text, weight, craft, bonus_only). Missing weight ⇒ default;
    out-of-range (a hand edit) is clamped so the scorer never chokes.

    Order-tolerant, because the doc is hand-editable; render_tier2 always emits
    the canonical `[craft] [bonus] [w: X]` order.
    """
    weight = DEFAULT_TIER2_WEIGHT
    craft = bonus = False
    seen_weight = False
    while True:
        m = _WEIGHT_SUFFIX.search(line)
        # Only the FIRST (rightmost) weight token counts, matching the
        # single-strip behavior this replaced: a line ending `[w: 2] [w: 3]`
        # keeps 3 rather than quietly preferring the leftmost.
        if m and not seen_weight:
            weight = _clamp_weight(float(m.group(1)))
            seen_weight = True
            line = line[: m.start()].rstrip()
            continue
        m = _ATTR_SUFFIX.search(line)
        if m:
            if m.group(1).lower() == "craft":
                craft = True
            else:
                bonus = True
            line = line[: m.start()].rstrip()
            continue
        return line, weight, craft, bonus


def parse_tier2(text: str) -> list[dict]:
    """Ordered Tier 2 criteria between the tier2 markers as {text, weight} dicts.
    Each item is the text after a leading 'N.' (wrapped continuation lines joined),
    minus an optional trailing `[w: X]` weight token (default 1.0). Returns an
    empty list when the markers are absent."""
    match = _TIER2_BLOCK.search(text)
    if match is None:
        return []
    raw: list[str] = []
    current: str | None = None
    for line in match.group(2).splitlines():
        head = re.match(r"\s*\d+\.\s+(.*)", line)
        if head:
            if current is not None:
                raw.append(_collapse_ws(current))
            current = head.group(1)
        elif current is not None and line.strip():
            current += " " + line.strip()
    if current is not None:
        raw.append(_collapse_ws(current))
    items: list[dict] = []
    for line in raw:
        body, weight, craft, bonus = _split_tokens(line)
        stray = _STRAY_ATTR.search(body)
        if stray:
            raise CriteriaError(
                f"Tier 2 criterion {len(items) + 1} has a {stray.group(0)} marker that "
                "is not at the end of the line — markers go last, after the text and "
                "before any [w: X]. Left in place it would be read as ordinary text "
                "and the criterion's role would be silently lost."
            )
        items.append(
            {"text": body, "weight": weight, "craft": craft, "bonus_only": bonus}
        )
    return items


def _item_fields(item) -> tuple[str, float, bool, bool]:
    """Normalize a {text, weight, craft, bonus_only} dict (the live shape) or a
    bare string (legacy / forward-compat) to (text, weight, craft, bonus_only)."""
    if isinstance(item, str):
        return _collapse_ws(item), DEFAULT_TIER2_WEIGHT, False, False
    text = _collapse_ws(str(item.get("text", "")))
    try:
        weight = _clamp_weight(float(item.get("weight", DEFAULT_TIER2_WEIGHT)))
    except (TypeError, ValueError):
        weight = DEFAULT_TIER2_WEIGHT
    return text, weight, bool(item.get("craft")), bool(item.get("bonus_only"))


def render_tier2(items: list) -> str:
    lines = []
    for i, item in enumerate(items, 1):
        text, weight, craft, bonus = _item_fields(item)
        # Tokens are omitted at their defaults, so a doc that uses none of them
        # stays byte-identical — the no-op-write contract the editor relies on.
        suffix = ""
        if craft:
            suffix += " [craft]"
        if bonus:
            suffix += " [bonus]"
        if weight != DEFAULT_TIER2_WEIGHT:
            suffix += f" [w: {_fmt_weight(weight)}]"
        lines.append(f"{i}. {text}{suffix}")
    return "\n".join(lines)


def _derive_tier2_attributes(
    tier2: list, legacy_eligible: bool
) -> tuple[int | None, frozenset, bool]:
    """(craft criterion position, bonus-only positions, craft was explicit).

    Positions are 1-based, matching how the doc and the model both number the
    criteria. Derived rather than asserted against a code constant: the count
    that used to live in haiku.TIER2_COUNT is now just len(tier2).

    The third value separates "the author marked this criterion" from "we
    inferred it from a legacy-shaped doc". Only an explicit axis is worth
    failing a score over — see haiku._parse_tier2's never-null rule.

    legacy_eligible says the doc has none of the Phase-2 machine blocks, the
    only shape the 11-item positional fallback may apply to (see the
    LEGACY_TIER2_COUNT comment).
    """
    craft = [i for i, item in enumerate(tier2, 1) if item.get("craft")]
    no_negative = frozenset(
        i for i, item in enumerate(tier2, 1) if item.get("bonus_only")
    )
    if len(craft) > 1:
        raise CriteriaError(
            f"Tier 2 criteria {craft} are all marked [craft] — the craft/convert "
            "axis is a single criterion, and craft_lean is derived from it"
        )
    if craft and craft[0] in no_negative:
        raise CriteriaError(
            f"Tier 2 criterion {craft[0]} is marked both [craft] and [bonus]. The "
            "bonus clamp would floor the craft axis at 0, so the lean could never "
            "go negative and the convert/sell tension label would be unreachable."
        )
    if craft:
        return craft[0], no_negative, True
    if no_negative:
        # Markers are in use; the author simply declared no craft axis.
        return None, no_negative, False
    if legacy_eligible and len(tier2) == LEGACY_TIER2_COUNT:
        return LEGACY_CRAFT_CRITERION, LEGACY_NO_NEGATIVE_CRITERIA, False
    return None, no_negative, False


def _reload_after_write(path: Path) -> Criteria:
    """Reload after a writer has replaced the doc, dropping the mtime cache
    first. The writer KNOWS the content changed; the cache cannot be trusted
    to notice, because Windows updates file times on a coarse timer tick
    (~15ms), so back-to-back writes can share one st_mtime and the stale
    parse would be served."""
    global _cache
    _cache = None
    return load_criteria(path)


def write_criteria(
    tier1_params: dict, tier2_criteria: list, path: Path | None = None,
    *, size_scale: bool = False,
) -> Criteria:
    """Splice new Tier 1 params + Tier 2 list into the doc and swap atomically.

    The assembled text is validated by load_criteria() against a temp file BEFORE
    it replaces the live doc, so an invalid edit raises CriteriaError and never
    corrupts the criteria (a broken doc would hard-fail all scoring). Returns the
    freshly-loaded Criteria (which also refreshes the mtime cache).

    size_scale=True (the ranked-list endpoints) additionally sizes a
    machine-owned score_scale block to the list being saved — same swap, one
    validation. See _scale_sized_text for the ownership contract.
    """
    if path is None:
        path = CRITERIA_PATH
    original = path.read_text(encoding="utf-8")
    text = original
    if _PARAMS_BLOCK.search(text) is None:
        raise CriteriaError("no ```json tier1_params``` block to replace")
    if _TIER2_BLOCK.search(text) is None:
        raise CriteriaError("no tier2 markers to replace")

    block = "```json tier1_params\n" + json.dumps(tier1_params, indent=2, ensure_ascii=False) + "\n```"
    text = _PARAMS_BLOCK.sub(lambda _m: block, text, count=1)
    rendered = render_tier2(tier2_criteria)
    text = _TIER2_BLOCK.sub(lambda m: m.group(1) + rendered + m.group(3), text, count=1)
    if size_scale:
        text = _scale_sized_text(original, text)

    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        load_criteria(tmp)  # full validation; path != CRITERIA_PATH so uncached
    except CriteriaError:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(path)  # atomic on the same filesystem
    return _reload_after_write(path)


def read_editable(path: Path | None = None) -> tuple[dict, list[dict]]:
    """(validated Tier 1 params, ordered Tier 2 {text, weight} list) for the
    Settings editor. Resolves CRITERIA_PATH at call time so endpoints follow a
    monkeypatch."""
    if path is None:
        path = CRITERIA_PATH
    return load_criteria(path).params, parse_tier2(path.read_text(encoding="utf-8"))


def derive_scale(tier2_items: list[dict]) -> dict:
    """A score_scale sized to THIS rubric (Phase 5b).

    intercept 50, slope 20/Σw, where Σw is the list's actual weight sum.
    Pushed through aggregate() exactly: +2 on every criterion ⇒ 90; an
    average of +1 across the list ⇒ 70 = POSITIVE_FIT — so the threshold
    means "solidly positive on average" at every rubric size, where the
    shipped 1.6/55 made 70 unreachable below five wizard-ramped items.
    Eleven ramped items give slope ≈ 1.6, the constants this generalizes.
    Clamped to the parser's 0.01..50 slope rail (reachable only by a
    single-item list hand-weighted to 0.25)."""
    total = sum(float(t.get("weight", 1.0)) for t in tier2_items)
    slope = 20.0 / total if total else 20.0
    return {"slope": min(50.0, round(slope, 4)), "intercept": 50.0}


def _scale_sized_text(old_text: str, new_text: str) -> str:
    """The score_scale half of a ranked-list write (Phase 5b).

    A pure text transform run INSIDE write_criteria / write_synthesis_prose
    (size_scale=True), between the tier2 splice and the tmp validation — so
    re-sizing (including pruning silence keys a shrunk list can no longer
    carry) lands in the same atomic swap the list does, and the validator sees
    the finished doc. Three contracts, in order:

    - An unchanged weight profile (same count, same weights) touches nothing,
      so the editor's byte-identical no-op save stays a no-op.
    - A HAND-AUTHORED block is never touched. Ownership is a SELF-fingerprint:
      machine writes stamp "derived_from_total" (the Σw the block was sized
      to; scoring-inert, ignored by _parse_scale) and a block is machine-owned
      when its slope/intercept equal derive_scale of its OWN recorded total.
      Fingerprinting against the block's own stamp — not against
      derive_scale(pre-save list) — is what survives the user hand-editing a
      [w: X] token in the doc between saves: under the old pre-list
      fingerprint that edit orphaned the machine's own block as
      "hand-authored" forever, silently breaking the all-+2 ⇒ 90 contract on
      every later save. A hand-TUNED slope still sticks (the tune breaks the
      self-fingerprint, so the block reads hand-authored), and the pre-list
      fingerprint remains as a fallback for blocks written before the stamp
      existed. The shipped example doc's block (1.6/55, no stamp) matches
      neither test, so live-doc round-trip tests cannot clobber it. (A
      hand-authored block whose silence keys outlive a shrunk list still
      hard-fails validation, by design: the parser's error names the
      reorder, and silently dropping a hand-set value would be worse.)
    - An ABSENT block is machine-owned: the starter doc's first wishlist
      save gets a scale sized to it. Machine-block silence entries survive a
      re-derive while their criterion index still exists; out-of-range keys
      are pruned.
    """
    pre = parse_tier2(old_text)
    post = parse_tier2(new_text)
    if [float(t.get("weight", 1.0)) for t in pre] == [
        float(t.get("weight", 1.0)) for t in post
    ]:
        return new_text
    match = _SCALE_BLOCK.search(old_text)
    silence: dict = {}
    if match is not None:
        try:
            current = json.loads(match.group(1))
        except json.JSONDecodeError:
            return new_text  # broken block: let the validator report it
        if not isinstance(current, dict):
            return new_text
        recorded = current.get("derived_from_total")
        if isinstance(recorded, (int, float)) and not isinstance(recorded, bool):
            expected = derive_scale([{"weight": float(recorded)}])
        else:
            expected = derive_scale(pre)  # pre-stamp block: legacy fingerprint
        owned = (
            float(current.get("slope", -1.0) or -1.0) == expected["slope"]
            and float(current.get("intercept", -1.0) or -1.0) == expected["intercept"]
        )
        if not owned:
            return new_text
        raw = current.get("silence") or {}
        if isinstance(raw, dict):
            silence = {k: v for k, v in raw.items() if str(k).isdigit() and int(k) <= len(post)}
    payload = {
        **derive_scale(post),
        "derived_from_total": sum(float(t.get("weight", 1.0)) for t in post),
    }
    if silence:
        payload["silence"] = silence
    block = "```json score_scale\n" + json.dumps(payload, indent=2) + "\n```"
    if _SCALE_BLOCK.search(new_text) is not None:
        return _SCALE_BLOCK.sub(lambda _m: block, new_text, count=1)
    if "<!-- tier2:end -->" in new_text:
        # First emission: anchor right under the list it prices.
        return new_text.replace("<!-- tier2:end -->", "<!-- tier2:end -->\n\n" + block, 1)
    return new_text  # no anchor: leave the doc alone, the validator decides


def write_persona(
    display_name: str | None, domain_label: str, path: Path | None = None
) -> Criteria:
    """Rewrite the persona block (display_name + domain_label) and swap atomically.

    display_name may be None to name nobody. Same tmp→load_criteria→replace
    discipline as write_criteria, so an invalid value (too long, embedded quote)
    raises CriteriaError before the live doc is touched. When the doc has no
    persona block yet, one is inserted immediately before the tier1_params fence
    (which every editable doc has). Returns the freshly-loaded Criteria.
    """
    if path is None:
        path = CRITERIA_PATH
    text = path.read_text(encoding="utf-8")
    persona = {"display_name": display_name, "domain_label": domain_label}
    block = "```json persona\n" + json.dumps(persona, indent=2, ensure_ascii=False) + "\n```"
    if _PERSONA_BLOCK.search(text) is not None:
        text = _PERSONA_BLOCK.sub(lambda _m: block, text, count=1)
    elif _PARAMS_BLOCK.search(text) is not None:
        # No persona block yet — anchor a new one just above the params fence.
        text = _PARAMS_BLOCK.sub(lambda m: block + "\n\n" + m.group(0), text, count=1)
    else:
        raise CriteriaError("no ```json tier1_params``` block to anchor a persona block to")

    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        load_criteria(tmp)  # full validation (incl. _parse_persona); uncached
    except CriteriaError:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(path)  # atomic on the same filesystem
    return _reload_after_write(path)


def _discipline_slug(label: str) -> str:
    """A snake_case token for a free-text field label (disciplines keys are sent
    to the model as enum values and stored in the DB). Falls back to a fixed
    token when the label has no usable ascii letters."""
    slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    return slug if re.fullmatch(r"[a-z][a-z0-9_]*", slug or "") else "in_field"


def write_field(field_label: str, path: Path | None = None) -> Criteria:
    """Declare the user's field(s) as the in-band disciplines — the wizard's one
    field question (Phase 4). A comma splits the answer into several fields
    ("product management, design leadership" ⇒ two in-band disciplines): the
    wizard's own placeholder teaches that format, and fusing it into one token
    would make the fields inseparable in Settings and in the enum sent to the
    model. Writes a MINIMAL taxonomy block (the field(s) plus 'other' and
    'unclear') so the function check targets the user's fields instead of the
    design-specific code default. Richer vocabulary (glosses, a functions map)
    is a hand edit to the doc's taxonomy block — no Settings surface edits it.
    Same tmp→load→replace discipline as write_persona; a blank
    label raises CriteriaError before the doc is touched. Inserts a taxonomy
    block when absent, replaces it when present."""
    if path is None:
        path = CRITERIA_PATH
    labels = [" ".join(part.split()) for part in str(field_label).split(",")]
    labels = [label for label in labels if label]
    if not labels:
        raise CriteriaError("field label must not be blank")
    in_band: dict[str, str] = {}
    for label in labels:
        slug = _discipline_slug(label)
        if slug in ("other", "unclear"):
            slug += "_field"  # never shadow the reserved out-of-band keys
        in_band.setdefault(slug, label)  # first label wins a slug collision
    taxonomy = {
        "disciplines": {
            **in_band,
            "other": "a role in some other field",
            "unclear": (
                "the evidence is genuinely thin — flagged for manual review, "
                "never a pass"
            ),
        },
        "in_band_disciplines": list(in_band),
    }
    text = path.read_text(encoding="utf-8")
    block = "```json taxonomy\n" + json.dumps(taxonomy, indent=2, ensure_ascii=False) + "\n```"
    if _TAXONOMY_BLOCK.search(text) is not None:
        text = _TAXONOMY_BLOCK.sub(lambda _m: block, text, count=1)
    elif _PARAMS_BLOCK.search(text) is not None:
        # No taxonomy block yet — anchor a new one just above the params fence.
        text = _PARAMS_BLOCK.sub(lambda m: block + "\n\n" + m.group(0), text, count=1)
    else:
        raise CriteriaError("no ```json tier1_params``` block to anchor a taxonomy block to")

    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        load_criteria(tmp)  # full validation (incl. _parse_taxonomy); uncached
    except CriteriaError:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(path)  # atomic on the same filesystem
    return _reload_after_write(path)


# Machine-block openers a synthesis body must never contain: a duplicate fence
# would be silently shadowed by the first (load_criteria wouldn't catch it),
# so reject at the door instead.
_MACHINE_OPENERS = (
    "```json tier1_params",
    "```json persona",
    "```json taxonomy",
    "```json level_bands",
    "```json score_adjustments",
    "```json score_caps",
    "```json score_scale",
)


def write_synthesis_prose(
    body: str, tier2_criteria: list | None = None, path: Path | None = None,
    *, size_scale: bool = False,
) -> Criteria:
    """Splice synthesized reflection prose (and optionally a refined Tier 2
    list) into the doc atomically.

    The body lands between <!-- synthesis:start/end --> fences: present ⇒
    replaced in place; absent ⇒ inserted above the ## Scoring rubric heading
    (the section order the shipped example uses), or appended when the doc has
    no such heading. Prose and Tier 2 are ONE text pass and one atomic swap —
    an invalid half never lands. The body is sanitized first: HTML comments
    (they'd break the comment stripper or smuggle a fence) and machine-block
    openers raise CriteriaError; the deterministic renderer never emits either,
    so this is the backstop against model output reaching config.
    """
    if path is None:
        path = CRITERIA_PATH
    body = body.strip()
    if not body:
        raise CriteriaError("synthesis body is empty")
    if "<!--" in body or "-->" in body:
        raise CriteriaError("synthesis body must not contain HTML comments")
    lowered = body.lower()
    for opener in _MACHINE_OPENERS:
        if opener in lowered:
            raise CriteriaError(f"synthesis body must not contain a {opener}``` block")

    original = path.read_text(encoding="utf-8")
    text = original
    fenced = "<!-- synthesis:start -->\n" + body + "\n<!-- synthesis:end -->"
    if _SYNTHESIS_BLOCK.search(text) is not None:
        text = _SYNTHESIS_BLOCK.sub(lambda _m: fenced, text, count=1)
    elif _RUBRIC_HEADING.search(text) is not None:
        text = _RUBRIC_HEADING.sub(lambda m: fenced + "\n\n" + m.group(0), text, count=1)
    else:
        text = text.rstrip("\n") + "\n\n" + fenced + "\n"

    if tier2_criteria is not None:
        if _TIER2_BLOCK.search(text) is None:
            raise CriteriaError("no tier2 markers to replace")
        rendered = render_tier2(tier2_criteria)
        text = _TIER2_BLOCK.sub(lambda m: m.group(1) + rendered + m.group(3), text, count=1)
        if size_scale:
            text = _scale_sized_text(original, text)

    # A hand-added stray fence would make the NEXT re-run truncate everything
    # between the outermost markers — fail loud now instead.
    if text.count("<!-- synthesis:start -->") != 1 or text.count("<!-- synthesis:end -->") != 1:
        raise CriteriaError("the doc must contain exactly one synthesis fence pair")

    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        load_criteria(tmp)  # full validation; path != CRITERIA_PATH so uncached
    except CriteriaError:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(path)  # atomic on the same filesystem
    return _reload_after_write(path)


def _fmt_terms(values, limit: int = 6) -> str:
    items = [str(v) for v in values]
    shown = ", ".join(items[:limit])
    if len(items) > limit:
        shown += ", …"
    return shown


def render_params_summary(params: dict) -> str:
    """Plain-English markdown of the Tier 1 parameters for the in-app rubric
    viewer (QA 2026-06-15). The raw `tier1_params` JSON reads as config; this
    surfaces the actual current thresholds (comp, commute, locations, sectors,
    titles) so the modal documents the gates rather than dumping a 75-line block.
    Generated from the already-validated params — the doc stays the source of
    truth; the criteria-doc endpoint inserts this ahead of the raw block, which
    the viewer keeps available behind a disclosure. Title bands humanize their
    snake_case (senior_director → 'senior director')."""
    floor = params.get("comp_floor")
    target = params.get("comp_target")
    allow = params.get("location_allowlist") or []
    overrides = list((params.get("company_location_overrides") or {}).keys())
    regions = params.get("remote_regions") or []
    sectors = params.get("excluded_sectors") or []
    targets = [str(t).replace("_", " ") for t in (params.get("target_title_bands") or [])]
    flagged = [str(t).replace("_", " ") for t in (params.get("flag_title_bands") or {})]
    radius = params.get("location_radius") or {}
    minutes = radius.get("radius_minutes")
    center = (radius.get("center") or {}).get("label")

    lines = ["### Current Tier 1 settings", ""]
    if floor is not None:
        comp = f"floor ${floor:,}"
        if target is not None:
            comp += f", target ${target:,}"
        lines.append(f"- **Compensation** — {comp}.")
    if minutes:
        where = f" of {center}" if center else " of home"
        lines.append(f"- **Commute** — within {minutes:g} minutes' drive{where}.")
    if allow:
        extra = f", plus per-company exceptions for {_fmt_terms(overrides, 4)}" if overrides else ""
        lines.append(f"- **Allowed locations** — {len(allow)} towns ({_fmt_terms(allow)}){extra}.")
    if regions:
        lines.append(f"- **Remote** — accepted when US-scoped or unscoped ({_fmt_terms(regions)}).")
    if sectors:
        lines.append(f"- **Excluded sectors** — {_fmt_terms(sectors)}.")
    if targets:
        flag = f"; flagged — {_fmt_terms(flagged)}" if flagged else ""
        lines.append(f"- **Target titles** — {_fmt_terms(targets)}{flag}.")
    return "\n".join(lines)
