"""Resume + cover letter tailoring (Phase 7e).

One model call, two outputs: a structured resume change plan addressed by
content.json node ids, and a cover letter draft — shared JD analysis so the
letter reinforces exactly what the resume tweaks emphasize. The agent edits
content, never layout: only paragraph sections and bullets are editable;
everything else (skills tables, role titles/dates) is read-only context.

The Anthropic client is injected by the caller, same as compose. Output is a
strict-JSON contract parsed from the text response; one corrective retry on
a parse failure, then the caller gets a TailorError. The model never sends
the old text — the server fills `old` from content.json, so the displayed
diff can't misquote the resume.

Chat refinement (Phase 7f) reuses the same machinery: a turn replays the
stored thread compactly (user texts + assistant replies only) and injects
the CURRENT plan + letter into the new user turn, so manual PATCH edits
between turns are always reflected and the model's old deltas never need
replaying. A turn may revise, add, or remove planned changes and/or rewrite
the letter; merge_chat_changes applies the same hardening as generate.
"""

import copy
import json
import sqlite3

from jshq import compose
from jshq.resume import render
from jshq.scoring.criteria import persona_display_name

MODEL = compose.MODEL
MAX_TOKENS = 8192
CHAT_MAX_TOKENS = 4096  # a turn returns a short reply + deltas, not a full plan
JD_CHAR_LIMIT = 12_000  # tailoring reads the full JD, not compose's 6k gist
MAX_CHANGES = 20


class TailorError(Exception):
    """The model returned no usable tailoring, or the plan no longer applies.
    Carries the per-attempt `usages` so a caller can still bill the tokens a
    failed run spent (parse/validation errors have none; the retry loop passes
    what it accumulated)."""

    def __init__(self, message, usages=None):
        super().__init__(message)
        self.usages = usages or []


# ---------------------------------------------------------------- content

def _editable_node_refs(content: dict) -> dict[str, dict]:
    """id → the dict whose 'text' is editable: paragraph sections and every
    bullet (top-level and per-role). Role titles/dates, columns items, and
    keyvalue rows stay read-only (no per-item ids; layout-sensitive)."""
    refs: dict[str, dict] = {}
    for section in content["sections"]:
        if section["type"] == "paragraph":
            refs[section["id"]] = section
        for bullet in section.get("bullets", []):
            refs[bullet["id"]] = bullet
        for role in section.get("roles", []):
            for bullet in role.get("bullets", []):
                refs[bullet["id"]] = bullet
    return refs


def get_editable_nodes(content: dict) -> dict[str, str]:
    """id → current text, in document order."""
    return {node_id: node["text"] for node_id, node in _editable_node_refs(content).items()}


# ---------------------------------------------------------------- prompts

def _voice_block(voice_guide: str) -> str:
    if voice_guide.strip():
        return (
            "The voice guide below governs BOTH the cover letter AND every "
            "rewritten resume line. Follow it literally, including its formatting "
            "rules (for example its ban on em dashes). Where it is silent or "
            "marked TODO, default to plain, direct, warm-but-brief professional "
            "writing with no filler.\n\n"
            "--- VOICE GUIDE ---\n"
            f"{voice_guide.strip()}\n"
            "--- END VOICE GUIDE ---"
        )
    return (
        "No voice guide is available; write plain, direct, warm-but-brief "
        "professional prose with no filler."
    )


def _change_rules(name: str) -> str:
    """A function rather than a constant only because its last bullet names the
    person; the persona comes from the criteria doc at call time."""
    return (
        "Resume change rules:\n"
        "- You may only rewrite lines whose id appears in the EDITABLE IDS "
        "list. Lines marked (read-only) are context; never propose changes "
        "to them.\n"
        "- At most one change per id; propose at most ~10 high-impact "
        "changes. Fewer, sharper changes beat a blanket rewrite.\n"
        "- LENGTH IS A HARD CONSTRAINT: each rewrite must be NO LONGER than the "
        "line it replaces (same length or shorter, never longer). Rephrase or trim "
        "to fit; do not expand or add detail. The resume must stay two pages, so a "
        "rewrite that adds words elsewhere has to cut words to compensate.\n"
        "- Never add, remove, merge, or reorder lines.\n"
        "- Preserve the inline markup conventions: **bold**, *italic*, "
        "[text](https://url).\n"
        f"- Be truthful: re-emphasize and re-word {name}'s real experience for "
        "this job; never invent experience, metrics, or skills."
    )

