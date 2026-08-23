"""Token-cost accounting for model calls (Phase 8 observability).

Reads the `usage` object off a Messages response, prices it at the model's
published per-token rate, and accumulates the running total into the settings
`usage_totals` k/v row. Self-contained: imports nothing from `app`, so any
scorer/composer can record without a circular import.
"""

import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from jshq import paths

# ($ per 1M input tokens, $ per 1M output tokens). Cache reads bill ~0.1x the
# input rate; cache writes ~1.25x (Anthropic pricing, 5-minute TTL).
PRICES = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-sonnet-5": (3.00, 15.00),  # standard; see rate_for() for the intro window
    "claude-opus-5": (5.00, 25.00),
}

# Claude Sonnet 5 launched on introductory pricing ($2/$10 per 1M) that reverts
# to standard ($3/$15) on 2026-09-01. We price each call by its OWN date, so the
# ledger switches to the standard rate automatically on the cutoff — no edit and
# no scheduled task needed. Pre-cutoff records keep the intro rate they were
# billed at; post-cutoff records use standard.
SONNET_5_STANDARD_FROM = date(2026, 9, 1)
_SONNET_5_INTRO = (2.00, 10.00)

_CACHE_READ_MULT = 0.10
_CACHE_WRITE_MULT = 1.25

USAGE_KEY = "usage_totals"
# Fallback per-job cost for the rescore estimate before any real usage exists.
DEFAULT_COST_PER_JOB = 0.006

# File ledger for the read-only harness scripts (2026-08-08). Their spend is
# real but usage_totals deliberately never sees it — recording to the DB would
# break the scripts' nothing-is-written promise, which is why they are trusted
# to measure a rescore before it runs. A file keeps the promise and ends the
# unexplained gap between summed script printouts and the console figure.
# Lives under data/ (gitignored) beside the DB it refuses to write.
HARNESS_LEDGER = paths.DATA_DIR / "harness_spend.jsonl"


def _fields(usage) -> dict:
    """Pull token counts off an SDK usage object or a plain dict; missing -> 0."""
    get = usage.get if isinstance(usage, dict) else (lambda k: getattr(usage, k, 0))
    return {
        "input": get("input_tokens") or 0,
        "output": get("output_tokens") or 0,
        "cache_read": get("cache_read_input_tokens") or 0,
        "cache_write": get("cache_creation_input_tokens") or 0,
    }


def rate_for(model: str, *, on: date | None = None):
    """Per-1M (input, output) rate for `model` on date `on` (default today).
    Sonnet 5's introductory rate auto-reverts to standard on SONNET_5_STANDARD_FROM;
    every other model is a flat lookup. Unknown model -> None."""
    if model == "claude-sonnet-5":
        on = on or date.today()
        return PRICES[model] if on >= SONNET_5_STANDARD_FROM else _SONNET_5_INTRO
    return PRICES.get(model)


def cost_of(model: str, usage, *, on: date | None = None) -> float:
    """USD for one usage record. Unknown model or None usage -> 0.0."""
    rate = rate_for(model, on=on)
    if rate is None or usage is None:
        return 0.0
    inp, out = rate
    f = _fields(usage)
    return (
        f["input"] * inp
        + f["cache_read"] * inp * _CACHE_READ_MULT
        + f["cache_write"] * inp * _CACHE_WRITE_MULT
        + f["output"] * out
    ) / 1_000_000


def read_usage_totals(conn: sqlite3.Connection):
    """The stored totals dict, or None if nothing has been recorded yet."""
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (USAGE_KEY,)).fetchone()
    return json.loads(row["value"]) if row and row["value"] else None


def usages_of(exc) -> list:
    """Per-attempt usages a failed model-call error carries (empty if none), so
    an endpoint's error path can bill the tokens a call spent before it errored.
    Without this, only successful calls hit the ledger and a failing retry loop
    (the sonnet-5 thinking blowup) is invisible in-app until the console shows it."""
    return getattr(exc, "usages", None) or []


def append_harness_ledger(
    script: str, model: str, usage, *, calls: int, cost: float,
    args: str = "", path: Path | None = None,
) -> Path:
    """Append one JSON line for a harness run to HARNESS_LEDGER (see the
    constant's comment for why this is a file, not the DB). `usage` is an SDK
    usage object or the scripts' plain accumulator dict. Returns the path."""
    target = path or HARNESS_LEDGER
    target.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "script": script, "model": model, "calls": calls,
        **_fields(usage or {}),
        "cost": round(cost, 6), "args": args,
    }
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    return target


def record_usage(
    conn: sqlite3.Connection, model: str, usage, *, calls: int = 1, local: bool = False
) -> None:
    """Accumulate one record (or a batch's summed token counts + its call count)
    into usage_totals. No-op for None usage. Caller owns the commit.

    `local` marks a loopback endpoint's spend (Tier 2): its $0.00 is TRUE,
    not a silent understatement, so the entry is labeled `local` instead of
    `unpriced` and the spend total stays exact."""
    if usage is None:
        return
    totals = read_usage_totals(conn) or {
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "by_model": {},
    }
    by = totals["by_model"].setdefault(
        model,
        {"calls": 0, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "cost": 0.0},
    )
    f = _fields(usage)
    by["calls"] += calls
    by["input"] += f["input"]
    by["output"] += f["output"]
    by["cache_read"] += f["cache_read"]
    by["cache_write"] += f["cache_write"]
    by["cost"] = round(by["cost"] + cost_of(model, usage), 6)
    if rate_for(model) is None:
        if local:
            # Loopback endpoint: $0.00 is the real cost, labeled as such.
            by["local"] = True
        else:
            # A model PRICES doesn't know accumulates at $0.00 — say so instead
            # of letting the spend line silently understate. The flag is sticky:
            # any unpriced call taints the entry (its cost is wrong from then on).
            by["unpriced"] = True
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (USAGE_KEY, json.dumps(totals)),
    )
