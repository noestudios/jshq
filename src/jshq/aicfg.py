"""Per-task AI model selection (Providers Tiers 1–2 — docs/PROVIDERS-FEASIBILITY.md).

The one authority for which provider and model each AI task runs on. Two
user-facing axes — "analysis" (scoring, job-URL parsing, LinkedIn title
suggestions, roadmap synthesis, rule proposals) and "writing" (compose,
tailor, refine) — each overridable from Settings → System through a single
`ai_models` settings row. An unset axis resolves to each task's shipped
default (Anthropic, per-task tiers), so a fresh install behaves exactly as
it did when the ids were module constants.

Since Tier 2 an axis can also point at the user's OpenAI-compatible endpoint
(providers.py) with a free-text model id. The settings row stays backward
compatible: a bare Tier-1 model string reads as the Anthropic provider, an
axis object carries {"provider", "model"}. No migration.

Resolution happens where the DB connection lives (main.py endpoints and the
rescore loop in scoring/__init__.py) via ``binding_for``, and the resolved
Binding is passed INTO the feature modules — so a call and the
`usage.record_usage` that bills it can never disagree. Feature modules fall
back to DEFAULTS when called without a model (direct callers, tests,
scripts). Nothing here talks to the network.
"""

import json
import sqlite3
from dataclasses import dataclass

from jshq import providers

# Shipped default per task — what an unset axis resolves to. These are the ids
# that used to live as MODEL constants in each feature module (two of them
# duplicated as bare literals; this map is now the only place ids are spelled).
DEFAULTS = {
    # analysis axis
    "scoring": "claude-haiku-4-5",
    "jobparse": "claude-haiku-4-5",
    "linkedin_titles": "claude-haiku-4-5",
    "synthesis": "claude-sonnet-5",
    "learned": "claude-sonnet-5",
    # writing axis
    "compose": "claude-sonnet-5",
    "tailor": "claude-sonnet-5",
    "refine": "claude-sonnet-5",
}

# The two override axes and the tasks each governs. The analysis axis has mixed
# defaults on purpose (bulk scoring is Haiku-priced; synthesis and rule
# proposals reason over whole JDs on the Sonnet tier) — an override flattens
# the axis onto one model, the default leaves each task on its own tier.
AXES = {
    "analysis": ("scoring", "jobparse", "linkedin_titles", "synthesis", "learned"),
    "writing": ("compose", "tailor", "refine"),
}
TASK_AXIS = {task: axis for axis, tasks in AXES.items() for task in tasks}

# Curated choices for the Settings selects. Every id here must be priced in
# usage.PRICES (test-pinned) so a selectable model never bills as a silent
# $0.00 — and must have correct thinking/sampling rules below.
MODELS = [
    {"id": "claude-haiku-4-5", "label": "Claude Haiku 4.5"},
    {"id": "claude-sonnet-4-6", "label": "Claude Sonnet 4.6"},
    {"id": "claude-sonnet-5", "label": "Claude Sonnet 5"},
    {"id": "claude-opus-5", "label": "Claude Opus 5"},
]
MODEL_IDS = {m["id"] for m in MODELS}

# The key-test liveness ping stays on the cheapest tier regardless of settings:
# it proves the key works, not the chosen model.
PING_MODEL = "claude-haiku-4-5"

# The scoring calibration baseline (tests/fixtures/calibration/baseline.json)
# is blessed against the default scoring model. Any other analysis choice is
# uncalibrated — Settings shows a drift note instead of pretending parity.
CALIBRATED_SCORING_MODEL = DEFAULTS["scoring"]

SETTING_KEY = "ai_models"

# Models that run extended thinking ON unless explicitly disabled. Every prompt
# in this app is written for no thinking (strict-JSON or plain-text calls;
# with thinking on, the model can spend the whole max_tokens budget thinking
# and return an empty text block, stop_reason=max_tokens), so these get
# thinking={"type": "disabled"}. Haiku 4.5 / Sonnet 4.6 run without thinking
# when the param is simply omitted — and omission is also the safe default for
# any unknown id (Fable 5, for example, 400s on "disabled").
_THINKING_ON_BY_DEFAULT = {"claude-sonnet-5", "claude-opus-5"}

# Models that reject sampling params outright (temperature/top_p/top_k → 400).
# The analysis calls pass temperature=0 for run-to-run stability wherever the
# model still accepts it; on these tiers the knob simply doesn't exist.
_NO_SAMPLING = {"claude-sonnet-5", "claude-opus-5"}


@dataclass(frozen=True)
class Binding:
    """A resolved (provider, model) for one task, plus the two derived facts
    every billing site needs: ``ledger_key`` namespaces compat spend so a
    local model that happens to share an Anthropic id can never pollute its
    spend row, and ``local`` marks loopback endpoints for the $0.00-local
    (not silently-unpriced) ledger label."""

    provider: str  # "anthropic" | "openai_compat"
    model: str     # the bare wire model id
    ledger_key: str
    local: bool


