"""Semantic JD / role-mismatch learned rules (Phase 7i).

An on-demand, *cached* Claude call reads one job description and proposes a
single human-readable rule that would down-rank roles like it. Accepted rules
act at the **scoring layer** — they are injected into the Haiku scoring prompt
(``haiku.build_system_prompt``) as soft negative signals, the same way the
dismissal digest already feeds the scorer. They are deliberately NOT title
keywords and never touch ingestion (that is the inclusion-rules compiler, a
separate mechanism — see ``rules.py``).

Two settings keys hold the state (k/v JSON, lazy-initialised, not seeded, not in
EDITABLE_SETTINGS — mirrors ``inclusion_rules``):

- ``scoring_rules``           — accepted rules: ``[{id, text, source, job_id, created_at}]``
- ``scoring_rule_proposals``  — the pending review queue (one entry per origin
  job, which doubles as the per-job cache for the on-demand call).

The Anthropic client is injected by the caller — this module never creates one,
so tests pass a fake and can never hit the live API (mirrors haiku/compose).
"""

import json

from jshq import aicfg
from . import haiku
from .criteria import Criteria
from .rules import _get_json, _put_json

MAX_TOKENS = 1024

SCHEMA = {
    "type": "object",
    "properties": {
        "rule_text": {"type": "string"},
        "rationale": {"type": "string"},
    },
    "required": ["rule_text", "rationale"],
    "additionalProperties": False,
}


class LearnedRuleError(Exception):
    """The model returned unusable output after a retry. Carries per-attempt
    `usages` so a failed run's tokens are still billable."""

    def __init__(self, message, usages=None):
        super().__init__(message)
        self.usages = usages or []


def build_proposal_prompt(
    criteria: Criteria, digest: str, existing_rules: list[str]
) -> str:
    parts = [
        "You help refine the rubric a job-scoring tool uses to rank "
        f"{criteria.domain_label} postings for one specific person, "
        f"{criteria.display_name}. Read their criteria, their recent dismissals "
        "(demonstrated taste), and the job posting in the user message, then "
        "propose ONE rule that would cause roles like this one to be "
        "down-ranked for ROLE MISMATCH — the title may look right but the "
        "actual role is a poor fit (wrong discipline, wrong focus, "
        "hands-on-vs-leadership mismatch, and so on).",
        "The rule acts at the SCORING layer — it is a soft negative signal fed "
        "to the scorer, NOT a title keyword and NOT an ingestion filter. Phrase "
        "it as a description-level pattern, generalisable beyond this one "
        "posting (e.g. \"Down-rank roles that are primarily hands-on execution "
        f"rather than {criteria.domain_label}.\").",
        criteria.prose.strip(),
    ]
    if digest:
        parts.append(digest)
    if existing_rules:
        parts.append(
            "Existing learned rules — do NOT propose one that duplicates these:\n"
            + "\n".join(f"- {t}" for t in existing_rules)
        )
    parts.append(
        "Return JSON with:\n"
        "- rule_text: one imperative sentence stating the rule\n"
        "- rationale: 1-2 sentences naming what in this posting triggered it"
    )
    return "\n\n---\n\n".join(parts)


def build_user_message(job) -> str:
    """The same JD block the scorer sees (title/company/location/level/salary +
    truncated description). Reused so JD handling never drifts between the two."""
    return haiku.build_user_message(job)


def _parse(resp) -> dict:
    text = next(b.text for b in resp.content if b.type == "text")
    data = json.loads(text)
    rule_text = (data.get("rule_text") or "").strip()
    if not rule_text:
        raise ValueError("empty rule_text")
    return {"rule_text": rule_text, "rationale": (data.get("rationale") or "").strip()}


async def propose_rule(client, system: str, user: str, model: str | None = None) -> tuple[dict, list]:
    """One model call (plus one retry on unusable output). Returns (proposal,
    per-call usages). Raises LearnedRuleError."""
    model = model or aicfg.DEFAULTS["learned"]
    last_exc: Exception | None = None
    usages: list = []
    for _ in range(2):
        resp = await client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            **aicfg.thinking_kwargs(model),
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        )
        usages.append(getattr(resp, "usage", None))
        try:
            return _parse(resp), usages
        except (json.JSONDecodeError, KeyError, ValueError, TypeError, StopIteration) as exc:
            last_exc = exc
    raise LearnedRuleError(f"unusable model output after retry: {last_exc}", usages) from last_exc


# --- settings JSON stores (no commit; the endpoint owns the transaction) -----


def read_scoring_rules(db) -> list[dict]:
    return _get_json(db, "scoring_rules", [])


def write_scoring_rules(db, rules: list[dict]) -> None:
    _put_json(db, "scoring_rules", rules)


def read_proposals(db) -> list[dict]:
    return _get_json(db, "scoring_rule_proposals", [])


def write_proposals(db, proposals: list[dict]) -> None:
    _put_json(db, "scoring_rule_proposals", proposals)
