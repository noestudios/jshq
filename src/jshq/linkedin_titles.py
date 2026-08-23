"""AI-suggested LinkedIn role-check titles.

The wizard derives a deterministic default title list (band × field templates in
welcome.js), but it cannot reach ADJACENT disciplines — a product designer also
networks with UX researchers and research managers. This one on-demand Haiku
call proposes those neighbours, reviewed as accept/ignore cards in Settings →
Sourcing before anything lands in the ``linkedin_title_defaults`` setting.

The Anthropic client is injected by the caller — this module never creates one,
so tests pass a fake and can never hit the live API (mirrors learned/compose).
"""

import json

from jshq import aicfg

MAX_TOKENS = 768

SCHEMA = {
    "type": "object",
    "properties": {
        "titles": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["title", "why"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["titles"],
    "additionalProperties": False,
}

# The review cards cap what one call may propose; the model is told the same
# number so the two never drift apart silently.
MAX_SUGGESTIONS = 8


class TitleSuggestError(Exception):
    """The model returned unusable output after a retry. Carries per-attempt
    `usages` so a failed run's tokens are still billable."""

    def __init__(self, message, usages=None):
        super().__init__(message)
        self.usages = usages or []


def build_prompt(criteria, existing: list[str]) -> str:
    """System prompt from the criteria doc's own vocabulary: the field
    (domain_label + discipline glosses) and the target seniority bands. The
    existing list is the dedupe contract — the model must go BEYOND it."""
    bands = criteria.params.get("target_title_bands") or []
    band_labels = [criteria.level_band_labels.get(b, b) for b in bands]
    disciplines = [
        gloss
        for slug, gloss in (criteria.taxonomy.get("disciplines") or {}).items()
        if slug not in ("other", "unclear")
    ]
    parts = [
        "You help a job seeker build a LinkedIn networking list: job titles to "
        "search for at a target company to find people worth talking to about "
        f"roles in {criteria.domain_label}. Suggest titles of ADJACENT and "
        "SURROUNDING roles — the peers, partner disciplines, and leaders such a "
        "person would network with — not just restatements of their own target "
        "title. (Example of the idea: a product designer also networks with UX "
        "researchers and research managers.)",
        "Titles are used as quoted LinkedIn people-search keywords, so each must "
        "be a short phrase real titles contain. No seniority-spanning mashups, "
        "no slashes, no explanations inside the title itself.",
    ]
    if disciplines:
        parts.append("Their discipline vocabulary: " + ", ".join(disciplines) + ".")
    if band_labels:
        parts.append(
            "Seniority they target (weight suggestions toward these levels and "
            "the people who hire or partner at them): " + ", ".join(band_labels) + "."
        )
    if existing:
        parts.append(
            "Titles already on their list — do NOT repeat or trivially rephrase "
            "these:\n" + "\n".join(f"- {t}" for t in existing)
        )
    parts.append(
        f"Return JSON with `titles`: up to {MAX_SUGGESTIONS} entries, each with\n"
        "- title: the search phrase\n"
        "- why: a few words on who this finds and why they're worth talking to"
    )
    return "\n\n".join(parts)


def _parse(resp, existing: list[str]) -> list[dict]:
    text = next(b.text for b in resp.content if b.type == "text")
    data = json.loads(text)
    seen = {t.strip().lower() for t in existing}
    out = []
    for entry in data.get("titles") or []:
        title = (entry.get("title") or "").strip()
        key = title.lower()
        if not title or key in seen:
            continue
        seen.add(key)
        out.append({"title": title, "why": (entry.get("why") or "").strip()})
    if not out:
        raise ValueError("no usable titles")
    return out[:MAX_SUGGESTIONS]


async def propose(client, system: str, existing: list[str], model: str | None = None) -> tuple[list[dict], list]:
    """One model call (plus one retry on unusable output). Returns
    (suggestions, per-call usages). Raises TitleSuggestError."""
    model = model or aicfg.DEFAULTS["linkedin_titles"]
    last_exc: Exception | None = None
    usages: list = []
    for _ in range(2):
        resp = await client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            **aicfg.thinking_kwargs(model),
            **aicfg.temperature_kwargs(model, 0.0),
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": "Suggest the titles."}],
            output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        )
        usages.append(getattr(resp, "usage", None))
        try:
            return _parse(resp, existing), usages
        except (json.JSONDecodeError, KeyError, ValueError, TypeError, StopIteration) as exc:
            last_exc = exc
    raise TitleSuggestError(f"unusable model output after retry: {last_exc}", usages) from last_exc
