"""Opt-in "remove AI tells" pass over drafted copy (a compose draft or a cover
letter), scored against docs/AI-TELLS.md. One Sonnet call, structured output, in
the sender's own voice (named by the criteria doc's persona block). It rewrites
toward human-sounding prose and NEVER invents or alters facts, numbers, or names.

The Anthropic client is injected by the caller (same as compose/tailor), so
tests pass a fake and never hit the live API.
"""

import json

from jshq import compose
from jshq.scoring.criteria import persona_display_name

MODEL = compose.MODEL
MAX_TOKENS = 4096

SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer"},
        "tells_fixed": {"type": "array", "items": {"type": "string"}},
        "refined_text": {"type": "string"},
    },
    "required": ["score", "tells_fixed", "refined_text"],
    "additionalProperties": False,
}


class RefineError(Exception):
    """The model returned no usable refined text. Carries per-attempt `usages`
    so a failed run's tokens are still billable."""

    def __init__(self, message, usages=None):
        super().__init__(message)
        self.usages = usages or []


def build_system_prompt(voice_guide: str, ai_tells: str) -> str:
    # Call time, not import time: the criteria doc is user-editable.
    name = persona_display_name()
    parts = [
        f"You are an editor removing 'AI tells' from {name}'s own first-person "
        "professional copy (a cover letter or an outreach message). Rewrite it so "
        "it reads as written by a person, using the rubric below."
    ]
    if voice_guide.strip():
        parts.append(
            f"Stay in {name}'s voice per the voice guide below.\n\n"
            "--- VOICE GUIDE ---\n"
            f"{voice_guide.strip()}\n"
            "--- END VOICE GUIDE ---"
        )
    if ai_tells.strip():
        parts.append(
            "--- AI-TELL RUBRIC ---\n"
            f"{ai_tells.strip()}\n"
            "--- END AI-TELL RUBRIC ---"
        )
    parts.append(
        "Hard constraints: NEVER change facts, numbers, names, dates, or claims, "
        "and add nothing that isn't in the original. When a flagged line can't be "
        "de-telled without inventing a fact, CUT it rather than reword. Fixed lines "
        "should be MORE concrete, never blander; keep the length close to the "
        "original and don't strip all rhythm or personality (see 'Don't over-"
        "correct'). Never use em dashes."
    )
    parts.append(
        "Return the fully rewritten copy as `refined_text`; an integer `score` 0-10 "
        "for how human the RESULT reads (10 = no tells left); and `tells_fixed`, the "
        "rubric flag names you addressed (e.g. 'em-dash', 'cliche-metaphor'), or [] "
        "if the copy was already clean and you changed nothing."
    )
    return "\n\n".join(parts)


def _parse(resp) -> dict:
    text = next(b.text for b in resp.content if b.type == "text")
    data = json.loads(text)
    refined = (data.get("refined_text") or "").strip()
    if not refined:
        raise ValueError("empty refined_text")
    score = data.get("score")
    tells = [str(t).strip() for t in (data.get("tells_fixed") or []) if str(t).strip()]
    return {
        "score": int(score) if isinstance(score, (int, float)) else None,
        "tells_fixed": tells,
        # Belt-and-suspenders: the rubric bans em dashes, but sweep anyway.
        "refined_text": compose.strip_em_dashes(refined),
    }


async def refine(client, text: str) -> tuple[dict, list]:
    """One Sonnet call (plus one retry on unusable output). Returns
    ({score, tells_fixed, refined_text}, per-call usages). Raises RefineError."""
    system = build_system_prompt(compose.load_voice_guide(), compose.load_ai_tells())
    user = f"--- COPY TO REFINE ---\n{text.strip()}\n--- END COPY ---"
    last_exc: Exception | None = None
    usages: list = []
    for _ in range(2):
        resp = await client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            thinking=compose.THINKING,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        )
        usages.append(getattr(resp, "usage", None))
        try:
            return _parse(resp), usages
        except (json.JSONDecodeError, KeyError, ValueError, TypeError, StopIteration) as exc:
            last_exc = exc
    raise RefineError(f"unusable model output after retry: {last_exc}", usages) from last_exc