_LETTER_RULES = (
    "Cover letter rules: 250–350 words, plain text with a greeting and "
    "sign-off, no address or date header (the template adds those). It "
    "must reinforce the same strengths the resume changes emphasize."
)

# Stated explicitly because _change_rules() is otherwise silent on voice, and the
# most common leak is em dashes. Applies to rewrites AND the letter.
_FORMATTING_RULE = (
    "Formatting (applies to every rewritten line AND the cover letter): no em "
    "dashes. Use periods, commas, semicolons, or parentheses instead. Avoid the "
    "banned vocabulary and AI-cliché phrasing called out in the voice guide."
)


def build_system_prompt(voice_guide: str, ai_tells: str = "") -> str:
    name = persona_display_name()
    parts = [
        f"You tailor {name}'s resume for one specific job and draft their "
        f"cover letter. You write AS {name}, in first person where the format "
        "calls for it. You edit content only, never structure, never layout.",
        _voice_block(voice_guide),
        *([compose.tells_framing(ai_tells)] if ai_tells.strip() else []),
        _change_rules(name),
        _LETTER_RULES,
        _FORMATTING_RULE,
        "Reply with ONLY a raw JSON object (no code fences, no commentary, "
        "no text before or after it), in exactly this shape:\n"
        '{"analysis": "<your shared read of the JD: what they want, which of '
        f"{name}'s strengths to lead with>\", "
        '"changes": [{"id": "<editable id>", "new": "<rewritten line>", '
        '"rationale": "<why, one sentence>"}], '
        '"cover_letter": "<the full letter>"}',
    ]
    return "\n\n".join(parts)


def build_chat_system_prompt(voice_guide: str, ai_tells: str = "") -> str:
    name = persona_display_name()
    parts = [
        f"You are refining a pending tailoring of {name}'s resume and "
        f"cover letter, in conversation with {name}. An earlier run produced "
        f"the change plan and letter you'll be shown; each turn {name} asks "
        "for adjustments or just asks a question. You may revise planned "
        "changes, add new changes on other editable lines, remove planned "
        "changes, and/or rewrite the cover letter, or simply answer them. "
        f"You write AS {name}, in first person where the format calls for it. "
        "You edit content only, never structure, never layout.",
        _voice_block(voice_guide),
        *([compose.tells_framing(ai_tells)] if ai_tells.strip() else []),
        _change_rules(name),
        _LETTER_RULES,
        _FORMATTING_RULE,
        "Reply with ONLY a raw JSON object (no code fences, no commentary, "
        "no text before or after it), in exactly this shape:\n"
        f'{{"reply": "<your reply to {name}, 1-3 sentences>", '
        '"changes": [{"id": "<editable id>", "new": "<rewritten line>", '
        '"rationale": "<why, one sentence>"}], '
        '"remove": ["<id of a planned change to drop>"], '
        '"cover_letter": "<the full replacement letter, or null to leave it '
        'unchanged>"}\n'
        'Leave "changes" and "remove" empty and "cover_letter" null when the '
        'turn needs no edits. A "changes" entry whose id is already in the '
        'plan replaces that planned rewrite. "cover_letter", when present, '
        "is always the complete letter, never a fragment.",
    ]
    return "\n\n".join(parts)


def _job_block(job: sqlite3.Row) -> str:
    """Like compose._job_block but with tailoring's full JD budget."""
    jd = job["description_text"] or "(no description available)"
    if len(jd) > JD_CHAR_LIMIT:
        jd = jd[:JD_CHAR_LIMIT] + "\n[truncated]"
    return (
        f"Job: {job['title']}\n"
        f"Company: {job['company_name']}\n"
        f"Location: {job['location'] or 'unknown'} ({job['remote_type'] or 'unknown'})\n"
        f"Level band: {job['level_band'] or 'unknown'}\n\n"
        f"Job description:\n{jd}"
    )


