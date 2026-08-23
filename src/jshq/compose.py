"""Compose drafts in the sender's own voice. Voice comes from
docs/voice_guide.md; AI-tell avoidance from docs/AI-TELLS.md. Who the sender is
comes from the criteria doc's persona block, never from code.

The Anthropic client is injected by the caller; this module never creates
one, so tests pass a fake and can never hit the live API. No structured
output: the draft text is the payload. No temperature override: default
sampling gives natural variation across regenerates.
"""

import json
import re
import sqlite3
from pathlib import Path

from jshq import aicfg, paths
from jshq.scoring.criteria import persona_display_name

VOICE_GUIDE_PATH = paths.DEFAULTS_DIR / "voice_guide.md"  # shipped default / fallback
AI_TELLS_PATH = paths.DEFAULTS_DIR / "AI-TELLS.md"


def voice_guide_path() -> Path:
    """The user's editable copy in DATA_DIR if present, else the shipped default.
    Resolved at call time so first-run seeding (which copies it into DATA_DIR) and
    a test's monkeypatched DATA_DIR are both followed. AI-TELLS.md stays read-only
    in the package — it is app craft knowledge, not the user's voice."""
    live = paths.DATA_DIR / "voice_guide.md"
    return live if live.exists() else VOICE_GUIDE_PATH


def save_voice_guide(text: str) -> None:
    """Write the user's voice guide to DATA_DIR atomically (temp + replace). It is
    prose the prompts carry verbatim, not machine config, so there is no structural
    validation here — the endpoint caps its size and that is all."""
    path = paths.DATA_DIR / "voice_guide.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)

# Model choice lives in aicfg (per-task selection, Providers Tier 1): callers
# resolve and pass it in; None falls back to the shipped default. The thinking
# workaround the old THINKING constant carried (Sonnet-tier models think by
# default and can spend the whole max_tokens budget on it) now rides
# aicfg.thinking_kwargs, keyed on the model actually used.
MAX_TOKENS = 2048

# Em dash (U+2014) / horizontal bar (U+2015) -> ", ". The one AI-tell hard rule
# safe to auto-fix on output: collapses only HORIZONTAL whitespace so paragraph
# breaks (\n\n) survive, and leaves en dashes (U+2013) in ranges untouched.
_EM_DASH_RE = re.compile(r"[ \t]*[—―][ \t]*")


def strip_em_dashes(text: str) -> str:
    """Deterministic backstop under the voice guide's em-dash ban."""
    return _EM_DASH_RE.sub(", ", text)
JD_CHAR_LIMIT = 6_000  # compose needs the gist, not scoring's full 12k budget
TIMELINE_LIMIT = 15
TIMELINE_LINE_LIMIT = 300

# Module-level constants, so they cannot interpolate a per-call persona name:
# these stay name-free and lean on "the sender" instead.
INTENT_BRIEFS = {
    "thank_you": (
        "a short thank-you note after an interview or meeting, referencing "
        "something specific from the recent history"
    ),
    "follow_up": (
        "a brief follow-up nudge on an application or conversation that has "
        "gone quiet; light touch, gives an easy way to respond"
    ),
    "linkedin_comment": (
        "a LinkedIn comment: conversational, adds a substantive point, "
        "never reads as networking-for-networking's-sake"
    ),
    "connection_note": (
        "a LinkedIn connection-request note, hard cap 300 characters, one "
        "specific reference to the conversation or shared context, no ask, "
        "clean close"
    ),
    "reconnect": (
        "a reconnect with a dormant relationship: specific to them first, "
        "the sender's news second, delivered plainly, no embedded ask"
    ),
    "outreach": (
        "a first-touch outreach message: why this person/company "
        "specifically, what the sender brings, one clear low-pressure ask"
    ),
    "application_answer": (
        "an answer to the application question above, written in first "
        "person; respect any stated word limit"
    ),
}


class ComposeError(Exception):
    """The model returned no usable draft text. Carries the call's `usages` so
    an empty draft's tokens are still billable."""

    def __init__(self, message, usages=None):
        super().__init__(message)
        self.usages = usages or []


def load_voice_guide(path: Path | None = None) -> str:
    if path is None:
        path = voice_guide_path()  # live DATA_DIR copy, else the shipped default
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""  # missing guide degrades to the base framing, never errors


