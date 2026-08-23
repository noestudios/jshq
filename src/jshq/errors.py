"""The [JSHQ-###] error-code registry.

User-facing error details the backend authors end with a stable code —
``"Couldn't draft that message — the AI call failed. Try again in a moment.
[JSHQ-501]"`` — so any report (a screenshot, a friend's paraphrase, a log
line) targets the exact raise site no matter how the wording evolves. Tests
assert on the code, not the prose, which leaves message copy free to improve
without churn.

Rules:

- **Append-only.** A retired message keeps its number reserved forever; never
  renumber, never reuse.
- **Blocks** — the hundreds digit hints at the subsystem:
  0xx transport/validation/generic · 1xx companies/jobs/applications ·
  2xx settings/key/uploads · 3xx criteria & scoring · 4xx synthesis ·
  5xx compose/tailor/resume/render · 6xx ATS detect/refresh · 7xx backup ·
  8xx wizard/onboarding · 9xx reserved.
- **Raw exception text never rides a detail.** The raise site logs the
  exception server-side (stderr via the ``jshq`` logger); the code is what
  connects a user's toast to that log line.
- The registry doubles as the documentation source: a user-manual appendix
  can be generated from (code, message, note) — write notes as user-manual
  copy, without em dashes (the docs lint).
"""

from dataclasses import dataclass

from fastapi import HTTPException


@dataclass(frozen=True)
class ErrorCode:
    code: int
    message: str  # default user-facing sentence; fmt() appends the code
    note: str  # when it fires / what to do; future manual-appendix copy


REGISTRY: dict[int, ErrorCode] = {}


def _register(code: int, message: str, note: str) -> ErrorCode:
    if code in REGISTRY:
        raise ValueError(f"duplicate error code {code}")
    entry = ErrorCode(code, message, note)
    REGISTRY[code] = entry
    return entry


def fmt(entry: ErrorCode, message: str | None = None) -> str:
    """The wire form: a human sentence with the code appended.

    ``message`` overrides the registry default when the site composes its own
    prose (e.g. the validation handler's per-field sentences)."""
    return f"{message or entry.message} [JSHQ-{entry.code:03d}]"


def http_error(status: int, entry: ErrorCode, message: str | None = None) -> HTTPException:
    return HTTPException(status_code=status, detail=fmt(entry, message))


# Marker lines for the generated user-manual section. The manual is under the
# em-dash lint (test_docs_no_ai_tells), which is why notes are em-dash-free.
APPENDIX_START = "<!-- error-codes:start (generated from src/jshq/errors.py by scripts/gen_error_appendix.py; do not hand-edit) -->"
APPENDIX_END = "<!-- error-codes:end -->"


def manual_appendix() -> str:
    """The user manual's "Error codes" section, rendered from the registry.

    Notes are the copy, not the default messages: messages change freely (the
    code is the stable identity) and they use em dashes, which the manual's
    lint bans. scripts/gen_error_appendix.py splices this between the markers;
    tests/test_errors.py pins the shipped manual to this exact output."""
    lines = [
        "# Error codes",
        "",
        "Error messages in the app end with a code like [JSHQ-501]. The code",
        "names the exact failure point, so a screenshot or a copied line is",
        "enough to pin down what happened even after wording changes. What",
        "each code means:",
        "",
    ]
    for code in sorted(REGISTRY):
        entry = REGISTRY[code]
        lines.append(f"- **JSHQ-{code:03d}** {entry.note}")
    return "\n".join(lines) + "\n"


# --- 0xx transport/validation/generic ---------------------------------------

VALIDATION = _register(
    1,
    "That input couldn't be saved — check the form and try again.",
    "A request failed field validation. The message lists each field with "
    "what it needs.",
)

# --- 1xx companies/jobs/applications ------------------------------------------

COMPANY_GONE = _register(
    101,
    "That company no longer exists — refresh and try again.",
    "The company a form referenced was deleted in the meantime. Reload the "
    "view and pick again.",
)

JOB_GONE = _register(
    102,
    "That job no longer exists — refresh and try again.",
    "The job a form referenced was deleted in the meantime. Reload the view "
    "and pick again.",
)

APPLICATION_EXISTS = _register(
    103,
    "You already have an application for this job.",
    "Each job carries at most one application. The board resyncs to show "
    "the existing one.",
)

ENTITY_GONE = _register(
    104,
    "That item no longer exists — refresh and try again.",
    "The job, company, contact, or application an action referenced was "
    "deleted in the meantime. Reload the view.",
)

# --- 2xx settings/key/uploads ------------------------------------------------

TITLE_SUGGEST_FAILED = _register(
    201,
    "Couldn't fetch title suggestions — the AI call failed. Try again in a moment.",
    "The Suggest-with-AI call for LinkedIn title defaults did not complete. "
    "The server log has the underlying error.",
)

RULE_LOCATION_EXCLUDE = _register(
    202,
    "A location rule can't be an exclusion — there is no location block-list. "
    "Use an “Always include” rule with the towns that work instead.",
    "Sourcing rules have no location exclusion list. A town allowlist "
    "(an include rule) is the only location mechanism.",
)

UPLOAD_BAD_FILENAME = _register(
    203,
    "That file name can't be used — give it a simple name ending in "
    ".pdf, .docx, .doc, .txt, .md, or .html.",
    "Application files need a plain file name (no folders, no reserved "
    "device names, no trailing dot or space) with a supported extension.",
)

VOICE_GUIDE_TOO_LARGE = _register(
    204,
    "The voice guide is too large to save (max 200 KB).",
    "The voice guide is prose the AI writes with; trim it below 200 KB and "
    "save again.",
)