def _fit_block(job: sqlite3.Row) -> str:
    try:
        flags = ", ".join(json.loads(job["near_miss_flags"] or "[]")) or "none"
    except json.JSONDecodeError:
        flags = job["near_miss_flags"] or "none"
    score = "not scored" if job["fit_score"] is None else f"{job['fit_score']}/100"
    return (
        f"Fit score: {score}\n"
        f"Fit quadrant: {job['fit_quadrant'] or 'unknown'}\n"
        f"Near-miss flags: {flags}\n"
        f"Scoring notes: {job['scoring_notes'] or '(none)'}"
    )


def build_resume_block(content: dict) -> str:
    lines = [f"Name: {content['name']}", f"Headline: {content['title']}"]
    for section in content["sections"]:
        lines.append(f"\n## {section['heading']}")
        stype = section["type"]
        if stype == "paragraph":
            lines.append(f"[{section['id']}] {section['text']}")
        elif stype == "columns":
            lines.append("(read-only) " + " | ".join(section["items"]))
        elif stype == "keyvalue":
            lines.extend(f"(read-only) {r['label']}: {r['text']}" for r in section["rows"])
        elif stype == "bullets":
            lines.extend(f"[{b['id']}] {b['text']}" for b in section["bullets"])
        else:  # roles
            for role in section["roles"]:
                dates = f" ({role['dates']})" if role.get("dates") else ""
                lines.append(f"(read-only) {role['title']}{dates}")
                lines.extend(f"[{b['id']}] {b['text']}" for b in role.get("bullets", []))
    lines.append(f"\nEDITABLE IDS: {', '.join(get_editable_nodes(content))}")
    return "\n".join(lines)


def build_user_message(job: sqlite3.Row, content: dict, instructions: str | None) -> str:
    parts = [
        "Tailor the resume and draft the cover letter for this job.",
        f"--- JOB ---\n{_job_block(job)}\n--- END JOB ---",
        f"--- FIT ASSESSMENT (from the scoring pipeline) ---\n{_fit_block(job)}\n"
        "--- END FIT ASSESSMENT ---",
        f"--- RESUME (current content) ---\n{build_resume_block(content)}\n--- END RESUME ---",
    ]
    if instructions and instructions.strip():
        parts.append(
            f"Additional instructions from {persona_display_name()}: {instructions.strip()}"
        )
    parts.append("Reply with the JSON object now.")
    return "\n\n".join(parts)


def _plan_block(plan: list[dict]) -> str:
    """One line per planned change, approval state included so the model can
    treat approved lines as settled unless the user asks otherwise."""
    if not plan:
        return "(no resume changes planned)"
    lines = []
    for change in plan:
        mark = "approved" if change.get("approved") else "not yet approved"
        rationale = f" — {change['rationale']}" if change.get("rationale") else ""
        lines.append(f"[{change['id']}] ({mark}) -> {change['new']}{rationale}")
    return "\n".join(lines)


def build_chat_user_message(
    job: sqlite3.Row, plan: list[dict], cover_letter: str, content: dict, message: str
) -> str:
    """Built fresh from CURRENT DB state every turn — the user may have hand-
    edited the plan or letter (PATCH) since the last one, and this snapshot
    is also why prior assistant deltas never need replaying."""
    name = persona_display_name()
    return "\n\n".join([
        f"Refine the pending tailoring below according to {name}'s message.",
        f"--- JOB ---\n{_job_block(job)}\n--- END JOB ---",
        f"--- FIT ASSESSMENT (from the scoring pipeline) ---\n{_fit_block(job)}\n"
        "--- END FIT ASSESSMENT ---",
        f"--- RESUME (current content) ---\n{build_resume_block(content)}\n--- END RESUME ---",
        f"--- CURRENT CHANGE PLAN ---\n{_plan_block(plan)}\n--- END CURRENT CHANGE PLAN ---",
        f"--- CURRENT COVER LETTER ---\n{cover_letter}\n--- END CURRENT COVER LETTER ---",
        # "Message from X" rather than "X says", so the line still reads as a
        # heading when the doc names nobody and the name is "the candidate".
        f"Message from {name}: {message.strip()}",
        "Reply with the JSON object now.",
    ])