def load_ai_tells(path: Path = AI_TELLS_PATH) -> str:
    """The full AI-tell rubric (for the refine pass). Missing file -> ''."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def ai_tells_prompt_block(path: Path = AI_TELLS_PATH) -> str:
    """The generation-time subset of the rubric: everything up to the Appendix
    (hard rules + patterns = the negative-constraint material). The Appendix
    LLM-judge prompt is for the refine pass, not generation."""
    return load_ai_tells(path).split("\n## Appendix", 1)[0].strip()


def tells_framing(ai_tells: str) -> str:
    """Wrap the rubric as hard negative constraints, shared by compose + tailor."""
    return (
        "Also avoid AI tells: the phrasing that makes writing read as machine-"
        "generated. Treat the rubric below as hard negative constraints. Keep "
        "real specifics, numbers, and voice; do not overcorrect into blandness.\n\n"
        "--- AI-TELL RUBRIC ---\n"
        f"{ai_tells.strip()}\n"
        "--- END AI-TELL RUBRIC ---"
    )


def build_system_prompt(voice_guide: str, ai_tells: str = "") -> str:
    # Read at call time, not import time: the criteria doc is user-editable and
    # mtime-cached, so an import-time read would freeze a stale name.
    name = persona_display_name()
    parts = [
        f"You draft job-search correspondence for one specific person, {name}. "
        f"You write AS {name}, in first person, in their voice."
    ]
    if voice_guide.strip():
        parts.append(
            "The voice guide below is the authority on tone, vocabulary, and "
            "format; follow it literally. Where it is silent or marked TODO, "
            "default to plain, direct, warm-but-brief professional writing "
            "with no filler.\n\n"
            "--- VOICE GUIDE ---\n"
            f"{voice_guide.strip()}\n"
            "--- END VOICE GUIDE ---"
        )
    else:
        parts.append(
            "No voice guide is available; write plain, direct, warm-but-brief "
            "professional prose with no filler."
        )
    if ai_tells.strip():
        parts.append(tells_framing(ai_tells))
    parts.append(
        "Output only the draft text itself: no preamble, no options, no "
        "commentary, no subject line unless the intent calls for one."
    )
    return "\n\n".join(parts)


def _salary_line(job: sqlite3.Row) -> str:
    if job["salary_stated"] and job["salary_max"] is not None:
        lo = job["salary_min"] if job["salary_min"] is not None else job["salary_max"]
        return f"${lo:,}–${job['salary_max']:,} (stated)"
    return "not stated"


def _activity_line(row: sqlite3.Row) -> str:
    """One bounded timeline line; JSON-content rows summarize to a phrase."""
    content = row["content"] or ""
    if row["type"] in ("dismissal", "applied", "compose"):
        try:
            data = json.loads(content or "{}")
        except json.JSONDecodeError:
            data = {}
        if row["type"] == "dismissal":
            content = f"dismissed: {data.get('reason', '?')}"
            if data.get("note"):
                content += f" ({data['note']})"
        elif row["type"] == "applied":
            content = "applied"
        else:
            draft = (data.get("draft") or "").replace("\n", " ")
            content = f"drafted {data.get('intent', '?')}: \"{draft[:200]}\""
    content = " ".join(content.split())
    if len(content) > TIMELINE_LINE_LIMIT:
        content = content[:TIMELINE_LINE_LIMIT] + "…"
    return f"- {row['date']} {row['type']}: {content}"


def _timeline(conn: sqlite3.Connection, entity_type: str, entity_id: int) -> str:
    rows = conn.execute(
        """SELECT date, type, content FROM activities
           WHERE entity_type = ? AND entity_id = ?
           ORDER BY date DESC, id DESC LIMIT ?""",
        (entity_type, entity_id, TIMELINE_LIMIT),
    ).fetchall()
    if not rows:
        return "(none logged)"
    return "\n".join(_activity_line(row) for row in rows)


def _job_block(job: sqlite3.Row) -> str:
    jd = job["description_text"] or "(no description available)"
    if len(jd) > JD_CHAR_LIMIT:
        jd = jd[:JD_CHAR_LIMIT] + "\n[truncated]"
    return (
        f"Job: {job['title']}\n"
        f"Company: {job['company_name']}\n"
        f"Location: {job['location'] or 'unknown'} ({job['remote_type'] or 'unknown'})\n"
        f"Level band: {job['level_band'] or 'unknown'}\n"
        f"Salary: {_salary_line(job)}\n"
        f"Status: {job['status']}\n\n"
        f"Job description:\n{jd}"
    )


def _contact_block(contact: sqlite3.Row) -> str:
    return (
        f"Contact: {contact['name']}\n"
        f"Role: {contact['role'] or 'unknown'}\n"
        f"Company: {contact['company_name'] or 'none on record'}\n"
        f"How we know each other: {contact['source'] or 'unknown'}\n"
        f"Last contact: {contact['last_contact_date'] or 'unknown'}\n"
        f"Relationship notes: {contact['relationship_notes'] or '(none)'}"
    )


def build_entity_context(
    conn: sqlite3.Connection, entity_type: str, entity_id: int
) -> str | None:
    """Entity block + bounded activity timeline. None = entity not found."""
    if entity_type == "job":
        row = conn.execute(
            """SELECT jobs.*, companies.name AS company_name
               FROM jobs JOIN companies ON companies.id = jobs.company_id
               WHERE jobs.id = ?""",
            (entity_id,),
        ).fetchone()
        block = _job_block(row) if row else None
    else:
        row = conn.execute(
            """SELECT contacts.*, companies.name AS company_name
               FROM contacts LEFT JOIN companies ON companies.id = contacts.company_id
               WHERE contacts.id = ?""",
            (entity_id,),
        ).fetchone()
        block = _contact_block(row) if row else None
    if block is None:
        return None
    timeline = _timeline(conn, entity_type, entity_id)
    return f"{block}\n\nRecent history (newest first):\n{timeline}"


def build_user_message(
    intent: str, context: str, instructions: str | None, question: str | None
) -> str:
    parts = [f"Intent: {intent} ({INTENT_BRIEFS[intent]})"]
    if question:
        parts.append(f"Application question:\n{question.strip()}")
    parts.append(f"--- CONTEXT ---\n{context}\n--- END CONTEXT ---")
    if instructions and instructions.strip():
        parts.append(
            f"Additional instructions from {persona_display_name()}: "
            f"{instructions.strip()}"
        )
    parts.append("Write the draft now.")
    return "\n\n".join(parts)


async def generate(client, system: str, user: str, model: str | None = None) -> tuple[str, list]:
    """One model call. The SDK already retries 429/5xx; nothing to parse, so
    no application-level retry. Raises ComposeError on an empty draft. Returns
    (draft, [usage]) so the endpoint can record spend."""
    model = model or aicfg.DEFAULTS["compose"]
    resp = await client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        **aicfg.thinking_kwargs(model),
        # cache_control: free win if the voice guide ever clears Sonnet's
        # cache minimum; harmless below it.
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    if not text:
        raise ComposeError("model returned no text", [getattr(resp, "usage", None)])
    return strip_em_dashes(text), [getattr(resp, "usage", None)]
