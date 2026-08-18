"""Human-readable inclusion rules (Phase 7i, decision C).

Rules are the source of truth for what gets pulled in. Each rule is a verb
(include/exclude) + a target (title/location) + literal terms. ``compile_rules``
deterministically lowers them to the three raw arrays the rest of the app already
reads — the ingestion title filter (``title_keywords`` /
``title_exclude_keywords``, settings table) and the Tier 1 location gate
(``location_allowlist``, ``fit_criteria.md``) — so scoring and ingestion are
untouched.

The raw arrays the Settings page used to edit are demoted to a read-mostly
"Advanced — compiled from rules" view: every entry is tagged ``rule`` or
``manual``. Provenance is derived, not separately stored: the live arrays ARE the
manual store — ``manual = live - rule-emitted``. So a one-off keyword added in the
Advanced view survives a recompile (it stays in the live array, never rule-emitted,
so it keeps reading as manual), a term dropped from a rule disappears cleanly (the
client stops sending it), and a keyword written by another path (e.g. an accepted
dismissal suggestion appends to ``title_exclude_keywords``) auto-surfaces as a
manual chip with no extra bookkeeping.

Two physical stores, one writer (``write_rules``). The file write (location ->
fit_criteria.md, the only step that can fail validation) goes first, then the
settings rows commit together — see ``write_rules`` for the failure contract.
"""

import json
import sqlite3

from jshq.scoring import criteria as criteria_mod

# The three raw arrays a rule can compile into.
_ARRAYS = ("title_keywords", "title_exclude_keywords", "location_allowlist")

# (target, verb) -> the raw array a rule emits into. (location, exclude) is
# absent on purpose: there is no location exclusion list, so the model rejects it
# before it ever reaches here.
_EMIT = {
    ("title", "include"): "title_keywords",
    ("title", "exclude"): "title_exclude_keywords",
    ("location", "include"): "location_allowlist",
}


def _normalize(target: str, term: str) -> str:
    """location_allowlist is stored lowercased (tier1.norm() lowercases both
    sides at match time anyway); titles keep their display casing since the
    ingestion filter matches case-insensitively (re.I)."""
    term = term.strip()
    return term.lower() if target == "location" else term


def _dedupe_ci(values: list[str]) -> list[str]:
    """First-occurrence-wins, case-insensitive; drops empties."""
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        key = v.lower()
        if key and key not in seen:
            seen.add(key)
            out.append(v)
    return out


def compile_rules(rules: list[dict]) -> dict[str, list[str]]:
    """Lower the rules to ``{array: [terms]}`` (rule-emitted only, deduped CI)."""
    out: dict[str, list[str]] = {arr: [] for arr in _ARRAYS}
    for rule in rules:
        arr = _EMIT.get((rule["target"], rule["verb"]))
        if arr is None:
            continue  # (location, exclude) — rejected by the model upstream
        for term in rule["terms"]:
            normalized = _normalize(rule["target"], term)
            if normalized:
                out[arr].append(normalized)
    return {arr: _dedupe_ci(vals) for arr, vals in out.items()}


def _merge(emitted: list[str], manual: list[str]) -> list[str]:
    """The live array: rule terms first, then manual extras not already emitted
    (rule wins on a case-insensitive collision), all deduped."""
    emitted = _dedupe_ci(emitted)
    seen = {v.lower() for v in emitted}
    extra = [v for v in _dedupe_ci(manual) if v.lower() not in seen]
    return emitted + extra


def _provenance(emitted: list[str], live: list[str]) -> list[dict]:
    """Tag each live entry rule|manual. Rule terms (source of truth) lead in rule
    order; everything else in the live array is a manual extra."""
    emitted = _dedupe_ci(emitted)
    keys = {v.lower() for v in emitted}
    manual_extra = [v for v in live if v.lower() not in keys]
    return [{"value": v, "source": "rule"} for v in emitted] + [
        {"value": v, "source": "manual"} for v in manual_extra
    ]


# --- settings JSON helpers (the table is a flexible k/v JSON store) ----------


def _get_json(db: sqlite3.Connection, key: str, default):
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if not row or row["value"] is None:
        return default
    try:
        return json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        return default


def _put_json(db: sqlite3.Connection, key: str, value) -> None:
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )


def _current_live(db: sqlite3.Connection) -> dict[str, list[str]]:
    """The raw arrays as ingestion/scoring read them right now."""
    return {
        "title_keywords": _get_json(db, "title_keywords", []),
        "title_exclude_keywords": _get_json(db, "title_exclude_keywords", []),
        "location_allowlist": criteria_mod.read_editable()[0].get(
            "location_allowlist", []
        ),
    }


def read_rules(db: sqlite3.Connection) -> dict:
    """GET payload: the authored rules + each compiled array with rule|manual
    provenance, derived from the rules and the current live arrays."""
    rules = _get_json(db, "inclusion_rules", [])
    emitted = compile_rules(rules)
    live = _current_live(db)
    compiled = {arr: _provenance(emitted[arr], live[arr]) for arr in _ARRAYS}
    return {"rules": rules, "compiled": compiled}


def write_rules(
    db: sqlite3.Connection, rules: list[dict], manual: dict[str, list[str]]
) -> dict:
    """Persist the rules and rewrite all three raw arrays to ``rules ∪ manual``.

    ``manual`` is the set of manual chips the client is showing; trusting it (vs.
    re-deriving) is what makes a rule-delete clean — the client simply stops
    sending the deleted rule's terms.

    File-first ordering: ``write_criteria`` re-validates the whole doc against a
    temp file and is the only step that can fail (CriteriaError -> 422), so it
    runs before any settings row changes — a bad payload leaves both stores
    untouched. The settings writes then commit together. The only residual
    inconsistency window is the process dying between the file replace and the
    commit; the settings writes are then rolled back, and the next successful PUT
    rewrites all three arrays from the same ``rules``, so it self-heals.
    """
    emitted = compile_rules(rules)
    live = {arr: _merge(emitted[arr], manual.get(arr, [])) for arr in _ARRAYS}

    # 1) File first (the only fallible step). Replace ONLY location_allowlist in
    #    a fresh dict so the criteria mtime cache isn't mutated if this raises.
    params, tier2 = criteria_mod.read_editable()
    params = {**params, "location_allowlist": live["location_allowlist"]}
    criteria_mod.write_criteria(params, tier2)

    # 2) Settings second, batched into one commit.
    _put_json(db, "title_keywords", live["title_keywords"])
    _put_json(db, "title_exclude_keywords", live["title_exclude_keywords"])
    _put_json(db, "inclusion_rules", rules)
    db.commit()

    return read_rules(db)