# ---------------------------------------------------------------- output

def _json_object(text: str) -> dict:
    """Brace-slice the JSON object out; tolerant of fences/prose around it."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise TailorError("no JSON object in model output")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise TailorError(f"model output is not valid JSON: {exc}")
    if not isinstance(data, dict):
        raise TailorError("model output is not a JSON object")
    return data


def parse_output(text: str) -> dict:
    """Strict-JSON generate contract."""
    data = _json_object(text)
    if not isinstance(data.get("analysis"), str):
        raise TailorError("missing or non-string 'analysis'")
    if not isinstance(data.get("changes"), list):
        raise TailorError("missing or non-list 'changes'")
    if not isinstance(data.get("cover_letter"), str) or not data["cover_letter"].strip():
        raise TailorError("missing or empty 'cover_letter'")
    data["cover_letter"] = compose.strip_em_dashes(data["cover_letter"])
    return data


def parse_chat_output(text: str) -> dict:
    """Strict-JSON chat-turn contract. 'changes'/'remove' default to empty
    and a missing or blank 'cover_letter' becomes None — a discussion-only
    turn must never blank the letter by accident."""
    data = _json_object(text)
    if not isinstance(data.get("reply"), str) or not data["reply"].strip():
        raise TailorError("missing or empty 'reply'")
    changes = data.get("changes") or []
    if not isinstance(changes, list):
        raise TailorError("non-list 'changes'")
    remove = data.get("remove") or []
    if not isinstance(remove, list):
        raise TailorError("non-list 'remove'")
    letter = data.get("cover_letter")
    if letter is not None and not isinstance(letter, str):
        raise TailorError("non-string 'cover_letter'")
    if letter is not None:
        letter = compose.strip_em_dashes(letter.strip()) or None
    return {
        "reply": data["reply"].strip(),
        "changes": changes,
        "remove": remove,
        "cover_letter": letter,
    }


def normalize_changes(raw_changes: list, content: dict) -> tuple[list[dict], list[str]]:
    """Server-side hardening of the model's change list. Bad entries are
    dropped with a human-readable warning, never a hard failure; `old` is
    always the actual current text, whatever the model thought it was."""
    editable = get_editable_nodes(content)
    plan: list[dict] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for change in raw_changes:
        if not isinstance(change, dict):
            warnings.append("dropped a non-object change entry")
            continue
        node_id = str(change.get("id") or "")
        new = change.get("new")
        if node_id not in editable:
            warnings.append(f"dropped change for unknown or read-only id '{node_id}'")
            continue
        if node_id in seen:
            warnings.append(f"dropped duplicate change for '{node_id}'")
            continue
        if not isinstance(new, str) or not new.strip():
            warnings.append(f"dropped empty rewrite for '{node_id}'")
            continue
        new = compose.strip_em_dashes(new.strip())
        if new == editable[node_id]:
            warnings.append(f"dropped no-op change for '{node_id}'")
            continue
        seen.add(node_id)
        plan.append({
            "id": node_id,
            "old": editable[node_id],
            "new": new,
            "rationale": str(change.get("rationale") or "").strip(),
            "approved": False,
        })
    if len(plan) > MAX_CHANGES:
        warnings.append(f"kept the first {MAX_CHANGES} of {len(plan)} changes")
        plan = plan[:MAX_CHANGES]
    return plan, warnings


def merge_chat_changes(
    plan: list[dict], content: dict, parsed: dict
) -> tuple[list[dict], list[str]]:
    """Merge a chat turn's deltas into the pending plan with normalize_changes'
    hardening. Removes run first, so an id named in both 'remove' and 'changes'
    is a deterministic re-add (fresh row, unapproved). A revise keeps the row's
    `old` (preserving apply's drift guard) and its approved flag — the user
    asked for the edit and the diff row repaints; additions arrive unapproved.
    Returns a new list; the input plan rows are never mutated."""
    editable = get_editable_nodes(content)
    warnings: list[str] = []

    merged = list(plan)
    for node_id in parsed.get("remove") or []:
        node_id = str(node_id)
        kept = [c for c in merged if c["id"] != node_id]
        if len(kept) == len(merged):
            warnings.append(f"remove: no planned change for '{node_id}'")
        merged = kept

    seen: set[str] = set()
    for change in parsed.get("changes") or []:
        if not isinstance(change, dict):
            warnings.append("dropped a non-object change entry")
            continue
        node_id = str(change.get("id") or "")
        new = change.get("new")
        if node_id not in editable:
            warnings.append(f"dropped change for unknown or read-only id '{node_id}'")
            continue
        if node_id in seen:
            warnings.append(f"dropped duplicate change for '{node_id}'")
            continue
        if not isinstance(new, str) or not new.strip():
            warnings.append(f"dropped empty rewrite for '{node_id}'")
            continue
        new = compose.strip_em_dashes(new.strip())
        seen.add(node_id)
        rationale = str(change.get("rationale") or "").strip()
        index = next((i for i, c in enumerate(merged) if c["id"] == node_id), None)
        if new == editable[node_id]:
            # Rewritten back to the resume's current text — no longer a change.
            if index is not None:
                merged.pop(index)
                warnings.append(f"'{node_id}' reverted to its original text — change removed")
            else:
                warnings.append(f"dropped no-op change for '{node_id}'")
            continue
        if index is not None:
            row = dict(merged[index])
            row["new"] = new
            if rationale:
                row["rationale"] = rationale
            merged[index] = row
        elif len(merged) >= MAX_CHANGES:
            warnings.append(f"plan is at the {MAX_CHANGES}-change cap — dropped addition '{node_id}'")
        else:
            merged.append({
                "id": node_id,
                "old": editable[node_id],
                "new": new,
                "rationale": rationale,
                "approved": False,
            })
    return merged, warnings


def apply_changes(content: dict, plan: list[dict]) -> dict:
    """Approved changes applied to a deep copy — the master content.json is
    never written. Raises TailorError if an approved line's text on disk no
    longer matches the plan's `old` (the resume drifted since generation)."""
    patched = copy.deepcopy(content)
    refs = _editable_node_refs(patched)
    for change in plan:
        if not change.get("approved"):
            continue
        node = refs.get(change["id"])
        if node is None or node.get("text") != change["old"]:
            raise TailorError(f"resume content changed since this plan was made ('{change['id']}')")
        node["text"] = change["new"]
    render.validate_content(patched)
    return patched


# ---------------------------------------------------------------- model call

async def _call_with_retry(
    client, system: str, messages: list[dict], parser, max_tokens: int
) -> tuple[dict, list]:
    """One Sonnet call (the SDK retries 429/5xx itself) plus one corrective
    retry if the reply doesn't parse — the parse error goes back to the model
    as a follow-up turn. Returns (parsed contract dict, per-call usages)."""
    system_blocks = [
        {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
    ]
    last_error: TailorError | None = None
    usages: list = []
    for _ in range(2):
        resp = await client.messages.create(
            model=MODEL, max_tokens=max_tokens, thinking=compose.THINKING,
            system=system_blocks, messages=messages
        )
        usages.append(getattr(resp, "usage", None))
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        try:
            return parser(text), usages
        except TailorError as exc:
            last_error = exc
            messages = messages + [
                {"role": "assistant", "content": text or "(empty)"},
                {"role": "user", "content": (
                    f"Your previous reply could not be used: {exc}. Reply again "
                    "with ONLY the raw JSON object in the required shape — no "
                    "fences, no commentary."
                )},
            ]
    raise TailorError(f"unusable output after retry: {last_error}", usages)


async def generate(client, system: str, user: str) -> tuple[dict, list]:
    """The initial tailoring run: one user turn in, (contract, usages) out."""
    return await _call_with_retry(
        client, system, [{"role": "user", "content": user}], parse_output, MAX_TOKENS
    )


async def chat(client, system: str, messages: list[dict]) -> tuple[dict, list]:
    """A refinement turn: the replayed thread in, (chat contract, usages) out."""
    return await _call_with_retry(
        client, system, messages, parse_chat_output, CHAT_MAX_TOKENS
    )