MODEL_UNSUPPORTED = _register(
    205,
    "That model isn't one of the supported choices — pick one from the list "
    "in Settings → System.",
    "The Anthropic model controls accept only the app's curated model list, "
    "so every Anthropic choice has known pricing and request behavior. The "
    "OpenAI-compatible endpoint takes a free-text model id instead.",
)

COMPAT_MODEL_REQUIRED = _register(
    206,
    "Enter the model id your endpoint serves — for example llama3.3 — to use "
    "the OpenAI-compatible endpoint for this task.",
    "The OpenAI-compatible endpoint has no curated model list; a task pointed "
    "at it needs a model id typed in (the endpoint's Test button lists what "
    "it serves).",
)

PROVIDER_NOT_CONFIGURED = _register(
    207,
    "No OpenAI-compatible endpoint is configured — add its base URL in "
    "Settings → System first.",
    "A task can only point at the OpenAI-compatible endpoint after its base "
    "URL is saved; configure the endpoint, then pick it for a task.",
)

PROVIDER_URL_INVALID = _register(
    208,
    "That base URL can't be used — it must start with http:// or https:// "
    "and name a host, like http://localhost:11434/v1.",
    "The endpoint base URL failed validation; nothing was saved. Check for "
    "typos, a missing scheme, or stray spaces.",
)

SCHEDULE_TIME_INVALID = _register(
    209,
    "That schedule couldn't be saved — every time must be 24-hour HH:MM, "
    "like 16:30, and each job needs at least one.",
    "A scheduled time failed validation and nothing was saved. Times are "
    "24-hour HH:MM (16:30, not 4:30 PM), and refresh and backup each need "
    "at least one.",
)

SCHEDULE_UNSUPPORTED = _register(
    210,
    "Automatic scheduling isn't supported on this system — the user manual "
    "has instructions for setting it up by hand.",
    "This system has no scheduler the app knows how to write to (launchd, "
    "cron, or Task Scheduler). Point your own scheduler at jshq refresh and "
    "jshq backup instead; the README shows how.",
)

SCHEDULE_APPLY_FAILED = _register(
    211,
    "The scheduler refused the change — nothing was installed.",
    "Writing the OS scheduler entry failed (the message carries what the "
    "scheduler said). The saved times are unchanged; fix the underlying "
    "issue and try Install again, or schedule by hand per the README.",
)

# --- 3xx criteria & scoring ---------------------------------------------------

CRITERIA_INVALID = _register(
    302,
    "The fit-criteria doc couldn't be validated — nothing was changed.",
    "The criteria doc (or an edit to it) failed validation and the doc on "
    "disk was left untouched. The message names the exact block and rule.",
)

PERSONA_INVALID = _register(
    303,
    "That couldn't be saved to the persona — nothing was changed.",
    "The persona (display name or role description) failed the doc's "
    "validation rails; the doc was left untouched.",
)

DISCIPLINE_INVALID = _register(
    304,
    "That field couldn't be saved — nothing was changed.",
    "The stated field/discipline failed the taxonomy write's validation; "
    "the doc was left untouched.",
)

GEOCODE_NO_MATCH = _register(
    305,
    'Couldn\'t find that place — try "Town, ST" (e.g. Madison, WI).',
    "The offline US place table had no match for the typed town. Only US "
    "places resolve; a state abbreviation helps.",
)

SUGGESTION_STALE = _register(
    306,
    "That suggestion has already been handled — refresh and try again.",
    "The suggestion an action targeted is no longer pending (acted on "
    "elsewhere, or superseded by a newer refresh).",
)

PROPOSAL_STALE = _register(
    307,
    "That proposal has already been handled — refresh and try again.",
    "The scoring-rule proposal an action targeted is no longer pending "
    "(acted on elsewhere, or replaced by a newer proposal).",
)

RULE_PROPOSAL_FAILED = _register(
    301,
    "Couldn't propose a scoring rule — the AI call failed. Try again in a moment.",
    "The scoring-rule proposal call for a job did not complete. The server "
    "log has the underlying error.",
)

# --- 4xx synthesis -------------------------------------------------------------

SYNTHESIS_FAILED = _register(
    401,
    "Couldn't draft the scoring reflection — the AI call failed. Try again in a moment.",
    "Draft with AI (Settings, Scoring) did not complete. The copy-prompt "
    "route still works without a key. The server log has the underlying error.",
)

# --- 5xx compose/tailor/resume/render -----------------------------------------

COMPOSE_FAILED = _register(
    501,
    "Couldn't draft that message — the AI call failed. Try again in a moment.",
    "The compose call (outreach drafts, answers) did not complete. The "
    "server log has the underlying error.",
)

REFINE_FAILED = _register(
    502,
    "Couldn't refine that draft — the AI call failed. Try again in a moment.",
    "The AI-tell refine call did not complete. The draft is unchanged. The "
    "server log has the underlying error.",
)

TAILOR_FAILED = _register(
    503,
    "Couldn't tailor the resume — the AI call failed. Try again in a moment.",
    "The tailoring generation call did not complete. Nothing was saved. The "
    "server log has the underlying error.",
)

TAILOR_CHAT_FAILED = _register(
    504,
    "Couldn't apply that request to the tailoring — the AI call failed. Try again in a moment.",
    "A tailoring chat turn did not complete. The pending tailoring is "
    "unchanged. The server log has the underlying error.",
)

TAILORING_PENDING = _register(
    505,
    "A tailoring is already in progress for this application.",
    "Each application carries at most one pending tailoring. Apply or "
    "discard the pending one first.",
)

JOB_NO_DESCRIPTION = _register(
    506,
    "This job has no saved description — open the job and paste the description text in first.",
    "Tailoring reads the job description. Add the description text on the "
    "job's detail pane, then tailor again.",
)