def _normalize_axis(value) -> dict | None:
    """One axis value from the settings row → {"provider", "model"} or None
    (unset). A bare string is the Tier-1 shorthand for Anthropic; an
    anthropic object with no model is semantically unset (the default IS
    Anthropic per-task); a compat object without a model is unusable and
    reads as unset. Anything unreadable reads as unset."""
    if isinstance(value, str):
        return {"provider": "anthropic", "model": value} if value.strip() else None
    if not isinstance(value, dict):
        return None
    provider = value.get("provider")
    model = value.get("model")
    model = model.strip() if isinstance(model, str) and model.strip() else None
    if provider == "anthropic":
        return {"provider": "anthropic", "model": model} if model else None
    if provider == "openai_compat":
        return {"provider": "openai_compat", "model": model} if model else None
    return None


def read_overrides(conn: sqlite3.Connection) -> dict:
    """{"analysis": {"provider", "model"}|None, "writing": ...} from the
    settings row. Tolerant of a missing/garbled row — anything unreadable
    reads as unset; Tier-1 bare-string rows normalize on read forever."""
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (SETTING_KEY,)
    ).fetchone()
    data = {}
    if row and row["value"]:
        try:
            loaded = json.loads(row["value"])
            if isinstance(loaded, dict):
                data = loaded
        except ValueError:
            pass
    return {axis: _normalize_axis(data.get(axis)) for axis in AXES}


def read_remembered(conn: sqlite3.Connection) -> dict:
    """remembered[axis][provider] = the last explicitly chosen model for that
    provider on that axis — the Settings provider picker's switch-back memory
    (pick Anthropic again and your prior model choice is still there).
    Tolerant like read_overrides: unknown axes/providers are dropped, and an
    Anthropic id that has left the curated list is dropped too (restoring it
    would only 422 at the PUT)."""
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (SETTING_KEY,)
    ).fetchone()
    data = {}
    if row and row["value"]:
        try:
            loaded = json.loads(row["value"])
            if isinstance(loaded, dict):
                data = loaded
        except ValueError:
            pass
    stored = data.get("remembered")
    out: dict = {axis: {} for axis in AXES}
    if isinstance(stored, dict):
        for axis in AXES:
            per = stored.get(axis)
            if not isinstance(per, dict):
                continue
            for provider in providers.PROVIDERS:
                model = per.get(provider)
                if not (isinstance(model, str) and model.strip()):
                    continue
                model = model.strip()
                if provider == "anthropic" and model not in MODEL_IDS:
                    continue
                out[axis][provider] = model
    return out


def write_overrides(conn: sqlite3.Connection, overrides: dict) -> None:
    """Persist the two axes plus the switch-back memory in the one settings
    row: every explicit axis choice stamps remembered[axis][provider] =
    model. None (Default) writes no memory — Default is not a model choice,
    and the prior choices for both providers survive it."""
    remembered = read_remembered(conn)
    for axis, choice in overrides.items():
        if choice:
            remembered[axis][choice["provider"]] = choice["model"]
    payload = {
        **overrides,
        "remembered": {axis: per for axis, per in remembered.items() if per},
    }
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (SETTING_KEY, json.dumps(payload)),
    )
    conn.commit()


def binding_for(conn: sqlite3.Connection, task: str) -> Binding:
    """The provider+model `task` should use right now: its axis override when
    one is set, else the shipped Anthropic default. The endpoints and the
    rescore loop resolve through this so the call and its billing agree."""
    override = read_overrides(conn)[TASK_AXIS[task]]
    if override is None or override["provider"] == "anthropic":
        model = (override or {}).get("model") or DEFAULTS[task]
        return Binding("anthropic", model, model, False)
    model = override["model"]
    base_url = providers.compat_base_url(conn)
    return Binding(
        "openai_compat",
        model,
        f"openai-compat:{model}",
        providers.is_local(base_url) if base_url else False,
    )


def model_for(conn: sqlite3.Connection, task: str) -> str:
    """The bare model id `task` resolves to — kept for scripts and callers
    that only need the id; binding_for is the full answer."""
    return binding_for(conn, task).model


def thinking_kwargs(model: str) -> dict:
    """The `thinking` kwarg (or nothing) for a messages.create call on `model`.
    Spread into the call — never pass thinking=None to the SDK."""
    if model in _THINKING_ON_BY_DEFAULT:
        return {"thinking": {"type": "disabled"}}
    return {}


def temperature_kwargs(model: str, temperature: float) -> dict:
    """The `temperature` kwarg where `model` accepts sampling params, else
    nothing (the Sonnet 5 / Opus 5 tier rejects them with a 400)."""
    if model in _NO_SAMPLING:
        return {}
    return {"temperature": temperature}
