"""Job Search HQ API. All routes pass through require_user() — see CLAUDE.md hard rules."""

import asyncio
import hashlib
import importlib.metadata
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from jshq import aicfg, apikey, compose, errors, jobparse, linkedin_titles, logos, oaicompat, onboarding, paths, providers, refine, schedule, tailor, usage
from jshq.ats import detect as ats_detect
from jshq.ats import patterns as ats_patterns
from jshq.ats import refresh as ats_refresh
from jshq.ats.adapters import ADAPTERS
from jshq.ats.normalize import derive_level_band
from jshq.db import get_db, init_db
from jshq.ics import build_calendar
from jshq.models import (
    ActivityIn,
    AiModelsIn,
    AiProvidersIn,
    AxisChoiceIn,
    ApiKeyIn,
    ApplicationIn,
    ApplicationUpdate,
    CareersPreviewIn,
    CompanyIn,
    ComposeIn,
    ContactIn,
    CoverRerenderIn,
    CriteriaIn,
    InclusionRulesIn,
    JobCreateIn,
    JobDetailsIn,
    JobElevateIn,
    JobParseUrlIn,
    JobStatusIn,
    PersonaIn,
    RefineTellsIn,
    RefreshIn,
    ReminderIn,
    ReminderPatch,
    ReminderSuggestionActionIn,
    ScheduleIn,
    ScoringRuleActionIn,
    SettingIn,
    SuggestionActionIn,
    TailorIn,
    TailoringChatIn,
    TailoringPatch,
    VoiceGuideIn,
)
from jshq.models import DisciplineIn, OnboardingIn, RoadmapIn, SynthesisApplyIn, SynthesisReplyIn
from jshq.reminder_suggest import suggest_reminders
from jshq.resume import cover, render
from jshq.scoring import estimate_rescore, geo, haiku, learned, run_scoring, synthesis
from jshq.scoring import criteria
from jshq.scoring.criteria import (
    CriteriaError,
    load_criteria,
    read_editable,
    render_params_summary,
    write_criteria,
)
from jshq.scoring.digest import build_dismissal_digest
from jshq.scoring.rules import read_rules, write_rules
from jshq.scoring.suggest import suggest_exclusions

# App logging → stderr (whatever runs `jshq serve` decides where that goes).
# Without this, getLogger("jshq.*") messages (e.g. a scoring rate-limit) would
# vanish — the app shipped with no logging config before Phase 8.
_applog = logging.getLogger("jshq")
if not _applog.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    _applog.addHandler(_h)
    _applog.setLevel(logging.INFO)
    _applog.propagate = False

# Written by `jshq backup` (jshq.backup.write_status). A file, not a
# settings row: a corrupt live DB must still get its failure recorded.
BACKUP_STATUS_PATH = paths.DATA_DIR / "backup_status.json"

# Served read-only by the in-app Help view (Phase 9), mirroring criteria-doc.
# Read from the package defaults so it tracks the installed version.
USER_MANUAL_PATH = paths.DEFAULTS_DIR / "user-manual.md"

try:
    VERSION = importlib.metadata.version("jshq")
except importlib.metadata.PackageNotFoundError:  # bare checkout, no install
    VERSION = "0.0.0+dev"


async def require_user() -> None:
    """Auth stub. Pass-through for now; future basic-auth/token lives here.

    Do not remove — every route depends on it so auth can be added in one place.
    """
    return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Job Search HQ",
    version=VERSION,
    dependencies=[Depends(require_user)],
    lifespan=lifespan,
)


# Request-validation 422s, humanized (error-audit F2). FastAPI's default body
# is a Pydantic error array that the frontend flattens into toasts like
# "body.tier1_params.comp_floor: Input should be a valid integer" — Pydantic
# paths and Pydantic English at the user. This handler turns each error into
# "<field label> <plain sentence>" and ships ONE string detail with the
# [JSHQ-001] code; the raw (loc, msg, type) list rides alongside as "errors"
# for API callers. Messages from our own @model/@field_validator raises
# (type == "value_error") pass through verbatim: they are already authored
# prose, and settings.js parseRulesError matches on that text (P2).
_VALIDATION_SENTENCES = {
    "missing": "is required",
    "string_type": "must be text",
    "int_type": "must be a whole number",
    "int_parsing": "must be a whole number",
    "int_from_float": "must be a whole number",
    "float_type": "must be a number",
    "float_parsing": "must be a number",
    "bool_type": "must be yes or no",
    "bool_parsing": "must be yes or no",
    "list_type": "must be a list",
    "dict_type": "must be a set of key/value pairs",
    "literal_error": "isn't one of the allowed options",
    "enum": "isn't one of the allowed options",
    "string_pattern_mismatch": "isn't in the expected format",
    "date_from_datetime_parsing": "must be a date like 2026-08-22",
    "date_parsing": "must be a date like 2026-08-22",
}

# Field leaves whose generic "isn't in the expected format" needs a concrete
# example (the reminder time regex used to surface AS a regex in the toast).
_VALIDATION_HINTS = {"due_time": "use a 24-hour time like 09:30"}


def _validation_sentence(err: dict) -> str:
    kind = err.get("type", "")
    msg = str(err.get("msg", ""))
    if kind == "value_error":
        # Our own validator text — already a user-facing sentence.
        text = msg.removeprefix("Value error, ")
        return text[:1].upper() + text[1:] if text else "Invalid value."
    if kind == "json_invalid":
        return "The request wasn't valid JSON."
    # Drop the source marker and list indexes; the leaf field is the label.
    loc = [
        str(part)
        for part in err.get("loc", ())
        if part not in ("body", "query", "path") and not isinstance(part, int)
    ]
    leaf = loc[-1] if loc else ""
    label = leaf.replace("_", " ").strip().capitalize() or "That value"
    ctx = err.get("ctx") or {}
    if kind in ("string_too_short", "too_short"):
        n = ctx.get("min_length", 1)
        phrase = "can't be empty" if n <= 1 else f"needs at least {n} characters"
    elif kind == "string_too_long":
        phrase = f"is too long (max {ctx.get('max_length', '?')} characters)"
    elif kind == "greater_than_equal":
        phrase = f"must be at least {ctx.get('ge')}"
    elif kind == "less_than_equal":
        phrase = f"must be at most {ctx.get('le')}"
    elif kind == "greater_than":
        phrase = f"must be more than {ctx.get('gt')}"
    elif kind == "less_than":
        phrase = f"must be less than {ctx.get('lt')}"
    else:
        # Unknown kinds keep Pydantic's sentence, but behind the field label
        # instead of a body.* path.
        phrase = _VALIDATION_SENTENCES.get(kind) or (msg[:1].lower() + msg[1:] if msg else "is invalid")
    if leaf in _VALIDATION_HINTS and kind == "string_pattern_mismatch":
        phrase += f" — {_VALIDATION_HINTS[leaf]}"
    return f"{label} {phrase}."


@app.exception_handler(RequestValidationError)
async def _humanize_validation_error(request: Request, exc: RequestValidationError):
    raw = exc.errors()
    sentences: list[str] = []
    for err in raw:
        sentence = _validation_sentence(err)
        if sentence not in sentences:
            sentences.append(sentence)
    text = " ".join(sentences)
    # A passed-through validator sentence may already carry its own code
    # (e.g. the location-exclude rule) — don't stack [JSHQ-001] on top.
    detail = text if "[JSHQ-" in text else errors.fmt(errors.VALIDATION, text or None)
    return JSONResponse(
        status_code=422,
        content={
            "detail": detail,
            # The machine-shaped view, minus ctx (whose values can be
            # unserializable exception objects under Pydantic v2).
            "errors": [
                {"loc": list(e.get("loc", ())), "msg": str(e.get("msg", "")), "type": e.get("type", "")}
                for e in raw
            ],
        },
    )

JSON_COMPANY_FIELDS = ("linkedin_company_ids", "linkedin_title_searches")

# Strong refs to fire-and-forget onboarding tasks so they aren't GC'd mid-run
# (asyncio only holds weak refs to tasks); discarded on completion.
_onboard_tasks: set[asyncio.Task] = set()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _letter_date() -> str:
    # Not strftime("%-d"): the no-pad flag is a glibc/BSD extension that
    # raises ValueError on Windows.
    today = date.today()
    return f"{today:%B} {today.day}, {today.year}"


def _spawn_onboarding(company_id: int) -> None:
    """Fire-and-forget the add-time ATS detect + job pull (QA pass 2). Named as
    its own seam so tests can disable the background work (it does real network
    I/O and opens its own DB connection)."""
    task = asyncio.create_task(ats_refresh.detect_and_fetch_company(company_id))
    _onboard_tasks.add(task)
    task.add_done_callback(_onboard_tasks.discard)


def _spawn_company_refresh(company_id: int) -> None:
    """Fire-and-forget the on-demand single-board re-fetch. Its own seam (like
    _spawn_onboarding) so tests can disable the real network/DB work."""
    task = asyncio.create_task(ats_refresh.refresh_company_board(company_id))
    _onboard_tasks.add(task)
    task.add_done_callback(_onboard_tasks.discard)

COMPANY_COLUMNS = (
    "name", "location", "priority", "status", "values_fit",
    "website", "careers_url", "ats_type", "ats_slug", "notes",
    "linkedin_company_ids", "linkedin_title_searches",
)

CONTACT_COLUMNS = (
    "name", "company_id", "role", "linkedin_url", "email", "source",
    "relationship_notes", "last_contact_date",
)


# Shared by list + fetch: PUT/POST return through _fetch_company and the
# frontend replaces list rows with those payloads in place, so every company
# payload must carry the count or the list pill vanishes after a quiet save.
# active_job_count excludes Tier-1 hard fails (fit_score = 0, no LLM cost) unless
# manually elevated — they're hidden by default in the Jobs/Today lists, so they
# must not inflate the count either. `IS NOT 0` is NULL-safe: an unscored active
# job (fit_score NULL = "not scored yet") still counts, matching the frontend.
COMPANY_SELECT = """
    SELECT companies.*,
           (SELECT COUNT(*) FROM jobs
             WHERE jobs.company_id = companies.id AND jobs.status = 'active'
               AND (jobs.fit_score IS NOT 0 OR jobs.manually_elevated = 1))
           AS active_job_count
    FROM companies
"""


def _serialize_company(row: sqlite3.Row) -> dict:
    company = dict(row)
    for field in JSON_COMPANY_FIELDS:
        company[field] = json.loads(company[field]) if company[field] else []
    company["logo_url"] = (
        f"/api/companies/{company['id']}/logo" if company.get("logo_ext") else None
    )
    return company


def _with_company_logo(payload: dict) -> dict:
    """For a job/application/contact payload that joined the company: set
    company_logo to the logo endpoint URL when that company has a cached logo
    (the joined company_logo_ext), else None; drop the raw ext. Mutates + returns."""
    ext = payload.pop("company_logo_ext", None)
    cid = payload.get("company_id")
    payload["company_logo"] = f"/api/companies/{cid}/logo" if (ext and cid) else None
    return payload


def _company_values(body: CompanyIn) -> list:
    data = body.model_dump()
    for field in JSON_COMPANY_FIELDS:
        data[field] = json.dumps(data[field])
    return [data[col] for col in COMPANY_COLUMNS]


def _fetch_company(db: sqlite3.Connection, company_id: int) -> dict:
    row = db.execute(f"{COMPANY_SELECT} WHERE companies.id = ?", (company_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="company not found")
    return _serialize_company(row)


def _fetch_contact(db: sqlite3.Connection, contact_id: int) -> dict:
    row = db.execute(
        """SELECT contacts.*, companies.name AS company_name,
                  companies.logo_ext AS company_logo_ext
           FROM contacts LEFT JOIN companies ON companies.id = contacts.company_id
           WHERE contacts.id = ?""",
        (contact_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="contact not found")
    return _with_company_logo(dict(row))


def _check_contact_company(db: sqlite3.Connection, body: ContactIn) -> None:
    if body.company_id is None:
        return
    row = db.execute("SELECT 1 FROM companies WHERE id = ?", (body.company_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=400, detail=errors.fmt(errors.COMPANY_GONE))


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok", "version": VERSION}


@app.get("/api/companies")
async def list_companies(db: sqlite3.Connection = Depends(get_db)) -> list[dict]:
    rows = db.execute(
        f"""
        {COMPANY_SELECT}
        ORDER BY CASE WHEN status = 'closed' THEN 1 ELSE 0 END,
                 CASE WHEN priority IS NULL THEN 1 ELSE 0 END,
                 priority, name
        """
    ).fetchall()
    return [_serialize_company(row) for row in rows]


@app.get("/api/companies/{company_id}")
async def get_company(company_id: int, db: sqlite3.Connection = Depends(get_db)) -> dict:
    # Single-company read, used by the detail-pane poll while the ATS check runs.
    return _fetch_company(db, company_id)


# Content-type for a served logo by stored extension (Starlette would guess, but
# .ico guesses inconsistently across platforms — be explicit).
_LOGO_MEDIA = {
    "png": "image/png", "ico": "image/x-icon", "jpg": "image/jpeg",
    "webp": "image/webp", "svg": "image/svg+xml", "gif": "image/gif",
}


@app.get("/api/companies/{company_id}/logo")
async def get_company_logo(
    company_id: int, db: sqlite3.Connection = Depends(get_db)
) -> FileResponse:
    """Serve the cached brand logo (Apache only serves frontend/). The filename is
    built from the int id + DB-stored ext — no user input, so no path-traversal
    guard is needed. A 404 tells the frontend to render the monogram instead."""
    row = db.execute("SELECT logo_ext FROM companies WHERE id = ?", (company_id,)).fetchone()
    if row is None or not row["logo_ext"]:
        raise HTTPException(status_code=404, detail="no logo")
    path = logos.LOGOS_DIR / f"{company_id}.{row['logo_ext']}"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="no logo")
    return FileResponse(path, media_type=_LOGO_MEDIA.get(row["logo_ext"], "application/octet-stream"))


@app.post("/api/companies/{company_id}/logo/refresh")
async def refresh_company_logo_endpoint(
    company_id: int, db: sqlite3.Connection = Depends(get_db)
) -> dict:
    """Re-fetch + cache a company's logo on demand (the detail-pane ↻). Returns the
    company payload with the updated logo_url; best-effort — a miss leaves the monogram."""
    _fetch_company(db, company_id)  # 404 if unknown
    await logos.refresh_company_logo(db, company_id)
    return _fetch_company(db, company_id)


def _norm_company_name(name) -> str:
    return " ".join(str(name or "").lower().split())


def _norm_site_host(url) -> str | None:
    """A company website normalized to its host ("https://www.Acme.example/jobs"
    ⇒ "acme.example"): the path never distinguishes companies on their own
    domain. Careers URLs are NOT host-normalized — see _norm_careers_url."""
    u = str(url or "").strip().lower()
    if not u:
        return None
    u = re.sub(r"^[a-z][a-z0-9+.-]*://", "", u)
    u = u[4:] if u.startswith("www.") else u
    return u.split("/", 1)[0].rstrip(".") or None


def _norm_careers_url(url) -> str | None:
    """A careers URL normalized whole (scheme/www/trailing-slash stripped, path
    KEPT): hosted ATS boards share a host across companies
    (boards.greenhouse.example/acme vs /bravo), so host-matching would call two
    different companies duplicates."""
    u = str(url or "").strip().lower()
    if not u:
        return None
    u = re.sub(r"^[a-z][a-z0-9+.-]*://", "", u)
    u = u[4:] if u.startswith("www.") else u
    return u.rstrip("/") or None


def _find_duplicate_company(db: sqlite3.Connection, body: CompanyIn):
    """The existing company `body` duplicates, or None. A duplicate shares a
    normalized name, a website host, or a whole careers URL — the three
    identities a user re-adding a company could arrive with. Absent fields
    never match (a company with no careers URL is only guarded by name/site)."""
    name = _norm_company_name(body.name)
    host = _norm_site_host(body.website)
    careers = _norm_careers_url(body.careers_url)
    for row in db.execute("SELECT * FROM companies").fetchall():
        if _norm_company_name(row["name"]) == name:
            return row, "name"
        if host and _norm_site_host(row["website"]) == host:
            return row, "website"
        if careers and _norm_careers_url(row["careers_url"]) == careers:
            return row, "careers URL"
    return None


@app.post("/api/companies", status_code=201)
async def create_company(body: CompanyIn, db: sqlite3.Connection = Depends(get_db)) -> dict:
    # Refuse silent duplicates: re-adding by the same name / site / careers URL
    # creates a second row the refresh pipeline then pulls twice. 409 names the
    # existing company so both the Companies modal and the wizard can say
    # "already on your board" (the wizard treats it as the required step being
    # satisfied and reuses the existing row).
    dupe = _find_duplicate_company(db, body)
    if dupe is not None:
        row, basis = dupe
        # Structured like the add-job 409: the client gets the existing row's id
        # (error.info) so the wizard can reuse it as the satisfied required step.
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"{row['name']} is already on your board (same {basis}).",
                "company_id": row["id"],
            },
        )
    if not body.linkedin_title_searches:
        # Seed-on-create only; PUT never re-seeds, so clearing all titles stays
        # possible. The list is config (settings), not a code constant — and it
        # ships empty, so a fresh install creates companies with no titles until
        # the user sets some (the titles panel's empty state asks).
        seeded = _setting_value(db, "linkedin_title_defaults")
        body.linkedin_title_searches = list(seeded or [])
    placeholders = ", ".join("?" for _ in COMPANY_COLUMNS)
    cursor = db.execute(
        f"INSERT INTO companies ({', '.join(COMPANY_COLUMNS)}) VALUES ({placeholders})",
        _company_values(body),
    )
    company_id = cursor.lastrowid
    # When there's a URL to probe, kick off ATS detection + an initial job pull
    # in the background (QA pass 2). Stamp 'checking' first so the 201 payload
    # and the detail-pane poll show progress until the task flips the status.
    probe = bool(body.website or body.careers_url)
    if probe:
        db.execute(
            "UPDATE companies SET ats_last_status = 'checking', ats_last_checked = ? WHERE id = ?",
            (_utc_now(), company_id),
        )
    db.commit()
    if probe:
        _spawn_onboarding(company_id)
    return _fetch_company(db, company_id)


def _url_changed_on_update(old: sqlite3.Row, body: CompanyIn) -> bool:
    """Whether a PUT re-runs the add-time ATS probe: either URL genuinely
    changed (normalized, so a scheme/www/trailing-slash touch-up stays quiet)
    to a non-empty value. Before this, only POST /api/companies ever detected
    anything — adding a careers URL to an existing company was a silent no-op,
    though the wizard's done step already promised it would get "another look".

    Deliberately NOT gated on an in-flight check: the commonest correction is
    fixing a typo'd URL seconds after the add, exactly while the add-time
    probe is running. Skipping then meant the correction was never probed at
    all (nothing re-queued it), while the in-flight probe settled against the
    typo — under UI copy promising a corrected URL "gets re-checked when it
    saves". Overlapping probes are safe: each task re-reads its row at start,
    both write ats_last_status last-writer-wins, and the pull half serializes
    under _refresh_lock with dedupe upserts."""
    new_careers = _norm_careers_url(body.careers_url)
    new_site = _norm_site_host(body.website)
    return bool(
        (new_careers and new_careers != _norm_careers_url(old["careers_url"]))
        or (new_site and new_site != _norm_site_host(old["website"]))
    )


@app.put("/api/companies/{company_id}")
async def update_company(
    company_id: int, body: CompanyIn, db: sqlite3.Connection = Depends(get_db)
) -> dict:
    old = db.execute(
        "SELECT careers_url, website, ats_last_status FROM companies WHERE id = ?",
        (company_id,),
    ).fetchone()
    if old is None:
        raise HTTPException(status_code=404, detail="company not found")
    assignments = ", ".join(f"{col} = ?" for col in COMPANY_COLUMNS)
    db.execute(
        f"UPDATE companies SET {assignments}, updated_at = datetime('now') WHERE id = ?",
        [*_company_values(body), company_id],
    )
    # A changed website/careers URL re-runs detection exactly like an add:
    # stamp 'checking' so the PUT response itself shows progress, then probe.
    probe = _url_changed_on_update(old, body)
    if probe:
        db.execute(
            "UPDATE companies SET ats_last_status = 'checking', ats_last_checked = ? WHERE id = ?",
            (_utc_now(), company_id),
        )
    db.commit()
    if probe:
        _spawn_onboarding(company_id)
    return _fetch_company(db, company_id)


@app.delete("/api/companies/{company_id}")
async def delete_company(company_id: int, db: sqlite3.Connection = Depends(get_db)) -> dict:
    if db.execute("SELECT 1 FROM companies WHERE id = ?", (company_id,)).fetchone() is None:
        raise HTTPException(status_code=404, detail="company not found")
    job_ids = [
        row["id"]
        for row in db.execute("SELECT id FROM jobs WHERE company_id = ?", (company_id,))
    ]
    app_ids = []
    if job_ids:
        job_marks = ", ".join("?" for _ in job_ids)
        app_ids = [
            row["id"]
            for row in db.execute(
                f"SELECT id FROM applications WHERE job_id IN ({job_marks})", job_ids
            )
        ]
        _delete_tailorings(db, app_ids)
        # applications reference jobs (FK, no cascade) — delete them first
        for entity_type, ids in (("application", app_ids), ("job", job_ids)):
            if not ids:
                continue
            marks = ", ".join("?" for _ in ids)
            db.execute(
                f"DELETE FROM activities WHERE entity_type = ? AND entity_id IN ({marks})",
                [entity_type, *ids],
            )
            db.execute(
                f"DELETE FROM reminders WHERE entity_type = ? AND entity_id IN ({marks})",
                [entity_type, *ids],
            )
            db.execute(f"DELETE FROM {entity_type}s WHERE id IN ({marks})", ids)
    detached = db.execute(
        "UPDATE contacts SET company_id = NULL, updated_at = datetime('now') WHERE company_id = ?",
        (company_id,),
    ).rowcount
    for table in ("activities", "reminders"):
        db.execute(
            f"DELETE FROM {table} WHERE entity_type = 'company' AND entity_id = ?",
            (company_id,),
        )
    db.execute("DELETE FROM companies WHERE id = ?", (company_id,))
    db.commit()
    return {
        "deleted": company_id,
        "contacts_detached": detached,
        "jobs_deleted": len(job_ids),
        "applications_deleted": len(app_ids),
    }


# Jobs are written by the refresh pipeline; the API only reads them and lets
# the user move status between active/applied/dismissed (closed is decay's).

JOB_LIST_COLUMNS = (
    "jobs.id, jobs.company_id, jobs.external_id, jobs.title, jobs.url, "
    "jobs.location, jobs.remote_type, jobs.level_band, jobs.salary_min, "
    "jobs.salary_max, jobs.salary_stated, jobs.first_seen, jobs.last_seen, "
    "jobs.status, jobs.fit_score, jobs.fit_quadrant, jobs.near_miss_flags, "
    "jobs.miss_count, jobs.manually_elevated, jobs.source, "
    # The score BEFORE caps and deductions. fit_score clamps a capped cohort —
    # every IC seat above the ceiling lands on the same integer — so this is the
    # only field that orders those jobs against each other. NULL for Tier-1 hard
    # fails and pre-redesign rows, which is why the sort has to tolerate NULLs.
    "json_extract(jobs.score_detail, '$.model_score') AS model_score, "
    "companies.name AS company_name, "
    "companies.logo_ext AS company_logo_ext, "
    "applications.id AS application_id, applications.status AS application_status"
)

# 1:1 per the unique index, so the LEFT JOIN never multiplies job rows.
JOB_APPLICATION_JOIN = "LEFT JOIN applications ON applications.job_id = jobs.id"


@app.get("/api/jobs")
async def list_jobs(
    company_id: int | None = None,
    db: sqlite3.Connection = Depends(get_db),
) -> list[dict]:
    # description_text deliberately excluded: full JDs would bloat the list
    # payload. The detail endpoint returns it. company_id scopes the list to one
    # company (the Companies view's "Top jobs" section + "View all jobs" link).
    where, params = "", []
    if company_id is not None:
        where = "WHERE jobs.company_id = ?"
        params.append(company_id)
    rows = db.execute(
        f"""SELECT {JOB_LIST_COLUMNS}
            FROM jobs JOIN companies ON companies.id = jobs.company_id
            {JOB_APPLICATION_JOIN}
            {where}
            ORDER BY jobs.last_seen DESC, jobs.id DESC""",
        params,
    ).fetchall()
    return [_with_company_logo(dict(row)) for row in rows]


@app.post("/api/jobs/parse-url")
async def parse_job_url_endpoint(
    body: JobParseUrlIn, db: sqlite3.Connection = Depends(get_db)
) -> dict:
    """Best-effort prefill for the manual Add-job modal: fetch the pasted posting
    URL and extract its fields (schema.org JobPosting JSON-LD, else a Haiku pass
    over the page text). Never creates anything — the client prefills the modal
    and the user saves via POST /api/jobs. Returns nulls for fields it couldn't
    find; 422 for a bad/LinkedIn URL or an unreachable page."""
    binding = aicfg.binding_for(db, "jobparse")
    # Pass a ready provider's client in; otherwise None, and jobparse's own
    # JSON-LD-only degradation carries (it also self-guards the keyless
    # Anthropic default for direct callers).
    client = (
        providers.build_client(db, binding.provider, max_retries=4)
        if providers.is_ready(db, binding.provider)
        else None
    )
    try:
        return await jobparse.parse_job_url(body.url, client=client, model=binding.model)
    except jobparse.JobParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


_TRACKING_PARAMS = {"gh_src", "ref", "src", "source"}  # plus any utm_* param


def _url_key(url: str) -> str:
    """Comparison key for posting URLs: lowercase, scheme/www./fragment/trailing-
    slash insensitive. The query string is KEPT (minus tracking params, remainder
    sorted) — it carries job identity on some boards (embedded greenhouse tenants
    serve every posting at `/?gh_jid=<id>`; other careers sites use `?jobId=…`),
    so stripping it would collapse every job on those boards into one key."""
    parts = urlsplit(url.strip().lower())
    host = parts.netloc.removeprefix("www.")
    query = "&".join(
        sorted(
            p
            for p in parts.query.split("&")
            if p and not p.startswith("utm_") and p.split("=", 1)[0] not in _TRACKING_PARAMS
        )
    )
    return f"{host}{parts.path.rstrip('/')}?{query}"


@app.post("/api/jobs", status_code=201)
async def create_job(body: JobCreateIn, db: sqlite3.Connection = Depends(get_db)) -> dict:
    """Hand-enter a job the boards didn't surface (the user found it via the
    LinkedIn role links / careers page). Stored as source='manual' (board decay
    never closes it; the refresh liveness pass owns its closure), scored
    immediately like an ingested job, and deduped two ways: by URL against ALL
    same-company rows including ATS-ingested ones (the pasted URL is the same
    posting the adapter pulled — creating it again would twin the row), then by
    the manual dedupe key for title-only adds. Either match is a 409."""
    if db.execute("SELECT id FROM companies WHERE id = ?", (body.company_id,)).fetchone() is None:
        raise HTTPException(status_code=404, detail="company not found")
    key_seed = (body.url or body.title).strip().lower()
    dedupe_key = f"manual:{body.company_id}:{hashlib.sha1(key_seed.encode()).hexdigest()[:12]}"
    existing = db.execute(
        "SELECT id, status, title FROM jobs WHERE dedupe_key = ?", (dedupe_key,)
    ).fetchone()
    if existing is None and body.url and body.url.strip():
        wanted = _url_key(body.url)
        existing = next(
            (
                row
                for row in db.execute(
                    # ORDER BY: if several rows share the URL (a pre-existing
                    # ats/manual twin), report the ATS row — it's the one the
                    # board keeps honest — then the oldest.
                    "SELECT id, status, title, url FROM jobs "
                    "WHERE company_id = ? AND url IS NOT NULL "
                    "ORDER BY source = 'manual', id",
                    (body.company_id,),
                )
                if _url_key(row["url"]) == wanted
            ),
            None,
        )
    if existing:
        # Structured so the Add-job UI can offer to reactivate a dismissed/closed
        # duplicate instead of dead-ending on a flat "already tracked" error.
        raise HTTPException(
            status_code=409,
            detail={
                "message": "this job is already tracked",
                "job_id": existing["id"],
                "status": existing["status"],
                "title": existing["title"],
            },
        )
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    salary_stated = 1 if (body.salary_min is not None or body.salary_max is not None) else 0
    cur = db.execute(
        """INSERT INTO jobs (company_id, external_id, title, url, location, remote_type,
               level_band, salary_min, salary_max, salary_stated, description_text,
               first_seen, last_seen, status, miss_count, source, dedupe_key)
           VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 0, 'manual', ?)""",
        (
            body.company_id, body.title, body.url, body.location, body.remote_type,
            derive_level_band(body.title), body.salary_min, body.salary_max, salary_stated,
            body.description_text, now, now, dedupe_key,
        ),
    )
    job_id = cur.lastrowid
    db.commit()
    # Score now (Tier 1 + Haiku) so the fit shows immediately, like onboarding.
    # Best-effort: the job is already saved, so a scoring hiccup (no key, or a
    # busy DB during a concurrent refresh) leaves it pending for the next rescore
    # rather than failing the create.
    try:
        await run_scoring(db)
    except Exception:
        _applog.warning("manual job %s: immediate scoring failed", job_id, exc_info=True)
    return await get_job(job_id, db)


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: int, db: sqlite3.Connection = Depends(get_db)) -> dict:
    row = db.execute(
        f"""SELECT jobs.*, companies.name AS company_name,
               companies.logo_ext AS company_logo_ext,
               applications.id AS application_id, applications.status AS application_status
           FROM jobs JOIN companies ON companies.id = jobs.company_id
           {JOB_APPLICATION_JOIN}
           WHERE jobs.id = ?""",
        (job_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="job not found")
    return _with_company_logo(dict(row))


@app.patch("/api/jobs/{job_id}")
async def update_job_status(
    job_id: int, body: JobStatusIn, db: sqlite3.Connection = Depends(get_db)
) -> dict:
    row = db.execute(
        """SELECT title, status, fit_score,
                  json_extract(score_detail, '$.model_score') AS model_score
           FROM jobs WHERE id = ?""",
        (job_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="job not found")
    if body.status == "applied":
        # _ensure_applied owns the flip here: it needs the pre-change status to
        # tell a real transition from a no-op re-apply, so writing it first
        # would make every apply look like a no-op.
        _ensure_applied(db, job_id)
    else:
        db.execute("UPDATE jobs SET status = ? WHERE id = ?", (body.status, job_id))
        if row["status"] == "applied":
            # Leaving applied is a reversal and earns its own row. The prior
            # 'applied' stays as history — without this the timeline showed an
            # application that was submitted and never undone.
            db.execute(
                """INSERT INTO activities (entity_type, entity_id, date, type, content)
                   VALUES ('job', ?, ?, 'unapplied', ?)""",
                (
                    job_id,
                    date.today().isoformat(),
                    json.dumps({"title": row["title"], "to": body.status}),
                ),
            )
    if body.status == "dismissed" and body.reason:
        # Title snapshotted so the digest/suggester survive later title edits.
        # Scores snapshotted DECISION-TIME (2026-08-08): a later rescore
        # rewrites jobs.fit_score, so without these the applied-vs-dismissed
        # threshold validation would compare decisions against scores that did
        # not exist when the decisions were made.
        content = json.dumps(
            {
                "reason": body.reason, "note": body.note or None,
                "title": row["title"], "fit_score": row["fit_score"],
                "model_score": row["model_score"],
            }
        )
        db.execute(
            """INSERT INTO activities (entity_type, entity_id, date, type, content)
               VALUES ('job', ?, ?, 'dismissal', ?)""",
            (job_id, date.today().isoformat(), content),
        )
    db.commit()
    return await get_job(job_id, db)


@app.patch("/api/jobs/{job_id}/details")
async def update_job_details(
    job_id: int, body: JobDetailsIn, db: sqlite3.Connection = Depends(get_db)
) -> dict:
    """Correct an ATS job's facts — a wrong/missing location, the remote type, or a
    salary range learned from a recruiter. Sets manually_edited so the next board
    refresh preserves these fields instead of overwriting them, then re-scores:
    location/salary feed the deterministic Tier-1 gate, so a fix can move the job out
    of the fit-0 hard-fail (or flip the comp gate). salary_stated is derived here.
    Best-effort scoring, like the manual-add path.

    The rescore is scoped to THIS job with its OWN status (caught 2026-08-10):
    run_scoring's default population is active-only, so an edit to an
    applied/dismissed/closed job used to NULL its score and then rescore nothing
    — permanent score loss with no error. job_ids + the row's status reach the
    non-active row without sweeping in other pending rows of that status."""
    row = db.execute("SELECT id, status FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="job not found")
    salary_stated = 1 if (body.salary_min is not None or body.salary_max is not None) else 0
    # NULL the fit columns (the exact set the refresh nulls on a JD change) so the job
    # is pending and run_scoring re-evaluates it with the corrected facts.
    db.execute(
        """UPDATE jobs SET location = ?, remote_type = ?, salary_min = ?,
               salary_max = ?, salary_stated = ?, manually_edited = 1,
               tier1_results = NULL, fit_score = NULL, fit_quadrant = NULL,
               near_miss_flags = NULL, scoring_notes = NULL, score_detail = NULL
           WHERE id = ?""",
        (
            body.location, body.remote_type, body.salary_min, body.salary_max,
            salary_stated, job_id,
        ),
    )
    db.commit()
    try:
        await run_scoring(db, job_ids=(job_id,), statuses=(row["status"],))
    except Exception:
        _applog.warning("job %s: rescore after edit failed", job_id, exc_info=True)
    return await get_job(job_id, db)


@app.post("/api/jobs/{job_id}/elevate")
async def elevate_job(
    job_id: int, body: JobElevateIn, db: sqlite3.Connection = Depends(get_db)
) -> dict:
    """Manual override (QA): keep a maybe/below-fit job in the positive-fit
    category. Flips a flag only — fit_score stays model-judged and the flag
    survives rescores (run_scoring never writes this column)."""
    row = db.execute("SELECT id FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="job not found")
    db.execute(
        "UPDATE jobs SET manually_elevated = ? WHERE id = ?",
        (1 if body.elevated else 0, job_id),
    )
    db.commit()
    return await get_job(job_id, db)


# Applications (Phase 7c). One row per job (unique index), created
# from job detail ("Start application" → drafting) or by the jobs-PATCH applied
# hook. Timestamps written explicitly (v5 column migration carries no default).

APPLICATION_COLUMNS = (
    "status", "applied_date", "resume_version", "cover_note", "next_step", "next_step_date",
)

APPLICATION_SELECT = """
    SELECT a.*, j.title AS job_title, j.url AS job_url, j.status AS job_status,
           j.miss_count, j.company_id, j.fit_score, j.manually_elevated,
           j.salary_min, j.salary_max,
           c.name AS company_name,
           c.logo_ext AS company_logo_ext
    FROM applications a
    JOIN jobs j ON j.id = a.job_id
    JOIN companies c ON c.id = j.company_id
"""


# Tailoring artifacts: versioned PDFs per application, under
# gitignored data/. Apache never serves this — downloads go through the API.
APPLICATIONS_DIR = paths.DATA_DIR / "applications"


def _delete_tailorings(db: sqlite3.Connection, application_ids: list[int]) -> None:
    """Cascade for application deletes: tailoring rows, their chat messages,
    and the PDF directory.
    The rmtree matters — SQLite reuses freed rowids, so a future application
    could otherwise inherit a deleted one's stale PDFs."""
    if not application_ids:
        return
    marks = ", ".join("?" for _ in application_ids)
    db.execute(
        f"""DELETE FROM tailoring_messages WHERE tailoring_id IN
            (SELECT id FROM tailorings WHERE application_id IN ({marks}))""",
        application_ids,
    )
    db.execute(f"DELETE FROM tailorings WHERE application_id IN ({marks})", application_ids)
    for application_id in application_ids:
        shutil.rmtree(APPLICATIONS_DIR / str(application_id), ignore_errors=True)


def _fetch_application(db: sqlite3.Connection, application_id: int) -> dict:
    row = db.execute(f"{APPLICATION_SELECT} WHERE a.id = ?", (application_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="application not found")
    return _with_company_logo(dict(row))


def _application_values(body: ApplicationIn | ApplicationUpdate) -> list:
    data = body.model_dump()
    for field in ("applied_date", "next_step_date"):
        if data[field] is not None:
            data[field] = data[field].isoformat()
    return [data[col] for col in APPLICATION_COLUMNS]


def _ensure_applied(db: sqlite3.Connection, job_id: int) -> None:
    """Shared by jobs-PATCH(applied) and the applications POST/PUT applied
    paths: creates or promotes the application, stamps applied_date, flips the
    job, and logs an 'applied' job-activity for every real transition INTO
    applied. A row already past applied (screen/offer/…) is never demoted.

    The log used to fire at most once per JOB — the guard asked "does this job
    already have an 'applied' activity?" — which silently swallowed every
    re-apply after a revert: reverting deliberately keeps the old row as
    history, so the guard always found it and skipped. The condition is the
    job's own status now, because that is the thing a revert actually resets.
    Apply → revert → re-apply logs twice; a no-op PUT on an already-applied
    row still logs nothing.

    CALLERS MUST NOT flip jobs.status before calling this. The pre-flip read
    below is the only thing that distinguishes a real transition from a no-op,
    and this function owns the flip itself (see update_job_status)."""
    today = date.today().isoformat()
    job = db.execute(
        """SELECT status, title, fit_score,
                  json_extract(score_detail, '$.model_score') AS model_score
           FROM jobs WHERE id = ?""",
        (job_id,),
    ).fetchone()
    existing = db.execute(
        "SELECT id, status, applied_date FROM applications WHERE job_id = ?", (job_id,)
    ).fetchone()
    if existing is None:
        db.execute(
            """INSERT INTO applications (job_id, applied_date, status, created_at, updated_at)
               VALUES (?, ?, 'applied', datetime('now'), datetime('now'))""",
            (job_id, today),
        )
    elif existing["status"] == "drafting" or existing["applied_date"] is None:
        db.execute(
            """UPDATE applications
               SET status = CASE WHEN status = 'drafting' THEN 'applied' ELSE status END,
                   applied_date = COALESCE(applied_date, ?),
                   updated_at = datetime('now')
               WHERE id = ?""",
            (today, existing["id"]),
        )
    db.execute("UPDATE jobs SET status = 'applied' WHERE id = ?", (job_id,))
    if job["status"] != "applied":
        # fit_score/model_score are the DECISION-TIME scores (see the
        # dismissal insert in update_job_status) — rescores rewrite the job
        # row, so this activity is the only durable record of what the board
        # said when the user acted on it.
        db.execute(
            """INSERT INTO activities (entity_type, entity_id, date, type, content)
               VALUES ('job', ?, ?, 'applied', ?)""",
            (job_id, today, json.dumps({
                "title": job["title"], "fit_score": job["fit_score"],
                "model_score": job["model_score"],
            })),
        )


@app.get("/api/applications")
async def list_applications(db: sqlite3.Connection = Depends(get_db)) -> list[dict]:
    # The frontend owns pipeline grouping; id order ≈ creation order.
    rows = db.execute(f"{APPLICATION_SELECT} ORDER BY a.id").fetchall()
    return [_with_company_logo(dict(row)) for row in rows]


@app.get("/api/applications/{application_id}")
async def get_application(
    application_id: int, db: sqlite3.Connection = Depends(get_db)
) -> dict:
    return _fetch_application(db, application_id)


@app.post("/api/applications", status_code=201)
async def create_application(
    body: ApplicationIn, db: sqlite3.Connection = Depends(get_db)
) -> dict:
    if db.execute("SELECT 1 FROM jobs WHERE id = ?", (body.job_id,)).fetchone() is None:
        raise HTTPException(status_code=400, detail=errors.fmt(errors.JOB_GONE))
    existing = db.execute(
        "SELECT id FROM applications WHERE job_id = ?", (body.job_id,)
    ).fetchone()
    if existing is not None:
        # 409, not silent return: the frontend knows application_id from the
        # job payload, so a duplicate POST means a stale cache or a bug. The
        # existing id rides the structured detail (like the add-job 409), not
        # the sentence.
        raise HTTPException(
            status_code=409,
            detail={
                "message": errors.fmt(errors.APPLICATION_EXISTS),
                "application_id": existing["id"],
            },
        )
    placeholders = ", ".join("?" for _ in APPLICATION_COLUMNS)
    cursor = db.execute(
        f"""INSERT INTO applications (job_id, {', '.join(APPLICATION_COLUMNS)},
                                      created_at, updated_at)
            VALUES (?, {placeholders}, datetime('now'), datetime('now'))""",
        [body.job_id, *_application_values(body)],
    )
    # No status-change activity on create (intentional asymmetry with PUT):
    # the UI only creates at drafting/applied, and creation isn't a "change" —
    # the applied case logs via _ensure_applied's job-level row.
    if body.status == "applied":
        _ensure_applied(db, body.job_id)
    db.commit()
    return _fetch_application(db, cursor.lastrowid)


@app.put("/api/applications/{application_id}")
async def update_application(
    application_id: int, body: ApplicationUpdate, db: sqlite3.Connection = Depends(get_db)
) -> dict:
    row = db.execute(
        "SELECT job_id, status FROM applications WHERE id = ?", (application_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="application not found")
    assignments = ", ".join(f"{col} = ?" for col in APPLICATION_COLUMNS)
    db.execute(
        f"UPDATE applications SET {assignments}, updated_at = datetime('now') WHERE id = ?",
        [*_application_values(body), application_id],
    )
    if body.status == "applied":
        _ensure_applied(db, row["job_id"])
    # Log every real status transition to the timeline EXCEPT the genuine
    # first-apply promotion, which _ensure_applied's job-level 'applied' row
    # already represents. Demotions back into applied (screen/offer/… → applied)
    # and re-instates (rejected → applied) DO log — _ensure_applied adds nothing
    # new for them, and they'd otherwise re-bucket silently.
    if body.status != row["status"] and not (
        body.status == "applied" and row["status"] in ("drafting", None)
    ):
        db.execute(
            """INSERT INTO activities (entity_type, entity_id, date, type, content)
               VALUES ('application', ?, ?, 'status', ?)""",
            (
                application_id,
                date.today().isoformat(),
                json.dumps({"from": row["status"], "to": body.status}),
            ),
        )
    db.commit()
    return _fetch_application(db, application_id)


@app.delete("/api/applications/{application_id}")
async def delete_application(
    application_id: int, db: sqlite3.Connection = Depends(get_db)
) -> dict:
    row = db.execute(
        "SELECT job_id FROM applications WHERE id = ?", (application_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="application not found")
    job = db.execute(
        "SELECT status, title FROM jobs WHERE id = ?", (row["job_id"],)
    ).fetchone()
    for table in ("activities", "reminders"):
        db.execute(
            f"DELETE FROM {table} WHERE entity_type = 'application' AND entity_id = ?",
            (application_id,),
        )
    _delete_tailorings(db, [application_id])
    db.execute("DELETE FROM applications WHERE id = ?", (application_id,))
    # The job's 'applied' activity stays (history). Only an applied job
    # reverts to active — dismissed/closed jobs keep their status.
    db.execute(
        "UPDATE jobs SET status = 'active' WHERE id = ? AND status = 'applied'",
        (row["job_id"],),
    )
    # …and the reversal gets its own row, JOB-scoped: the loop above wipes every
    # application-scoped activity, so an application-scoped record of the delete
    # would delete itself. Leaving only the old 'applied' row is what made a
    # discarded application read as still submitted.
    if job is not None and job["status"] == "applied":
        db.execute(
            """INSERT INTO activities (entity_type, entity_id, date, type, content)
               VALUES ('job', ?, ?, 'unapplied', ?)""",
            (
                row["job_id"],
                date.today().isoformat(),
                json.dumps({"title": job["title"], "to": "active"}),
            ),
        )
    db.commit()
    return {"deleted": application_id}


@app.post("/api/refresh")
async def trigger_refresh(
    response: Response,
    body: RefreshIn | None = None,
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    if ats_refresh.is_running():
        return {"running": True}
    if body is not None and body.scope == "failed":
        # Bulk retry of failing boards only. Connectable companies only — a
        # manual/no-slug company can carry an error status but nothing can
        # re-fetch it, so pre-stamping it 'checking' would strand it there.
        marks = ",".join("?" * len(ADAPTERS))
        ids = [
            row["id"]
            for row in db.execute(
                f"SELECT id FROM companies WHERE ats_last_status LIKE 'error:%' "
                f"AND ats_type IN ({marks}) AND ats_slug IS NOT NULL ORDER BY id",
                tuple(ADAPTERS),
            )
        ]
        if not ids:
            return {"none": True}
        # Pre-stamp so the Companies list shows live per-row progress through
        # the existing 'checking' plumbing; _run selects by these explicit ids
        # (a status-based selection would no longer match).
        now = _utc_now()
        db.executemany(
            "UPDATE companies SET ats_last_status = 'checking', ats_last_checked = ? "
            "WHERE id = ?",
            [(now, company_id) for company_id in ids],
        )
        db.commit()
        task = asyncio.create_task(ats_refresh.run_refresh(company_ids=ids))
        app.state.refresh_task = task  # keep a reference so the task isn't GC'd
        response.status_code = 202
        return {"started": True, "ids": ids}
    # run_refresh opens its own connection — never the request-scoped one,
    # which is closed long before the run finishes.
    task = asyncio.create_task(ats_refresh.run_refresh())
    app.state.refresh_task = task  # keep a reference so the task isn't GC'd
    response.status_code = 202
    return {"started": True}


@app.post("/api/companies/{company_id}/refresh")
async def trigger_company_refresh(
    company_id: int, response: Response, db: sqlite3.Connection = Depends(get_db)
) -> dict:
    """Re-fetch ONE company's existing board on demand (no re-detection) — the
    per-company counterpart to POST /api/refresh. Non-blocking: stamp 'checking'
    and fire the pull in the background so the detail/list polls it to settle
    like a freshly-added company."""
    row = db.execute(
        "SELECT ats_type, ats_slug, ats_last_status FROM companies WHERE id = ?",
        (company_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="company not found")
    # Manual / undetected companies aren't on a board we can poll (manual is not
    # an ADAPTERS key), so there's nothing to re-fetch.
    if row["ats_type"] not in ADAPTERS or not row["ats_slug"]:
        raise HTTPException(status_code=400, detail="company has no connectable ATS board")
    if row["ats_last_status"] == "checking":
        return {"checking": True}  # already onboarding/refreshing — let the poll finish
    if ats_refresh.is_full_refresh_running():
        return {"running": True}  # a full refresh is mid-run and will cover this board
    # NB: only a FULL refresh short-circuits here. Another single-board refresh
    # holds _refresh_lock too, but it doesn't cover THIS board — so we fall
    # through, stamp 'checking', and let the spawned task queue behind it.
    db.execute(
        "UPDATE companies SET ats_last_status = 'checking', ats_last_checked = ? WHERE id = ?",
        (_utc_now(), company_id),
    )
    db.commit()
    _spawn_company_refresh(company_id)
    response.status_code = 202
    return {"started": True}


@app.post("/api/companies/{company_id}/detect")
async def trigger_company_detect(
    company_id: int, response: Response, db: sqlite3.Connection = Depends(get_db)
) -> dict:
    """Re-run ATS detection on demand — the recovery affordance for a company
    whose check found nothing ('none: …') or failed. Unlike /refresh it needs
    no connected board, just a URL to probe; detection that resolves also does
    the initial pull, exactly like add-time onboarding."""
    row = db.execute(
        "SELECT website, careers_url, ats_last_status FROM companies WHERE id = ?",
        (company_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="company not found")
    if not (row["website"] or row["careers_url"]):
        raise HTTPException(
            status_code=400,
            detail="no website or careers URL to check — add one first",
        )
    if row["ats_last_status"] == "checking":
        return {"checking": True}  # already probing/refreshing — let the poll finish
    db.execute(
        "UPDATE companies SET ats_last_status = 'checking', ats_last_checked = ? WHERE id = ?",
        (_utc_now(), company_id),
    )
    db.commit()
    _spawn_onboarding(company_id)
    response.status_code = 202
    return {"started": True}


@app.post("/api/companies/careers-preview")
async def preview_company_careers(body: CareersPreviewIn) -> dict:
    """Derive a careers/board URL from a website WITHOUT touching the database —
    the pre-add helper the wizard and the add-company flow call to fill the
    Careers field before a company row exists. Runs the same robots-respecting
    discovery as add-time detection (detect_company writes nothing itself), then
    returns the public board URL detection would have backfilled. No API key, no
    persistence. The outbound probes are identical to the add-time check
    (PRIVACY.md's add-company detection entry), just fired one step earlier."""
    if not (body.website or body.careers_url):
        raise HTTPException(
            status_code=400,
            detail="no website or careers URL to check",
        )
    # detect_company reads id/name/website/careers_url by key — a plain dict
    # stands in for the sqlite Row an added company would provide. id is only
    # echoed into its result envelope (unused here), so a placeholder is fine.
    row = {
        "id": None,
        "name": body.name or "",
        "website": body.website,
        "careers_url": body.careers_url,
    }
    async with httpx.AsyncClient(
        timeout=ats_detect.TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": ats_detect.USER_AGENT},
    ) as client:
        result = await ats_detect.detect_company(client, row)
    ats_type = result["ats_type"]
    return {
        "found": bool(ats_type),
        "ats_type": ats_type,
        "careers_url": ats_patterns.public_board_url(ats_type, result["ats_slug"])
        if ats_type
        else None,
    }


# A connected company's job list is "stale" if it pulled 0 jobs on its last
# successful run or hasn't been re-checked within this window (twice-daily
# refresh → a full day missed). Surfaces silent adapter breakage that a plain
# "ok:" status hides. Tunable.
STALE_REFRESH_HOURS = 24


@app.get("/api/refresh/status")
async def refresh_status(db: sqlite3.Connection = Depends(get_db)) -> dict:
    row = db.execute("SELECT value FROM settings WHERE key = 'last_refresh'").fetchone()
    # Manual companies never get an ats_last_status written by the refresh
    # pipeline, so no ats_type filter is needed here.
    errors = db.execute(
        """SELECT id AS company_id, name, ats_last_status, ats_last_checked
           FROM companies WHERE ats_last_status LIKE 'error:%' ORDER BY name"""
    ).fetchall()
    # Companies with no connectable ATS (marked manual, or detection found none):
    # never auto-pulled, so the user must check them by hand. Surfaced on Today.
    no_ats = db.execute(
        """SELECT id AS company_id, name FROM companies
           WHERE ats_type IS NULL OR ats_type = 'manual' ORDER BY name"""
    ).fetchall()
    # Stale / silently-broken job lists: a company with a connectable adapter
    # that pulled 0 jobs on its last successful run ("empty") or hasn't been
    # re-checked within the window ("not_refreshed"). 'error:' adapters live in
    # adapter_errors; 'checking' is mid-onboard; never-checked is pending.
    adapter_types = tuple(ADAPTERS)
    stale_cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=STALE_REFRESH_HOURS)
    ).isoformat(timespec="seconds")
    stale_rows = db.execute(
        f"""SELECT id AS company_id, name, ats_last_status, ats_last_checked
            FROM companies
            WHERE ats_type IN ({",".join("?" * len(adapter_types))})
              AND ats_slug IS NOT NULL
              AND COALESCE(ats_last_status, '') NOT LIKE 'error:%'
              AND COALESCE(ats_last_status, '') != 'checking'
              AND (ats_last_status = 'ok: 0 matched'
                   OR (ats_last_checked IS NOT NULL AND ats_last_checked < ?))
            ORDER BY name""",
        (*adapter_types, stale_cutoff),
    ).fetchall()
    stale = [
        {**dict(r), "reason": "empty" if r["ats_last_status"] == "ok: 0 matched" else "not_refreshed"}
        for r in stale_rows
    ]
    # Boards being individually refreshed right now (the per-board ↻ or add-time
    # onboarding stamp 'checking'; a full refresh never does). Names the in-flight
    # board(s) for Today's progress bar.
    checking = db.execute(
        "SELECT id AS company_id, name FROM companies "
        "WHERE ats_last_status = 'checking' ORDER BY name"
    ).fetchall()
    rescore = db.execute(
        "SELECT value FROM settings WHERE key = 'last_rescore'"
    ).fetchone()
    report = db.execute(
        "SELECT value FROM settings WHERE key = 'last_scoring_report'"
    ).fetchone()
    refresh_report = db.execute(
        "SELECT value FROM settings WHERE key = 'last_refresh_report'"
    ).fetchone()
    # Set only when the last refresh was a total connectivity outage (offline /
    # DNS / asleep): {at, reason, attempted}. Cleared by the next run that
    # reaches the internet. The UI shows one calm "couldn't reach any board"
    # banner instead of flagging every company as failing.
    outage = db.execute(
        "SELECT value FROM settings WHERE key = 'last_refresh_error'"
    ).fetchone()
    # How many companies can actually be pulled (a real adapter + a slug). Zero
    # means a refresh is a no-op that would only stamp last_refresh — the client
    # uses this to skip the day-one auto-refresh so the board stays calm (#34).
    connectable = db.execute(
        f"SELECT COUNT(*) FROM companies "
        f"WHERE ats_type IN ({','.join('?' * len(adapter_types))}) AND ats_slug IS NOT NULL",
        adapter_types,
    ).fetchone()[0]
    return {
        "last_refresh": row["value"] if row else None,
        "connectable": connectable,
        "last_rescore": rescore["value"] if rescore else None,
        "running": ats_refresh.is_running(),
        "refresh_error": json.loads(outage["value"]) if outage and outage["value"] else None,
        "adapter_errors": [dict(r) for r in errors],
        "no_ats": [dict(r) for r in no_ats],
        "stale": stale,
        "checking": [dict(r) for r in checking],
        "scoring_progress": ats_refresh.SCORING_PROGRESS,
        "scoring_report": json.loads(report["value"]) if report and report["value"] else None,
        "refresh_progress": ats_refresh.REFRESH_PROGRESS,
        "refresh_report": json.loads(refresh_report["value"]) if refresh_report and refresh_report["value"] else None,
        "usage": usage.read_usage_totals(db),
    }


@app.get("/api/backup/status")
async def backup_status() -> dict:
    if not BACKUP_STATUS_PATH.is_file():
        return {"present": False}
    try:
        contents = json.loads(BACKUP_STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"present": True, "result": "failed", "detail": "status file unreadable"}
    return {"present": True, **contents}


# Settings: only user-editable keys are exposed; schema_version/last_refresh
# stay internal. Values are JSON in the table, decoded at the API boundary.

# key -> expected JSON shape. SettingIn accepts any JSON value (the model is
# shared), so the shape check lives here: a string written where a list
# belongs doesn't fail the PUT — it 500s some LATER endpoint (str.append on
# the ignore list) or misbehaves quietly (list("text") seeds one LinkedIn
# title per character).
EDITABLE_SETTINGS = {
    "workday_search_terms": list,
    "linkedin_title_defaults": list,
    "dismiss_reasons": list,
    "contact_sources": list,
    "title_keywords": list,
    "title_exclude_keywords": list,
    "suggestions_ignored": list,
    "reminder_suggestions_ignored": list,
    "notify_popups": bool,
    "api_key_declined": bool,  # explicit "I don't want a key" — completes the api_key setup step
    "onboarding_tracker_dismissed": bool,  # "I'm set" — hide the persistent Setup N/total pill
}


def _setting_value(db: sqlite3.Connection, key: str):
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row and row["value"] else []


def _set_setting(db: sqlite3.Connection, key: str, value) -> None:
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )
    db.commit()


def _clear_setting(db: sqlite3.Connection, key: str) -> None:
    db.execute("DELETE FROM settings WHERE key = ?", (key,))
    db.commit()


# The Anthropic API key: the one secret the app holds. Managed entirely through
# apikey (DATA_DIR/.env) — never stored in the DB, never echoed back. The GET
# returns status only (configured/masked/source/editable); the key itself never
# leaves the machine and never rides an API response. These specific routes must
# precede /api/settings/{key} below, or "api-key" matches that catch-all first.


@app.get("/api/settings/api-key")
async def get_api_key(db: sqlite3.Connection = Depends(get_db)) -> dict:
    st = apikey.status()
    # Surface whether the configured key last tested 401, so callers can read a
    # rejected key as unusable rather than "AI on" (#33). Additive — the key
    # itself still never leaves the machine.
    st["rejected"] = bool(st["configured"]) and _setting_value(db, "api_key_test_verdict") == "rejected"
    return st


@app.put("/api/settings/api-key")
async def put_api_key(body: ApiKeyIn, db: sqlite3.Connection = Depends(get_db)) -> dict:
    try:
        result = apikey.write_key(body.key)
    except ValueError as exc:  # whitespace/control chars slipped past NonEmptyStr
        raise HTTPException(status_code=422, detail=str(exc))
    # A newly-saved key is untested — drop any prior 401 verdict so it doesn't
    # outlive the key it judged (#33).
    _clear_setting(db, "api_key_test_verdict")
    return result


@app.delete("/api/settings/api-key")
async def delete_api_key(db: sqlite3.Connection = Depends(get_db)) -> dict:
    result = apikey.clear_key()
    _clear_setting(db, "api_key_test_verdict")
    return result


@app.post("/api/settings/api-key/test")
async def test_api_key(db: sqlite3.Connection = Depends(get_db)) -> dict:
    """Explicit, user-initiated key check: one minimal call to api.anthropic.com
    with the user's own key. Never automatic, never on page load — the button in
    Settings is the only caller. 503 before any network when no key is set (the
    only path the keyless test suite reaches). Returns {ok, error}, distinguishing
    a rejected key from a network failure so the UI can say which it was.

    The verdict is persisted so readiness can demote a saved-but-rejected key
    (#33): only a pass or a 401 is recorded — a network/status failure says
    nothing about the key's validity, so it leaves any prior verdict alone."""
    if not apikey.is_configured():
        # The same actionable line every other keyless 503 uses — this one
        # said "no API key configured" while its siblings pointed at Settings.
        raise HTTPException(status_code=503, detail=apikey.MISSING_MESSAGE)
    from anthropic import (
        APIConnectionError,
        APIStatusError,
        AsyncAnthropic,
        AuthenticationError,
    )

    client = AsyncAnthropic(max_retries=0)  # a probe: fail fast, don't stack backoff
    try:
        await client.messages.create(
            model=aicfg.PING_MODEL,  # the cheapest tier — this is a liveness ping
            max_tokens=1,
            messages=[{"role": "user", "content": "ping"}],
        )
        _set_setting(db, "api_key_test_verdict", "ok")
        return {"ok": True, "error": None}
    except AuthenticationError:
        _set_setting(db, "api_key_test_verdict", "rejected")
        return {"ok": False, "error": "The key was rejected (401). Check it and try again."}
    except APIConnectionError:
        return {"ok": False, "error": "Couldn't reach api.anthropic.com. Check your connection."}
    except APIStatusError as exc:
        return {
            "ok": False,
            "error": errors.describe_provider_status(
                exc.status_code,
                getattr(exc, "message", "") or str(exc),
                subject="api.anthropic.com",
                billing="Add credit at console.anthropic.com (Plans & Billing)",
            ),
        }


# Per-task AI model selection (Providers Tier 1): one `ai_models` settings row,
# two override axes. Dedicated routes rather than EDITABLE_SETTINGS because the
# values are a closed vocabulary the generic list/bool shape check can't
# express — and like api-key, these must precede the /api/settings/{key}
# catch-all or "ai-models" matches it first.


def _ai_models_payload(db: sqlite3.Connection) -> dict:
    return {
        **aicfg.read_overrides(db),
        "remembered": aicfg.read_remembered(db),
        "models": aicfg.MODELS,
        "defaults": {
            axis: {task: aicfg.DEFAULTS[task] for task in tasks}
            for axis, tasks in aicfg.AXES.items()
        },
        "calibrated_scoring_model": aicfg.CALIBRATED_SCORING_MODEL,
    }


@app.get("/api/settings/ai-models")
async def get_ai_models(db: sqlite3.Connection = Depends(get_db)) -> dict:
    return _ai_models_payload(db)


@app.put("/api/settings/ai-models")
async def put_ai_models(
    body: AiModelsIn, db: sqlite3.Connection = Depends(get_db)
) -> dict:
    overrides = {}
    for axis in aicfg.AXES:
        value = getattr(body, axis)
        if isinstance(value, str):  # Tier-1 shorthand: a bare Anthropic id
            value = AxisChoiceIn(provider="anthropic", model=value)
        if value is None or (value.provider == "anthropic" and value.model is None):
            overrides[axis] = None
            continue
        if value.provider == "anthropic":
            if value.model not in aicfg.MODEL_IDS:
                raise HTTPException(
                    status_code=422, detail=errors.fmt(errors.MODEL_UNSUPPORTED)
                )
        else:  # openai_compat: free-text model id against the configured endpoint
            model = (value.model or "").strip()
            if not model or any(ch.isspace() for ch in model) or any(ch < " " for ch in model):
                raise HTTPException(
                    status_code=422, detail=errors.fmt(errors.COMPAT_MODEL_REQUIRED)
                )
            if providers.compat_base_url(db) is None:
                raise HTTPException(
                    status_code=422, detail=errors.fmt(errors.PROVIDER_NOT_CONFIGURED)
                )
            value = AxisChoiceIn(provider="openai_compat", model=model)
        overrides[axis] = {"provider": value.provider, "model": value.model}
    # write_overrides, not _set_setting: the row also carries the provider
    # picker's switch-back memory, which every axis write must update.
    aicfg.write_overrides(db, overrides)
    return _ai_models_payload(db)


# The OpenAI-compatible endpoint (Providers Tier 2): base URL in the
# `ai_providers` settings row (configuration-as-disclosure), key in
# DATA_DIR/.env beside the Anthropic one (a secret; never in the DB, never in
# a response). Like api-key and ai-models, these must precede the
# /api/settings/{key} catch-all below.


@app.get("/api/settings/ai-providers")
async def get_ai_providers(db: sqlite3.Connection = Depends(get_db)) -> dict:
    return providers.compat_status(db)


@app.put("/api/settings/ai-providers")
async def put_ai_providers(
    body: AiProvidersIn, db: sqlite3.Connection = Depends(get_db)
) -> dict:
    try:
        base_url = providers.validate_base_url(body.base_url)
    except ValueError:
        raise HTTPException(
            status_code=422, detail=errors.fmt(errors.PROVIDER_URL_INVALID)
        )
    if body.api_key is not None:
        if body.api_key == "":
            apikey.clear_env_value(providers.COMPAT_ENV_KEY)
        else:
            try:
                apikey.write_env_value(providers.COMPAT_ENV_KEY, body.api_key)
            except ValueError as exc:  # whitespace/control chars
                raise HTTPException(status_code=422, detail=str(exc))
    _set_setting(db, providers.SETTING_KEY, {"openai_compat": {"base_url": base_url}})
    return providers.compat_status(db)


@app.delete("/api/settings/ai-providers")
async def delete_ai_providers(db: sqlite3.Connection = Depends(get_db)) -> dict:
    """Remove the endpoint config and its key. Axes still pointing at the
    compat provider are deliberately left in place — the runtime guards (503,
    scoring skip) carry that drift with an actionable message, and re-adding
    the endpoint restores them untouched."""
    _clear_setting(db, providers.SETTING_KEY)
    apikey.clear_env_value(providers.COMPAT_ENV_KEY)
    return providers.compat_status(db)


@app.post("/api/settings/ai-providers/test")
async def test_ai_providers(db: sqlite3.Connection = Depends(get_db)) -> dict:
    """Explicit, user-initiated endpoint check: one zero-token GET /models to
    the configured base URL (nothing else is ever contacted). Never automatic,
    never on page load — the button in Settings is the only caller. 503 before
    any network when no endpoint is configured (the only path the endpointless
    test suite reaches). The model list rides back to feed the free-text model
    field's suggestions."""
    base_url = providers.compat_base_url(db)
    if base_url is None:
        raise HTTPException(status_code=503, detail=providers.MISSING_ENDPOINT_MESSAGE)
    try:
        result = await oaicompat.probe(base_url, os.environ.get(providers.COMPAT_ENV_KEY))
    except oaicompat.AuthenticationError:
        return {"ok": False, "error": "The key was rejected (401). Check it and try again.", "models": []}
    except oaicompat.APIConnectionError:
        return {"ok": False, "error": f"Couldn't reach {base_url}. Check the URL and that the server is running.", "models": []}
    except oaicompat.APIStatusError as exc:
        # oaicompat stringifies as "endpoint returned NNN: <excerpt>"; hand the
        # composer just the excerpt so it isn't wrapped in a second status line.
        excerpt = str(exc).split(": ", 1)[1] if ": " in str(exc) else ""
        return {
            "ok": False,
            "error": errors.describe_provider_status(
                exc.status_code, excerpt, subject="The endpoint"
            ),
            "models": [],
        }
    return {"ok": True, "error": None, "models": result["models"]}


# The scheduler control (see jshq.schedule): times live in the `schedule`
# settings row — the one source of truth the CLI reads too — while
# installed-ness is always read live from the OS, never stored. /api/schedule
# doesn't collide with the /api/settings/{key} catch-all, but it lives here
# with the other bespoke settings routes on purpose.


@app.get("/api/schedule")
async def get_schedule(db: sqlite3.Connection = Depends(get_db)) -> dict:
    return schedule.status(db)


@app.put("/api/schedule")
async def put_schedule(
    body: ScheduleIn, db: sqlite3.Connection = Depends(get_db)
) -> dict:
    try:
        times = {
            "refresh": schedule.parse_times(body.refresh),
            "backup": schedule.parse_times(body.backup),
        }
    except schedule.ScheduleError as exc:
        raise HTTPException(
            status_code=422, detail=errors.fmt(errors.SCHEDULE_TIME_INVALID, str(exc))
        )
    schedule.write_times(db, times)
    return schedule.status(db)


@app.post("/api/schedule/install")
async def install_schedule(db: sqlite3.Connection = Depends(get_db)) -> dict:
    """Write and load the OS scheduler entries for the stored times.
    Idempotent — a re-install replaces, never duplicates."""
    result = schedule.install(schedule.read_times(db))
    if not result["supported"]:
        raise HTTPException(status_code=422, detail=errors.fmt(errors.SCHEDULE_UNSUPPORTED))
    if not result["ok"]:
        raise HTTPException(
            status_code=500,
            detail=errors.fmt(errors.SCHEDULE_APPLY_FAILED, result["error"]),
        )
    return schedule.status(db)


@app.post("/api/schedule/uninstall")
async def uninstall_schedule(db: sqlite3.Connection = Depends(get_db)) -> dict:
    result = schedule.uninstall()
    if not result["supported"]:
        raise HTTPException(status_code=422, detail=errors.fmt(errors.SCHEDULE_UNSUPPORTED))
    if not result["ok"]:
        raise HTTPException(
            status_code=500,
            detail=errors.fmt(errors.SCHEDULE_APPLY_FAILED, result["error"]),
        )
    return schedule.status(db)


@app.get("/api/settings/{key}")
async def get_setting(key: str, db: sqlite3.Connection = Depends(get_db)) -> dict:
    if key not in EDITABLE_SETTINGS:
        raise HTTPException(status_code=404, detail="setting not found")
    return {"key": key, "value": _setting_value(db, key)}


@app.put("/api/settings/{key}")
async def put_setting(
    key: str, body: SettingIn, db: sqlite3.Connection = Depends(get_db)
) -> dict:
    if key not in EDITABLE_SETTINGS:
        raise HTTPException(status_code=404, detail="setting not found")
    expected = EDITABLE_SETTINGS[key]
    if not isinstance(body.value, expected) or (
        expected is list and not all(isinstance(v, str) for v in body.value)
    ):
        raise HTTPException(
            status_code=422,
            detail=f"{key} must be a {'list of strings' if expected is list else expected.__name__}",
        )
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(body.value)),
    )
    db.commit()
    return {"key": key, "value": body.value}


# Dismissal-driven suggestions: computed on the fly — cheap at
# single-user scale — and one-click accept/ignore, never auto-applied.


def _dismissals(db: sqlite3.Connection) -> list[dict]:
    rows = db.execute(
        """SELECT content FROM activities
           WHERE entity_type = 'job' AND type = 'dismissal' ORDER BY id DESC"""
    ).fetchall()
    out = []
    for row in rows:
        try:
            out.append(json.loads(row["content"] or "{}"))
        except json.JSONDecodeError:
            continue
    return out


def _reminder_suggestions(db: sqlite3.Connection) -> list[dict]:
    # rejected/withdrawn applications no longer need a follow-up; COALESCE
    # keeps legacy NULL-status rows suggesting (NULL NOT IN → NULL → dropped).
    applications = [dict(r) for r in db.execute(
        """SELECT a.id, a.job_id, a.applied_date, j.title || ' @ ' || c.name AS label
           FROM applications a JOIN jobs j ON j.id = a.job_id
           JOIN companies c ON c.id = j.company_id
           WHERE COALESCE(a.status, '') NOT IN ('rejected', 'withdrawn')"""
    ).fetchall()]
    activities = [dict(r) for r in db.execute(
        """SELECT a.id, a.entity_type, a.entity_id, a.date, a.type,
             CASE a.entity_type
               WHEN 'job' THEN (SELECT j.title || ' @ ' || c.name FROM jobs j
                                JOIN companies c ON c.id = j.company_id WHERE j.id = a.entity_id)
               WHEN 'contact' THEN (SELECT name FROM contacts WHERE id = a.entity_id)
               WHEN 'company' THEN (SELECT name FROM companies WHERE id = a.entity_id)
             END AS label
           FROM activities a WHERE a.type IN ('interview', 'meeting')"""
    ).fetchall()]
    reminders = [dict(r) for r in db.execute(
        "SELECT type, entity_type, entity_id FROM reminders"
    ).fetchall()]
    return suggest_reminders(
        applications,
        activities,
        reminders,
        ignored=_setting_value(db, "reminder_suggestions_ignored"),
        today=date.today(),
    )


@app.get("/api/scoring/criteria-doc")
async def get_criteria_doc() -> dict:
    """Read-only view of DATA_DIR/fit_criteria.md for the in-app rubric modal
    (QA pass 1). No path parameter — this serves exactly the one file;
    editing stays file-first (git is the audit trail).

    A plain-English summary of the Tier 1 params is spliced in ahead of the raw
    ```json tier1_params``` block (QA 2026-06-15) so the modal reads as a rubric,
    not a config dump; the raw block stays in the text (the viewer collapses it).
    A malformed doc still serves raw — the summary is a nicety, not a gate."""
    try:
        # criteria.CRITERIA_PATH, not a by-value import: the attribute lookup
        # at call time is what lets tests rebind it in exactly one place (the
        # old dual binding needed a double patch).
        text = criteria.CRITERIA_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="criteria doc not found")
    try:
        summary = render_params_summary(load_criteria(criteria.CRITERIA_PATH).params)
        marker = "```json tier1_params"
        idx = text.find(marker)
        if summary and idx != -1:
            text = text[:idx] + summary + "\n\n" + text[idx:]
    except CriteriaError:
        pass  # broken doc — serve it raw; the rubric still renders
    return {"markdown": text}


@app.get("/api/docs/user-manual")
async def get_user_manual() -> dict:
    """Read-only view of docs/user-manual.md for the in-app Help view (Phase 9).
    No path parameter — serves exactly the one file, mirroring criteria-doc."""
    try:
        text = USER_MANUAL_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="user manual not found")
    return {"markdown": text}


# The voice guide is prose the compose/tailor/refine prompts carry verbatim (Phase
# 3 makes it user-editable — the live copy lives in DATA_DIR, seeded on first run).
VOICE_GUIDE_MAX_BYTES = 200_000  # a cap against abuse, not a schema


@app.get("/api/docs/voice-guide")
async def get_voice_guide() -> dict:
    """The editable voice guide (live DATA_DIR copy, else the shipped default)."""
    return {"markdown": compose.load_voice_guide()}


@app.put("/api/docs/voice-guide")
async def put_voice_guide(body: VoiceGuideIn) -> dict:
    """Write the voice guide to DATA_DIR. No structural validation — it is prose;
    only a byte cap. An empty guide is legal (prompts fall back to base framing)."""
    if len(body.markdown.encode("utf-8")) > VOICE_GUIDE_MAX_BYTES:
        raise HTTPException(status_code=422, detail=errors.fmt(errors.VOICE_GUIDE_TOO_LARGE))
    compose.save_voice_guide(body.markdown)
    return {"markdown": compose.load_voice_guide()}


def _doc_has_markers() -> bool:
    """Whether the live doc actually carries [craft]/[bonus] markers. A doc with
    none has nothing to lose, so an older client may still save it."""
    try:
        c = load_criteria()
    except CriteriaError:
        return False
    return any(i.get("craft") or i.get("bonus_only") for i in c.tier2)


@app.get("/api/scoring/criteria")
async def get_criteria() -> dict:
    """Structured criteria for the Settings editor (Phase 7h): the validated
    Tier 1 params plus the ordered Tier 2 ranked list. (criteria-doc still serves
    raw markdown for the read-only rubric viewer.)"""
    try:
        params, tier2 = read_editable()
        # not `criteria` — that name is the module import at the top of the file
        loaded = load_criteria()
    except CriteriaError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {
        "tier1_params": params,
        "tier2_criteria": tier2,
        # The DERIVED rubric shape, so a client can show which criterion carries
        # the craft axis instead of inferring it. On a marker-less legacy doc
        # these report the inferred positions, which is what actually governs
        # scoring — the tier2_criteria flags alone would say "no markers".
        "craft_criterion": loaded.craft_criterion,
        "no_negative_criteria": sorted(loaded.no_negative_criteria),
        "craft_explicit": loaded.craft_explicit,
    }


@app.put("/api/scoring/criteria")
async def put_criteria(body: CriteriaIn) -> dict:
    """File-first edit of DATA_DIR/fit_criteria.md. Validated + atomic: an invalid
    payload returns 422 and never corrupts the live doc (a broken doc would
    hard-fail all scoring). Existing jobs need a rescore to pick up the change."""
    # craft/bonus_only are round-tripped, not edited. Pydantic cannot tell
    # "client set them false" from "client never heard of them", so a client
    # predating the markers would silently move the craft axis and change
    # craft_lean on every job thereafter. model_fields_set can tell, and a
    # refusal is cheaper than a silent rescore of everything.
    mentions_markers = any(
        {"craft", "bonus_only"} & t.model_fields_set for t in body.tier2_criteria
    )
    try:
        if not mentions_markers and _doc_has_markers():
            raise HTTPException(
                status_code=422,
                detail=(
                    "this payload omits the Tier 2 craft/bonus_only markers, which "
                    "would strip them from the doc and move the craft axis. Re-read "
                    "GET /api/scoring/criteria and send the items back with their "
                    "markers (send craft=false explicitly to clear one)."
                ),
            )
        # size_scale: a machine-owned score_scale block follows the list it
        # prices (hand-authored blocks are never touched — see
        # criteria._scale_sized_text). Same atomic swap as the list itself.
        write_criteria(
            body.tier1_params,
            [t.model_dump() for t in body.tier2_criteria],
            size_scale=True,
        )
    except CriteriaError as exc:
        # Structured for the Settings editor: field/kind name the failing
        # tier1 input and how it failed, so the client anchors an inline
        # error without parsing the message prose (error-audit P1). Both are
        # None for doc-shape errors outside the editor's fields.
        raise HTTPException(
            status_code=422,
            detail={
                "message": errors.fmt(errors.CRITERIA_INVALID, str(exc)),
                "field": exc.field,
                "kind": exc.kind,
            },
        )
    params, tier2 = read_editable()
    return {"tier1_params": params, "tier2_criteria": tier2}


def _persona_payload(loaded) -> dict:
    """The editor's view of the persona: the RAW display_name (None when the doc
    names nobody, so the field renders blank) and the effective domain_label.

    domain_label_is_default marks the neutral fallback ("the roles you are
    searching for") — served when the doc has no persona block, and written
    literally by a name-only save. It is placeholder prose, not user content:
    editors must render it as an empty input, never as a value. Prefilling it
    once concatenated it with a user's real answer and 422'd the length rail."""
    label = loaded.domain_label
    return {
        "display_name": loaded.persona.get("display_name"),
        "domain_label": label,
        "domain_label_is_default": label == criteria.DEFAULT_PERSONA["domain_label"],
    }


@app.get("/api/scoring/persona")
async def get_persona() -> dict:
    """display_name + domain_label for the Settings identity editor. A broken doc
    falls back to defaults so the editor still renders (the criteria error surfaces
    elsewhere); the raw name is returned, not the 'the candidate' resolution."""
    try:
        return _persona_payload(load_criteria())
    except CriteriaError:
        return {
            "display_name": None,
            "domain_label": criteria.DEFAULT_PERSONA["domain_label"],
            "domain_label_is_default": True,
        }


@app.put("/api/scoring/persona")
async def put_persona(body: PersonaIn) -> dict:
    """Rewrite the persona block, file-first and atomic. A blank/whitespace name
    means nobody (anonymous prompts). 422 on an invalid value, leaving the doc
    untouched."""
    name = (body.display_name or "").strip() or None
    try:
        loaded = criteria.write_persona(name, body.domain_label.strip())
    except CriteriaError as exc:
        raise HTTPException(status_code=422, detail=errors.fmt(errors.PERSONA_INVALID, str(exc)))
    return _persona_payload(loaded)


@app.put("/api/scoring/discipline")
async def put_discipline(body: DisciplineIn) -> dict:
    """The wizard's field step: declare the user's field as the in-band discipline
    so scoring targets it rather than the design-specific default. File-first +
    atomic — a 422 leaves the doc untouched. (Richer vocabulary — glosses, a
    functions map — is a hand edit to the taxonomy block in the criteria doc
    itself; no Settings surface edits it.)"""
    try:
        loaded = criteria.write_field(body.field.strip())
    except CriteriaError as exc:
        raise HTTPException(
            status_code=422, detail=errors.fmt(errors.DISCIPLINE_INVALID, str(exc))
        )
    return {"in_band_disciplines": loaded.taxonomy["in_band_disciplines"]}


@app.get("/api/scoring/criteria-example")
async def get_criteria_example() -> dict:
    """The shipped Alex Rivera EXAMPLE criteria, read-only — the wizard's 'see a
    filled-in example' affordance. The user's own live doc is the neutral starter;
    this reference is never written to."""
    return {"markdown": (paths.DEFAULTS_DIR / "fit_criteria.md").read_text(encoding="utf-8")}


# Onboarding (Phase 4): first-run detection + a readiness aggregate driving the
# wizard and the always-visible completeness tracker, plus the raw-exercise
# roadmap store. State (skipped/finished) is a single internal settings row — not
# in EDITABLE_SETTINGS, so it never rides the generic /api/settings/{key} routes.


def _company_count(db: sqlite3.Connection) -> int:
    return db.execute("SELECT COUNT(*) FROM companies").fetchone()[0]


def _onboarding_state(db: sqlite3.Connection) -> dict:
    row = db.execute("SELECT value FROM settings WHERE key = 'onboarding_state'").fetchone()
    return json.loads(row["value"]) if row and row["value"] else {}


def _onboarding_payload(db: sqlite3.Connection) -> dict:
    """First-run flag + persisted state + the readiness breakdown, in one GET so
    the wizard and the tracker fan in once."""
    declined = bool(_setting_value(db, "api_key_declined"))
    # A saved key that last tested 401 is present but useless: demote the step
    # and surface a standing flag so the wizard/board never imply scoring is on
    # (#33). Only meaningful while a key is actually configured.
    api_key_rejected = _setting_value(db, "api_key_test_verdict") == "rejected" and apikey.is_configured()
    readiness = onboarding.build_readiness(
        _company_count(db),
        api_key_declined=declined,
        api_key_rejected=api_key_rejected,
        compat_configured=providers.compat_base_url(db) is not None,
    )
    state = _onboarding_state(db)
    # "I'm set — hide this" on the tracker pill: an acknowledgement, not a readiness
    # change (optional steps left blank on purpose stay done:false forever). It only
    # suppresses the persistent nudge; every count still rides the payload.
    dismissed = bool(_setting_value(db, "onboarding_tracker_dismissed"))
    # A fresh install has never touched onboarding AND has no company yet. Adding a
    # company (the one required step) or dismissing/completing ends first-run.
    first_run = not state and readiness["company_count"] == 0
    return {
        "first_run": first_run,
        "state": state,
        "api_key_declined": declined,
        "api_key_rejected": api_key_rejected,
        "tracker_dismissed": dismissed,
        **readiness,
    }


@app.get("/api/onboarding")
async def get_onboarding(db: sqlite3.Connection = Depends(get_db)) -> dict:
    return _onboarding_payload(db)


@app.put("/api/onboarding")
async def put_onboarding(
    body: OnboardingIn, db: sqlite3.Connection = Depends(get_db)
) -> dict:
    """Record a skip (dismissed) or a finish (completed). Neither gates anything —
    the app is fully usable either way; this only stops the first-run redirect and
    lets the tracker reflect the choice."""
    state = _onboarding_state(db)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if body.dismissed:
        state["dismissed_at"] = now
    if body.completed:
        state["completed_at"] = now
    db.execute(
        "INSERT INTO settings (key, value) VALUES ('onboarding_state', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (json.dumps(state),),
    )
    db.commit()
    return _onboarding_payload(db)


@app.get("/api/onboarding/roadmap")
async def get_roadmap() -> dict:
    """The user's RAW wishlist + fulfillment-matrix inputs (a later pass
    synthesizes them into criteria). {} when none saved yet."""
    return {"roadmap": onboarding.read_roadmap()}


@app.put("/api/onboarding/roadmap")
async def put_roadmap(body: RoadmapIn) -> dict:
    """Persist the raw exercise inputs verbatim (size-capped). No structural
    validation — these are the user's own words, kept for later synthesis."""
    try:
        saved = onboarding.write_roadmap(body.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"roadmap": saved}


@app.get("/api/scoring/vocab")
async def get_vocab(db: sqlite3.Connection = Depends(get_db)) -> dict:
    """Display vocabulary for the frontend: level bands, quadrant and tension
    labels and disciplines.

    The frontend used to hardcode its own copies, which drifted (its level-band
    list had lost `junior`, so junior-banded jobs were unfilterable). Serving
    them keeps one source.

    A broken criteria doc degrades to the code defaults and reports the error
    rather than failing: this is LABELING, and existing rows still have to
    render. Scoring fails loud on the same doc, so nothing is hidden.
    """
    from jshq.ats.normalize import DEFAULT_LEVEL_BANDS
    from jshq.scoring.criteria import DEFAULT_TAXONOMY

    error = None
    try:
        c = load_criteria()
        taxonomy = c.taxonomy
        labels = c.level_band_labels
        bands = [b for b, _ in c.level_bands] + [c.level_band_fallback]
    except CriteriaError as exc:
        error = str(exc)
        taxonomy = DEFAULT_TAXONOMY
        labels = {
            e["band"]: e.get("label") or e["band"].replace("_", " ")
            for e in DEFAULT_LEVEL_BANDS["bands"]
        }
        bands = [e["band"] for e in DEFAULT_LEVEL_BANDS["bands"]] + [
            DEFAULT_LEVEL_BANDS["fallback"]
        ]

    # doc order, first occurrence wins: a band may be listed twice (junior is,
    # so program titles outrank the seniority words above junior/jr/associate)
    seen: dict[str, str] = {}
    for band in bands:
        seen.setdefault(band, labels.get(band, band.replace("_", " ")))

    body = {
        "level_bands": [{"value": b, "label": lb} for b, lb in seen.items()],
        "quadrant_labels": taxonomy["quadrant_labels"],
        "tension_labels": taxonomy["tension_labels"],
        "disciplines": list(taxonomy["disciplines"]),
        "in_band_disciplines": list(taxonomy["in_band_disciplines"]),
        "flag_values": sorted({"below_band", "scope_gap"}),
    }
    if error:
        body["criteria_error"] = error
    return body

@app.get("/api/scoring/geocode")
async def geocode_place(q: str) -> dict:
    """Resolve a free-text place ('Evanston, IL') to {lat, lng, label} for the
    Settings location-radius center field, using the bundled offline US place
    table (Phase 7i). 404 when unresolvable (no recognizable US state, unknown
    town). No DB, no network."""
    hit = geo.resolve(q)
    if hit is None:
        raise HTTPException(
            status_code=404,
            detail=errors.fmt(
                errors.GEOCODE_NO_MATCH,
                f'Couldn\'t find "{q}" — try "Town, ST" (e.g. Madison, WI).',
            ),
        )
    return hit


@app.get("/api/inclusion-rules")
async def get_inclusion_rules(db: sqlite3.Connection = Depends(get_db)) -> dict:
    """Human-readable inclusion rules + the compiled raw arrays carrying
    rule|manual provenance (Phase 7i, decision C). Rules are the source of truth;
    the raw arrays are demoted to a read-mostly 'Advanced' view."""
    try:
        return read_rules(db)
    except CriteriaError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.put("/api/inclusion-rules")
async def put_inclusion_rules(
    body: InclusionRulesIn, db: sqlite3.Connection = Depends(get_db)
) -> dict:
    """Compile the rules down to title_keywords / title_exclude_keywords
    (settings) + location_allowlist (fit_criteria.md) and persist atomically
    (file-first). Manual one-offs survive a recompile. Pydantic rejects bad
    verb/target/empty-term and the invalid (location, exclude) combo as 422s."""
    try:
        return write_rules(
            db,
            [r.model_dump() for r in body.rules],
            body.manual.model_dump(),
        )
    except CriteriaError as exc:
        raise HTTPException(status_code=422, detail=errors.fmt(errors.CRITERIA_INVALID, str(exc)))


@app.post("/api/scoring/rescore")
async def trigger_rescore(response: Response) -> dict:
    """Re-score all active jobs against the current criteria (after an edit).
    Background + non-blocking like /api/refresh — a full rescore can be many
    Haiku calls and would otherwise exceed Apache's proxy timeout."""
    if ats_refresh.is_running():
        return {"running": True}
    task = asyncio.create_task(ats_refresh.run_rescore())
    app.state.rescore_task = task  # keep a reference so the task isn't GC'd
    response.status_code = 202
    return {"started": True}


@app.get("/api/scoring/rescore-estimate")
async def rescore_estimate(db: sqlite3.Connection = Depends(get_db)) -> dict:
    """Tier-1-only dry run (no AI) so the confirm modal can show how many jobs a
    rescore would AI-score and a rough cost. Cost/job is the observed Haiku
    average once usage exists, else a conservative default."""
    try:
        est = estimate_rescore(db)
    except CriteriaError as exc:
        raise HTTPException(status_code=422, detail=errors.fmt(errors.CRITERIA_INVALID, str(exc)))
    binding = aicfg.binding_for(db, "scoring")
    totals = usage.read_usage_totals(db)
    hk = (totals or {}).get("by_model", {}).get(binding.ledger_key)
    if binding.local:
        # A loopback endpoint's calls genuinely cost $0 — say "local", not $0.00
        # of API spend.
        return {**est, "est_cost_usd": 0.0, "pricing": "local"}
    if binding.provider == "openai_compat":
        # Remote endpoint: nothing in PRICES covers it, so any observed cost
        # is $0-by-ignorance and the Haiku-shaped default would be a
        # fabricated number — say "unknown" instead of either.
        return {**est, "est_cost_usd": None, "pricing": "unpriced"}
    avg = (hk["cost"] / hk["calls"]) if hk and hk.get("calls") else usage.DEFAULT_COST_PER_JOB
    return {**est, "est_cost_usd": round(est["to_score"] * avg, 4), "pricing": "estimated"}


@app.get("/api/suggestions")
async def get_suggestions(db: sqlite3.Connection = Depends(get_db)) -> dict:
    return {
        "title_exclude": suggest_exclusions(
            _dismissals(db),
            include_keywords=_setting_value(db, "title_keywords"),
            existing_excludes=_setting_value(db, "title_exclude_keywords"),
            ignored=_setting_value(db, "suggestions_ignored"),
            # Never suggest excluding a tracked company's name (QA pass 2): ATS
            # titles embed the brand, so it otherwise recurs as an n-gram.
            company_names=[r["name"] for r in db.execute("SELECT name FROM companies")],
        ),
        # Semantic JD/role-mismatch proposals (Phase 7i): the pending review
        # queue, surfaced as typed `scoring_rule` suggestions under Scoring.
        "scoring_rule": learned.read_proposals(db),
        "reminders": _reminder_suggestions(db),
    }


@app.post("/api/suggestions/title-exclude")
async def act_on_suggestion(
    body: SuggestionActionIn, db: sqlite3.Connection = Depends(get_db)
) -> dict:
    key = "title_exclude_keywords" if body.action == "accept" else "suggestions_ignored"
    values = _setting_value(db, key)
    if body.keyword not in values:
        values.append(body.keyword)
        db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(values)),
        )
        db.commit()
    return {key: values}


@app.post("/api/suggestions/reminder")
async def act_on_reminder_suggestion(
    body: ReminderSuggestionActionIn, db: sqlite3.Connection = Depends(get_db)
) -> dict:
    """Accept recomputes server-side and creates the reminder from the
    recomputed payload (client fields are never trusted); both actions record
    the key in reminder_suggestions_ignored so it never resurfaces."""
    reminder = None
    if body.action == "accept":
        match = next((s for s in _reminder_suggestions(db) if s["key"] == body.key), None)
        if match is None:
            raise HTTPException(status_code=404, detail=errors.fmt(errors.SUGGESTION_STALE))
        cursor = db.execute(
            """INSERT INTO reminders (title, type, entity_type, entity_id, due_date,
                                      ics_uid, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
            (match["title"], match["type"], match["entity_type"], match["entity_id"],
             match["due_date"], f"{uuid4()}@jobsearchhq"),
        )
        reminder_id = cursor.lastrowid
    ignored = _setting_value(db, "reminder_suggestions_ignored")
    if body.key not in ignored:
        ignored.append(body.key)
        db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            ("reminder_suggestions_ignored", json.dumps(ignored)),
        )
    db.commit()
    if body.action == "accept":
        reminder = _fetch_reminder(db, reminder_id)
    return {"key": body.key, "action": body.action, "reminder": reminder}


# Semantic JD / role-mismatch learned rules (Phase 7i): accepted rules act at
# the SCORING layer (injected into the Haiku prompt), not as title keywords. The
# on-demand proposal endpoint itself lives near /api/compose (it shares
# get_compose_client). See scoring/learned.py.


@app.post("/api/suggestions/scoring-rule")
async def act_on_scoring_proposal(
    body: ScoringRuleActionIn, db: sqlite3.Connection = Depends(get_db)
) -> dict:
    """Accept promotes the proposal into scoring_rules (injected into the scorer
    on the next score/rescore); ignore drops it. Both clear it from the pending
    queue. Returns the updated active + pending lists."""
    proposals = learned.read_proposals(db)
    match = next((p for p in proposals if p.get("id") == body.id), None)
    if match is None:
        raise HTTPException(status_code=404, detail=errors.fmt(errors.PROPOSAL_STALE))
    remaining = [p for p in proposals if p.get("id") != body.id]
    if body.action == "accept":
        rules = learned.read_scoring_rules(db)
        rules.append({
            "id": match["id"],
            "text": match["text"],
            "source": match.get("source", "description"),
            "job_id": match.get("job_id"),
            "created_at": match.get("created_at"),
        })
        learned.write_scoring_rules(db, rules)
    learned.write_proposals(db, remaining)
    db.commit()
    return {
        "id": body.id,
        "action": body.action,
        "rules": learned.read_scoring_rules(db),
        "proposals": remaining,
    }


@app.get("/api/scoring-rules")
async def get_scoring_rules(db: sqlite3.Connection = Depends(get_db)) -> dict:
    """The active learned scoring rules (Phase 7i)."""
    return {"rules": learned.read_scoring_rules(db)}


@app.delete("/api/scoring-rules/{rule_id}")
async def delete_scoring_rule(
    rule_id: str, db: sqlite3.Connection = Depends(get_db)
) -> dict:
    rules = learned.read_scoring_rules(db)
    remaining = [r for r in rules if r.get("id") != rule_id]
    if len(remaining) == len(rules):
        raise HTTPException(status_code=404, detail="scoring rule not found")
    learned.write_scoring_rules(db, remaining)
    db.commit()
    return {"rules": remaining}


@app.get("/api/contacts")
async def list_contacts(db: sqlite3.Connection = Depends(get_db)) -> list[dict]:
    rows = db.execute(
        """SELECT contacts.*, companies.name AS company_name,
                  companies.logo_ext AS company_logo_ext
           FROM contacts LEFT JOIN companies ON companies.id = contacts.company_id
           ORDER BY contacts.name"""
    ).fetchall()
    return [_with_company_logo(dict(row)) for row in rows]


@app.post("/api/contacts", status_code=201)
async def create_contact(body: ContactIn, db: sqlite3.Connection = Depends(get_db)) -> dict:
    _check_contact_company(db, body)
    data = body.model_dump()
    placeholders = ", ".join("?" for _ in CONTACT_COLUMNS)
    cursor = db.execute(
        f"INSERT INTO contacts ({', '.join(CONTACT_COLUMNS)}) VALUES ({placeholders})",
        [data[col] for col in CONTACT_COLUMNS],
    )
    db.commit()
    return _fetch_contact(db, cursor.lastrowid)


@app.put("/api/contacts/{contact_id}")
async def update_contact(
    contact_id: int, body: ContactIn, db: sqlite3.Connection = Depends(get_db)
) -> dict:
    _check_contact_company(db, body)
    data = body.model_dump()
    assignments = ", ".join(f"{col} = ?" for col in CONTACT_COLUMNS)
    cursor = db.execute(
        f"UPDATE contacts SET {assignments}, updated_at = datetime('now') WHERE id = ?",
        [*(data[col] for col in CONTACT_COLUMNS), contact_id],
    )
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="contact not found")
    db.commit()
    return _fetch_contact(db, contact_id)


@app.delete("/api/contacts/{contact_id}")
async def delete_contact(contact_id: int, db: sqlite3.Connection = Depends(get_db)) -> dict:
    if db.execute("SELECT 1 FROM contacts WHERE id = ?", (contact_id,)).fetchone() is None:
        raise HTTPException(status_code=404, detail="contact not found")
    db.execute(
        "DELETE FROM activities WHERE entity_type = 'contact' AND entity_id = ?", (contact_id,)
    )
    db.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))
    db.commit()
    return {"deleted": contact_id}


# Activities: the unified notes table. Until Phase 5 it was written
# only as a side effect (dismissals); now meetings/interviews/calls/notes are
# logged directly and feed the reminder suggestions + future compose context.


ENTITY_TABLES = {"job": "jobs", "contact": "contacts", "company": "companies",
                 "application": "applications"}


def _check_entity(db: sqlite3.Connection, entity_type: str | None, entity_id: int | None) -> None:
    if entity_type is None or entity_type == "general":
        return
    table = ENTITY_TABLES[entity_type]
    if db.execute(f"SELECT 1 FROM {table} WHERE id = ?", (entity_id,)).fetchone() is None:
        raise HTTPException(
            status_code=400,
            detail=errors.fmt(
                errors.ENTITY_GONE, f"That {entity_type} no longer exists — refresh and try again."
            ),
        )


@app.post("/api/activities", status_code=201)
async def create_activity(body: ActivityIn, db: sqlite3.Connection = Depends(get_db)) -> dict:
    _check_entity(db, body.entity_type, body.entity_id)
    when = (body.date or date.today()).isoformat()
    cursor = db.execute(
        """INSERT INTO activities (entity_type, entity_id, date, type, content)
           VALUES (?, ?, ?, ?, ?)""",
        (body.entity_type, body.entity_id, when, body.type, body.content),
    )
    db.commit()
    row = db.execute("SELECT * FROM activities WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return dict(row)


@app.get("/api/activities")
async def list_activities(
    entity_type: str | None = None,
    entity_id: int | None = None,
    types: str | None = None,
    db: sqlite3.Connection = Depends(get_db),
) -> list[dict]:
    clauses, params = [], []
    if entity_type is not None:
        clauses.append("entity_type = ?")
        params.append(entity_type)
    if entity_id is not None:
        clauses.append("entity_id = ?")
        params.append(entity_id)
    if types:
        wanted = [t.strip() for t in types.split(",") if t.strip()]
        clauses.append(f"type IN ({', '.join('?' for _ in wanted)})")
        params.extend(wanted)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = db.execute(
        f"SELECT * FROM activities {where} ORDER BY date DESC, id DESC", params
    ).fetchall()
    return [dict(row) for row in rows]


# Compose: drafts only — all sending is manual, by the user, outside
# the app. Every generated draft is logged as a compose activity so drafts
# feed future context; a regenerated-away draft is still taste signal.


def _interactive_client(db: sqlite3.Connection, task: str):
    """The client for one interactive AI endpoint, resolved through the
    task's axis binding (Tier 2: either provider). 503 with the provider's
    actionable message before any work when it isn't ready; construction is
    lazy inside providers.build_client so the app runs without the anthropic
    package (mirrors scoring.run_scoring).

    2 retries (3 attempts) bounds worst-case latency: exponential backoff on
    a 429/529 storm could otherwise stack past the 300s proxy budget on a
    single interactive tailor/compose call."""
    binding = aicfg.binding_for(db, task)
    if not providers.is_ready(db, binding.provider):
        raise HTTPException(status_code=503, detail=providers.missing_message(binding.provider))
    return providers.build_client(db, binding.provider, max_retries=2)


async def get_compose_client(db: sqlite3.Connection = Depends(get_db)):
    """The writing axis's client (compose/refine/tailor). The name predates
    Tier 2; it survives because seven test files override it by identity."""
    return _interactive_client(db, "compose")


async def get_analysis_client(db: sqlite3.Connection = Depends(get_db)):
    """The analysis axis's client (rule proposals, title suggestions,
    synthesis) — split from get_compose_client so per-axis provider choice
    reaches every endpoint, not just the writing ones."""
    return _interactive_client(db, "learned")


@app.post("/api/compose")
async def compose_draft(
    body: ComposeIn,
    db: sqlite3.Connection = Depends(get_db),
    client=Depends(get_compose_client),
) -> dict:
    context = compose.build_entity_context(db, body.entity_type, body.entity_id)
    if context is None:
        raise HTTPException(
            status_code=404,
            detail=errors.fmt(
                errors.ENTITY_GONE,
                f"That {body.entity_type} no longer exists — refresh and try again.",
            ),
        )
    system = compose.build_system_prompt(
        compose.load_voice_guide(), compose.ai_tells_prompt_block()
    )
    user = compose.build_user_message(body.intent, context, body.instructions, body.question)
    binding = aicfg.binding_for(db, "compose")
    model = binding.model
    try:
        draft, usages = await compose.generate(client, system, user, model)
    # Deliberately broad: anthropic is lazily imported (the app must run
    # without the package), so its typed exceptions can't be caught here.
    except Exception as exc:
        for u in usage.usages_of(exc):
            usage.record_usage(db, binding.ledger_key, u, local=binding.local)
        db.commit()
        _applog.warning("compose draft generation failed: %s", exc)
        raise errors.http_error(502, errors.COMPOSE_FAILED)
    for u in usages:
        usage.record_usage(db, binding.ledger_key, u, local=binding.local)
    content = json.dumps({
        "intent": body.intent,
        "instructions": body.instructions or None,
        "question": body.question or None,
        "draft": draft,
        "model": model,
    })
    cursor = db.execute(
        """INSERT INTO activities (entity_type, entity_id, date, type, content)
           VALUES (?, ?, ?, 'compose', ?)""",
        (body.entity_type, body.entity_id, date.today().isoformat(), content),
    )
    db.commit()
    return {"draft": draft, "model": model, "activity_id": cursor.lastrowid}


@app.post("/api/refine-tells")
async def refine_tells(
    body: RefineTellsIn,
    db: sqlite3.Connection = Depends(get_db),
    client=Depends(get_compose_client),
) -> dict:
    """Opt-in AI-tell scrub of a draft / cover letter. One Sonnet call; returns
    {score, tells_fixed, refined_text}. Records spend; nothing is persisted here
    (the caller drops refined_text back into its editable field)."""
    binding = aicfg.binding_for(db, "refine")
    model = binding.model
    try:
        result, usages = await refine.refine(client, body.text, model)
    # Broad for the same reason as compose: anthropic is lazily imported.
    except Exception as exc:
        for u in usage.usages_of(exc):
            usage.record_usage(db, binding.ledger_key, u, local=binding.local)
        db.commit()
        _applog.warning("refine failed: %s", exc)
        raise errors.http_error(502, errors.REFINE_FAILED)
    for u in usages:
        usage.record_usage(db, binding.ledger_key, u, local=binding.local)
    db.commit()
    return result


@app.post("/api/jobs/{job_id}/scoring-rule-proposal")
async def propose_scoring_rule(
    job_id: int,
    refresh: bool = False,
    db: sqlite3.Connection = Depends(get_db),
    client=Depends(get_analysis_client),
) -> dict:
    """On-demand (Phase 7i): read this job's JD and propose one scoring-layer
    role-mismatch rule. A pending proposal already on file for the job is
    returned without a model call (the per-job cache) unless ?refresh=true."""
    job = db.execute(
        """SELECT jobs.*, companies.name AS company_name
           FROM jobs JOIN companies ON companies.id = jobs.company_id
           WHERE jobs.id = ?""",
        (job_id,),
    ).fetchone()
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")

    proposals = learned.read_proposals(db)
    if not refresh:
        cached = next((p for p in proposals if p.get("job_id") == job_id), None)
        if cached is not None:
            return cached  # cache hit — no model call

    try:
        criteria = load_criteria()
    except CriteriaError as exc:
        raise HTTPException(status_code=422, detail=errors.fmt(errors.CRITERIA_INVALID, str(exc)))
    system = learned.build_proposal_prompt(
        criteria,
        build_dismissal_digest(db),
        [r["text"] for r in learned.read_scoring_rules(db)],
    )
    user = learned.build_user_message(job)
    binding = aicfg.binding_for(db, "learned")
    model = binding.model
    try:
        out, usages = await learned.propose_rule(client, system, user, model)
    # Broad: anthropic is lazily imported, so its typed exceptions can't be caught.
    except Exception as exc:
        for u in usage.usages_of(exc):
            usage.record_usage(db, binding.ledger_key, u, local=binding.local)
        db.commit()
        _applog.warning("scoring-rule proposal failed: %s", exc)
        raise errors.http_error(502, errors.RULE_PROPOSAL_FAILED)
    for u in usages:
        usage.record_usage(db, binding.ledger_key, u, local=binding.local)

    proposal = {
        "id": str(uuid4()),
        "text": out["rule_text"],
        "rationale": out["rationale"],
        "source": "description",
        "job_id": job_id,
        "job_title": job["title"],
        "company": job["company_name"],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    # One proposal per origin job (the per-job cache); replace any prior one.
    proposals = [p for p in proposals if p.get("job_id") != job_id] + [proposal]
    learned.write_proposals(db, proposals)
    db.commit()
    return proposal


@app.post("/api/settings/linkedin-titles/suggest")
async def suggest_linkedin_titles(
    db: sqlite3.Connection = Depends(get_db),
    client=Depends(get_analysis_client),
) -> dict:
    """On-demand: propose adjacent-discipline titles for the LinkedIn role-check
    defaults (the wizard's deterministic derivation can't reach neighbours —
    e.g. UX research for a designer). Suggestions are review-then-add cards in
    Settings → Sourcing; nothing lands in the setting without an explicit Add.
    503 keyless via the dependency, so the keyless suite never goes near it."""
    try:
        criteria = load_criteria()
    except CriteriaError as exc:
        raise HTTPException(status_code=422, detail=errors.fmt(errors.CRITERIA_INVALID, str(exc)))
    existing = list(_setting_value(db, "linkedin_title_defaults") or [])
    system = linkedin_titles.build_prompt(criteria, existing)
    binding = aicfg.binding_for(db, "linkedin_titles")
    model = binding.model
    try:
        suggestions, usages = await linkedin_titles.propose(client, system, existing, model)
    # Broad: anthropic is lazily imported, so its typed exceptions can't be caught.
    except Exception as exc:
        for u in usage.usages_of(exc):
            usage.record_usage(db, binding.ledger_key, u, local=binding.local)
        db.commit()
        _applog.warning("linkedin title suggestion failed: %s", exc)
        raise errors.http_error(502, errors.TITLE_SUGGEST_FAILED)
    for u in usages:
        usage.record_usage(db, binding.ledger_key, u, local=binding.local)
    db.commit()
    return {"suggestions": suggestions}


# Roadmap synthesis (Phase 4's deferred pass): the user's raw onboarding words
# become the reflection prose the scorer leans on. Two transports — a keyed
# Sonnet call, or a copied prompt + a pasted reply — converge on one validated
# proposal parked for explicit preview → apply; the doc is never auto-written.


def _synthesis_available() -> bool:
    wishlist, matrix = synthesis.roadmap_words(onboarding.read_roadmap())
    return bool(wishlist or matrix)


def _criteria_for_synthesis():
    try:
        return load_criteria()
    except CriteriaError as exc:
        raise HTTPException(status_code=422, detail=errors.fmt(errors.CRITERIA_INVALID, str(exc)))


@app.get("/api/scoring/synthesis")
async def get_synthesis(db: sqlite3.Connection = Depends(get_db)) -> dict:
    """The parked draft (or null) + whether there is anything to synthesize."""
    return {"proposal": synthesis.read_proposal(db), "available": _synthesis_available()}


@app.get("/api/scoring/synthesis/prompt")
async def get_synthesis_prompt() -> dict:
    """The keyless transport: the same prompt the keyed call sends, rendered
    for any chat model. Never 503s — no key is the point."""
    crit = _criteria_for_synthesis()
    try:
        system, user = synthesis.build_synthesis_prompt(crit, onboarding.read_roadmap())
    except synthesis.SynthesisError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"prompt": synthesis.render_clipboard_prompt(system, user)}


@app.post("/api/scoring/synthesis")
async def propose_synthesis(
    db: sqlite3.Connection = Depends(get_db), client=Depends(get_analysis_client)
) -> dict:
    crit = _criteria_for_synthesis()
    try:
        system, user = synthesis.build_synthesis_prompt(crit, onboarding.read_roadmap())
    except synthesis.SynthesisError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    binding = aicfg.binding_for(db, "synthesis")
    model = binding.model
    try:
        data, usages = await synthesis.propose(client, system, user, len(crit.tier2), model)
    # Broad: anthropic is lazily imported, so its typed exceptions can't be caught.
    except Exception as exc:
        for u in usage.usages_of(exc):
            usage.record_usage(db, binding.ledger_key, u, local=binding.local)
        db.commit()
        _applog.warning("synthesis failed: %s", exc)
        raise errors.http_error(502, errors.SYNTHESIS_FAILED)
    for u in usages:
        usage.record_usage(db, binding.ledger_key, u, local=binding.local)
    proposal = synthesis.write_proposal(db, "api", model, crit.tier2, data)
    db.commit()
    return {"proposal": proposal, "available": True}


@app.post("/api/scoring/synthesis/reply")
async def submit_synthesis_reply(
    body: SynthesisReplyIn, db: sqlite3.Connection = Depends(get_db)
) -> dict:
    """Keyless paste-back: the reply lands in the same validator and the same
    parked-proposal shape as the keyed path."""
    if len(body.reply.encode("utf-8")) > synthesis.REPLY_MAX_BYTES:
        raise HTTPException(status_code=422, detail="reply is too large to be a synthesis answer")
    crit = _criteria_for_synthesis()
    try:
        data = synthesis.validate_synthesis(body.reply, len(crit.tier2))
    except synthesis.SynthesisError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    proposal = synthesis.write_proposal(db, "paste", None, crit.tier2, data)
    db.commit()
    return {"proposal": proposal, "available": True}


@app.post("/api/scoring/synthesis/apply")
async def apply_synthesis(
    body: SynthesisApplyIn, db: sqlite3.Connection = Depends(get_db)
) -> dict:
    """One atomic write: reflection prose (always) + refined Tier 2 (opt-in).
    Failure keeps both the doc and the parked draft intact."""
    proposal = synthesis.read_proposal(db)
    if proposal is None:
        raise HTTPException(status_code=404, detail="no synthesis draft to apply")
    crit = _criteria_for_synthesis()
    data = proposal["data"]
    merged = None
    if body.apply_tier2:
        # Refinements address criteria by 1-based index, so ANY list change —
        # not just a length change — makes them land on the wrong criterion
        # (a same-length Settings reorder would even move the craft axis).
        # The texts fingerprint catches reorders and rewords; the count check
        # remains for drafts parked before the fingerprint existed.
        current_texts = [item["text"] for item in crit.tier2]
        stale = (
            proposal.get("tier2_texts") != current_texts
            if "tier2_texts" in proposal
            else proposal.get("tier2_count") != len(crit.tier2)
        )
        if stale:
            raise HTTPException(
                status_code=409,
                detail="your ranked list changed since this draft — re-run synthesis",
            )
        merged = [dict(item) for item in crit.tier2]
        for r in data["tier2_refinements"]:
            merged[r["index"] - 1].update(
                text=r["text"], weight=r["weight"], craft=r["craft"], bonus_only=r["bonus_only"]
            )
    # The craft-axis rubric only renders when the doc will actually have a
    # craft item afterwards — otherwise craft_lean is unreachable and the
    # rubric would be a promise the scorer can't keep.
    will_have_craft = (
        any(i.get("craft") for i in merged) if merged is not None
        else crit.craft_criterion is not None
    )
    try:
        body_md = synthesis.render_prose(data, crit, will_have_craft=will_have_craft)
        # size_scale: refined weights change what the rubric sums to, so a
        # machine-owned score_scale block re-sizes in the same atomic write
        # (hand-authored blocks are never touched — criteria._scale_sized_text).
        criteria.write_synthesis_prose(body_md, tier2_criteria=merged, size_scale=True)
    except synthesis.SynthesisError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except CriteriaError as exc:
        raise HTTPException(status_code=422, detail=errors.fmt(errors.CRITERIA_INVALID, str(exc)))
    synthesis.clear_proposal(db)
    db.commit()
    params, tier2 = read_editable()
    return {"tier1_params": params, "tier2_criteria": tier2}


@app.delete("/api/scoring/synthesis")
async def discard_synthesis(db: sqlite3.Connection = Depends(get_db)) -> dict:
    synthesis.clear_proposal(db)
    db.commit()
    return {"proposal": None, "available": _synthesis_available()}


# Tailoring (Phase 7e): the agent proposes, the user approves line
# by line, apply renders versioned PDFs. The master data/resume/content.json is
# never written — tailored content exists only in memory and in the PDFs.

# The tailoring pipeline's versioned artifacts: the PDFs the user sends AND the
# .html render sources beside them — all regenerable, none deletable here.
# Case-insensitive like the macOS/Windows filesystems these paths live on:
# "resume-v1.PDF" resolves to the protected resume-v1.pdf there, so a
# case-sensitive guard was a delete/overwrite bypass (safe_filename catches
# the trailing-dot/space aliases; this catches the case alias).
GENERATED_FILE_RE = re.compile(r"^(resume|cover)-v\d+\.(pdf|html)$", re.IGNORECASE)


def _resume_content_error(exc: "render.ResumeError") -> HTTPException:
    """The four tailoring/render endpoints' shared miss. 422, not 500: the fix
    is a user edit to resume/content.json (seeded on first run), and the
    detail points at the manual section that documents its shape instead of
    leaking a raw OSError with an absolute server path."""
    return HTTPException(
        status_code=422,
        detail=(
            f"resume content problem: {exc}. See 'Resume content file' in the "
            "user manual (Help) for the file's format."
        ),
    )


def _tailoring_payload(row: sqlite3.Row) -> dict:
    payload = dict(row)
    payload["change_plan"] = json.loads(payload["change_plan"] or "[]")
    return payload


def _fetch_tailoring_row(db: sqlite3.Connection, tailoring_id: int) -> sqlite3.Row:
    row = db.execute("SELECT * FROM tailorings WHERE id = ?", (tailoring_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="tailoring not found")
    return row


def _require_pending(row: sqlite3.Row) -> None:
    if row["status"] != "pending":
        raise HTTPException(
            status_code=409, detail=f"tailoring is {row['status']} — only pending ones change"
        )


def _pdf_page_count(pdf_path: Path) -> int:
    """Count page objects in a Chrome-written PDF (its page dictionaries are
    plain text, not in object streams). 0 = couldn't tell; don't warn on it."""
    try:
        data = pdf_path.read_bytes()
    except OSError:
        return 0
    return len(re.findall(rb"/Type\s*/Page\b", data))


@app.post("/api/applications/{application_id}/tailor", status_code=201)
async def tailor_application(
    application_id: int,
    body: TailorIn,
    db: sqlite3.Connection = Depends(get_db),
    client=Depends(get_compose_client),
) -> dict:
    application = _fetch_application(db, application_id)
    pending = db.execute(
        "SELECT id FROM tailorings WHERE application_id = ? AND status = 'pending'",
        (application_id,),
    ).fetchone()
    if pending is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "message": errors.fmt(errors.TAILORING_PENDING),
                "tailoring_id": pending["id"],
            },
        )
    job = db.execute(
        """SELECT jobs.*, companies.name AS company_name
           FROM jobs JOIN companies ON companies.id = jobs.company_id
           WHERE jobs.id = ?""",
        (application["job_id"],),
    ).fetchone()
    if not (job["description_text"] or "").strip():
        raise HTTPException(status_code=409, detail=errors.fmt(errors.JOB_NO_DESCRIPTION))
    try:
        content = render.load_content(render.CONTENT_PATH)
    except render.ResumeError as exc:
        raise _resume_content_error(exc)
    system = tailor.build_system_prompt(
        compose.load_voice_guide(), compose.ai_tells_prompt_block()
    )
    user = tailor.build_user_message(job, content, body.instructions)
    binding = aicfg.binding_for(db, "tailor")
    model = binding.model
    try:
        data, usages = await tailor.generate(client, system, user, model)
    # Broad for the same reason as compose: anthropic is lazily imported.
    except Exception as exc:
        for u in usage.usages_of(exc):
            usage.record_usage(db, binding.ledger_key, u, local=binding.local)
        db.commit()
        _applog.warning("tailoring generation failed: %s", exc)
        raise errors.http_error(502, errors.TAILOR_FAILED)
    for u in usages:
        usage.record_usage(db, binding.ledger_key, u, local=binding.local)
    plan, warnings = tailor.normalize_changes(data["changes"], content)
    cursor = db.execute(
        """INSERT INTO tailorings (application_id, status, analysis, change_plan,
                                   cover_letter, model)
           VALUES (?, 'pending', ?, ?, ?, ?)""",
        (application_id, data["analysis"].strip(), json.dumps(plan),
         data["cover_letter"].strip(), model),
    )
    db.commit()
    return _tailoring_payload(_fetch_tailoring_row(db, cursor.lastrowid)) | {
        "warnings": warnings
    }


@app.get("/api/applications/{application_id}/tailoring")
async def get_tailoring(
    application_id: int, db: sqlite3.Connection = Depends(get_db)
) -> dict:
    """The one row that drives the UI section: pending if any, else the
    latest applied. Discarded rows are history, never returned here."""
    _fetch_application(db, application_id)
    row = db.execute(
        """SELECT * FROM tailorings
           WHERE application_id = ? AND status != 'discarded'
           ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, id DESC
           LIMIT 1""",
        (application_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="no tailoring for this application")
    return _tailoring_payload(row)


@app.patch("/api/tailorings/{tailoring_id}")
async def patch_tailoring(
    tailoring_id: int, body: TailoringPatch, db: sqlite3.Connection = Depends(get_db)
) -> dict:
    row = _fetch_tailoring_row(db, tailoring_id)
    _require_pending(row)
    plan = json.loads(row["change_plan"])
    if body.changes is not None:
        by_id = {change["id"]: change for change in plan}
        for patch in body.changes:
            if patch.id not in by_id:
                raise HTTPException(
                    status_code=400, detail=f"no change with id '{patch.id}'"
                )
            by_id[patch.id]["approved"] = patch.approved
            if patch.new is not None and patch.new.strip():
                by_id[patch.id]["new"] = patch.new.strip()
    cover_letter = row["cover_letter"] if body.cover_letter is None else body.cover_letter
    db.execute(
        """UPDATE tailorings SET change_plan = ?, cover_letter = ?,
                  updated_at = datetime('now') WHERE id = ?""",
        (json.dumps(plan), cover_letter, tailoring_id),
    )
    db.commit()
    return _tailoring_payload(_fetch_tailoring_row(db, tailoring_id))


@app.post("/api/tailorings/{tailoring_id}/apply")
async def apply_tailoring(
    tailoring_id: int, db: sqlite3.Connection = Depends(get_db)
) -> dict:
    row = _fetch_tailoring_row(db, tailoring_id)
    _require_pending(row)
    application_id = row["application_id"]
    plan = json.loads(row["change_plan"])
    try:
        content = render.load_content(render.CONTENT_PATH)
    except render.ResumeError as exc:
        raise _resume_content_error(exc)
    try:
        patched = tailor.apply_changes(content, plan)
    except tailor.TailorError as exc:
        raise HTTPException(status_code=409, detail=f"{exc} — regenerate the tailoring")

    version = (db.execute(
        "SELECT MAX(version) AS v FROM tailorings WHERE application_id = ?",
        (application_id,),
    ).fetchone()["v"] or 0) + 1
    out_dir = APPLICATIONS_DIR / str(application_id)
    resume_pdf = out_dir / f"resume-v{version}.pdf"
    cover_pdf = out_dir / f"cover-v{version}.pdf"
    letter = row["cover_letter"]
    letter_date = _letter_date()
    try:
        # render_pdf blocks on a Chrome subprocess for seconds — keep it off
        # the event loop. A failure here leaves no DB change; a partial PDF
        # on disk is harmless and overwritten on retry.
        await asyncio.to_thread(render.render_pdf, render.build_html(patched), resume_pdf)
        await asyncio.to_thread(
            render.render_pdf, cover.build_cover_html(content, letter, letter_date), cover_pdf
        )
    except render.ResumeError as exc:
        raise HTTPException(status_code=502, detail=f"PDF render failed: {exc}")

    warnings = []
    pages = _pdf_page_count(resume_pdf)
    if pages and pages != 2:
        warnings.append(
            f"resume rendered to {pages} pages (blessed layout is 2) — check before sending"
        )
    approved = sum(1 for change in plan if change.get("approved"))
    db.execute(
        """UPDATE tailorings SET status = 'applied', version = ?,
                  applied_at = datetime('now'), updated_at = datetime('now')
           WHERE id = ?""",
        (version, tailoring_id),
    )
    db.execute(
        """UPDATE applications SET resume_version = ?, cover_note = ?,
                  updated_at = datetime('now') WHERE id = ?""",
        (f"v{version}", letter, application_id),
    )
    # intent/draft keys match compose activities so the existing timeline
    # renderers (compose._activity_line, frontend activityText) just work.
    activity = json.dumps({
        "intent": "tailoring",
        "draft": f"v{version} — {approved} resume change{'s' if approved != 1 else ''}"
                 " + cover letter",
        "tailoring_id": tailoring_id,
        "version": version,
        "files": [resume_pdf.name, cover_pdf.name],
        "model": row["model"],
    })
    db.execute(
        """INSERT INTO activities (entity_type, entity_id, date, type, content)
           VALUES ('application', ?, ?, 'compose', ?)""",
        (application_id, date.today().isoformat(), activity),
    )
    db.commit()
    return {
        "tailoring": _tailoring_payload(_fetch_tailoring_row(db, tailoring_id)),
        "application": _fetch_application(db, application_id),
        "files": [resume_pdf.name, cover_pdf.name],
        "warnings": warnings,
    }


@app.post("/api/tailorings/{tailoring_id}/rerender")
async def rerender_cover(
    tailoring_id: int, body: CoverRerenderIn, db: sqlite3.Connection = Depends(get_db)
) -> dict:
    """Re-render the cover PDF from a hand-edited letter as a new COVER version,
    with no model call — the path for revising an already-applied letter without
    regenerating. Only the cover advances: the resume is unchanged, so it keeps
    its own version (from the last full Apply) and its PDF is left untouched —
    no re-render, no copy. A fresh applied row is inserted (prior versions and
    PDFs stay on disk); applications.resume_version is deliberately NOT bumped,
    so the resume and cover versions can read independently in the UI."""
    row = _fetch_tailoring_row(db, tailoring_id)
    if row["status"] != "applied":
        raise HTTPException(
            status_code=409,
            detail=f"tailoring is {row['status']} — re-render revises an applied letter "
            "(use Apply to render a pending draft)",
        )
    letter = body.cover_letter.strip()
    if not letter:
        raise HTTPException(status_code=422, detail="cover letter is empty")
    if letter == (row["cover_letter"] or "").strip():
        raise HTTPException(
            status_code=400, detail="no changes to the letter — edit the text first"
        )
    application_id = row["application_id"]
    # The resume keeps the version it was last rendered at (the last full Apply);
    # a cover re-render never touches it or its PDF.
    resume_version = db.execute(
        "SELECT resume_version FROM applications WHERE id = ?", (application_id,)
    ).fetchone()["resume_version"]
    try:
        content = render.load_content(render.CONTENT_PATH)
    except render.ResumeError as exc:
        raise _resume_content_error(exc)

    version = (db.execute(
        "SELECT MAX(version) AS v FROM tailorings WHERE application_id = ?",
        (application_id,),
    ).fetchone()["v"] or 0) + 1
    out_dir = APPLICATIONS_DIR / str(application_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    cover_pdf = out_dir / f"cover-v{version}.pdf"
    letter_date = _letter_date()
    try:
        # render_pdf blocks on a Chrome subprocess for seconds — keep it off the
        # event loop. Only the cover is rendered; the resume's existing PDF stands.
        await asyncio.to_thread(
            render.render_pdf, cover.build_cover_html(content, letter, letter_date), cover_pdf
        )
    except render.ResumeError as exc:
        raise HTTPException(status_code=502, detail=f"PDF render failed: {exc}")

    resume_pdf_name = f"resume-{resume_version}.pdf" if resume_version else f"resume-v{version}.pdf"
    cursor = db.execute(
        """INSERT INTO tailorings (application_id, status, analysis, change_plan,
                                   cover_letter, model, version, applied_at)
           VALUES (?, 'applied', ?, ?, ?, ?, ?, datetime('now'))""",
        (application_id, row["analysis"], row["change_plan"], letter, row["model"], version),
    )
    # Stamp the edited letter to cover_note; resume_version is left as-is.
    db.execute(
        """UPDATE applications SET cover_note = ?, updated_at = datetime('now')
           WHERE id = ?""",
        (letter, application_id),
    )
    activity = json.dumps({
        "intent": "tailoring",
        "draft": f"cover v{version} — re-rendered cover letter (manual edit)",
        "tailoring_id": cursor.lastrowid,
        "version": version,
        "files": [resume_pdf_name, cover_pdf.name],
        "model": row["model"],
    })
    db.execute(
        """INSERT INTO activities (entity_type, entity_id, date, type, content)
           VALUES ('application', ?, ?, 'compose', ?)""",
        (application_id, date.today().isoformat(), activity),
    )
    db.commit()
    return {
        "tailoring": _tailoring_payload(_fetch_tailoring_row(db, cursor.lastrowid)),
        "application": _fetch_application(db, application_id),
        "files": [resume_pdf_name, cover_pdf.name],
    }


@app.post("/api/tailorings/{tailoring_id}/discard")
async def discard_tailoring(
    tailoring_id: int, db: sqlite3.Connection = Depends(get_db)
) -> dict:
    row = _fetch_tailoring_row(db, tailoring_id)
    _require_pending(row)
    db.execute(
        "UPDATE tailorings SET status = 'discarded', updated_at = datetime('now') WHERE id = ?",
        (tailoring_id,),
    )
    db.commit()
    return _tailoring_payload(_fetch_tailoring_row(db, tailoring_id))


@app.post("/api/tailorings/{tailoring_id}/chat")
async def chat_tailoring(
    tailoring_id: int,
    body: TailoringChatIn,
    db: sqlite3.Connection = Depends(get_db),
    client=Depends(get_compose_client),
) -> dict:
    """One refinement turn (Phase 7f). The stored thread is
    replayed compactly — prior user texts and assistant replies only — and
    the CURRENT plan/letter snapshot rides in the new turn, so hand edits
    between turns are always honored and old deltas never need replaying.
    A failed turn persists nothing (the user just resends); no activity here,
    mirroring generate — the activity logs at apply."""
    row = _fetch_tailoring_row(db, tailoring_id)
    _require_pending(row)
    application = _fetch_application(db, row["application_id"])
    job = db.execute(
        """SELECT jobs.*, companies.name AS company_name
           FROM jobs JOIN companies ON companies.id = jobs.company_id
           WHERE jobs.id = ?""",
        (application["job_id"],),
    ).fetchone()
    try:
        content = render.load_content(render.CONTENT_PATH)
    except render.ResumeError as exc:
        raise _resume_content_error(exc)
    plan = json.loads(row["change_plan"])
    history = db.execute(
        "SELECT role, content FROM tailoring_messages WHERE tailoring_id = ? ORDER BY id",
        (tailoring_id,),
    ).fetchall()
    messages = [{"role": m["role"], "content": m["content"]} for m in history]
    messages.append({
        "role": "user",
        "content": tailor.build_chat_user_message(
            job, plan, row["cover_letter"], content, body.message
        ),
    })
    system = tailor.build_chat_system_prompt(
        compose.load_voice_guide(), compose.ai_tells_prompt_block()
    )
    binding = aicfg.binding_for(db, "tailor")
    model = binding.model
    try:
        parsed, usages = await tailor.chat(client, system, messages, model)
    # Broad for the same reason as compose: anthropic is lazily imported.
    except Exception as exc:
        for u in usage.usages_of(exc):
            usage.record_usage(db, binding.ledger_key, u, local=binding.local)
        db.commit()
        _applog.warning("tailoring chat turn failed: %s", exc)
        raise errors.http_error(502, errors.TAILOR_CHAT_FAILED)
    for u in usages:
        usage.record_usage(db, binding.ledger_key, u, local=binding.local)
    merged, warnings = tailor.merge_chat_changes(plan, content, parsed)
    letter = parsed["cover_letter"] or row["cover_letter"]
    db.execute(
        """UPDATE tailorings SET change_plan = ?, cover_letter = ?,
                  updated_at = datetime('now') WHERE id = ?""",
        (json.dumps(merged), letter, tailoring_id),
    )
    db.execute(
        "INSERT INTO tailoring_messages (tailoring_id, role, content) VALUES (?, 'user', ?)",
        (tailoring_id, body.message.strip()),
    )
    cursor = db.execute(
        """INSERT INTO tailoring_messages (tailoring_id, role, content, payload)
           VALUES (?, 'assistant', ?, ?)""",
        (tailoring_id, parsed["reply"], json.dumps(parsed)),
    )
    db.commit()
    message = db.execute(
        "SELECT id, role, content, created_at FROM tailoring_messages WHERE id = ?",
        (cursor.lastrowid,),
    ).fetchone()
    return {
        "message": dict(message),
        "tailoring": _tailoring_payload(_fetch_tailoring_row(db, tailoring_id)),
        "warnings": warnings,
    }


@app.get("/api/tailorings/{tailoring_id}/messages")
async def list_tailoring_messages(
    tailoring_id: int, db: sqlite3.Connection = Depends(get_db)
) -> list[dict]:
    """The chat thread, oldest first. Works for applied/discarded rows too
    (it's history); payload (raw contract JSON) stays in the DB — audit
    only, dead weight to the UI."""
    _fetch_tailoring_row(db, tailoring_id)
    rows = db.execute(
        """SELECT id, role, content, created_at FROM tailoring_messages
           WHERE tailoring_id = ? ORDER BY id""",
        (tailoring_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# Application documents (2026-07-21): resumes/letters customized OUTSIDE the
# app live beside the generated tailoring artifacts in data/applications/<id>/
# — the user customizes every serious application, often off-app. Upload is a
# raw-body PUT (multipart would add a python-multipart dependency for no
# benefit at this scale). SAFE_FILE_RE is the path-traversal guard everywhere
# a filename reaches the filesystem; the generated resume-vN/cover-vN PDFs
# match GENERATED_FILE_RE and stay undeletable/unoverwritable.
UPLOAD_EXTENSIONS = {".pdf", ".docx", ".doc", ".txt", ".md", ".html"}
UPLOAD_MAX_BYTES = 15 * 1024 * 1024
SAFE_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ ()-]{0,120}$")

# Closed extension→type map instead of mimetypes.guess_type: on Windows that
# consults the registry, so the served Content-Type would vary per machine.
# octet-stream fallback still applies (generated artifacts, anything odd).
UPLOAD_MIME = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".html": "text/html",
}

# Reserved DOS device names resolve to devices on Windows — with ANY extension
# (CON.pdf too): write_bytes would "succeed" into the console device and store
# nothing. Enforced on every platform so a data dir stays copyable to Windows.
# COM0/LPT0 included defensively. (Not PureWindowsPath.is_reserved: deprecated
# in 3.13.)
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(10)),
    *(f"LPT{i}" for i in range(10)),
}


def safe_filename(name: str) -> bool:
    """Path-traversal + Windows-compat guard for user-supplied filenames."""
    if not SAFE_FILE_RE.fullmatch(name):
        return False
    if name[-1] in ". ":
        # Windows strips trailing dots/spaces on resolution, so "resume.pdf."
        # would silently alias "resume.pdf" for GET and DELETE.
        return False
    return name.split(".")[0].rstrip(" ").upper() not in _WINDOWS_RESERVED


@app.get("/api/applications/{application_id}/files")
async def list_application_files(
    application_id: int, db: sqlite3.Connection = Depends(get_db)
) -> list[dict]:
    """Every file in the application's folder — generated tailoring artifacts
    and uploaded documents alike; `generated` tells the frontend which get a ✕."""
    _fetch_application(db, application_id)
    directory = APPLICATIONS_DIR / str(application_id)
    files = []
    if directory.is_dir():
        for path in sorted(directory.iterdir()):
            if not path.is_file() or not safe_filename(path.name):
                continue
            stat = path.stat()
            files.append({
                "name": path.name,
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                "generated": bool(GENERATED_FILE_RE.fullmatch(path.name)),
            })
    return files


@app.put("/api/applications/{application_id}/files/{filename}", status_code=201)
async def upload_application_file(
    application_id: int,
    filename: str,
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    _fetch_application(db, application_id)
    if not safe_filename(filename) or Path(filename).suffix.lower() not in UPLOAD_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=errors.fmt(errors.UPLOAD_BAD_FILENAME),
        )
    if GENERATED_FILE_RE.fullmatch(filename):
        raise HTTPException(status_code=409, detail="that name is reserved for generated tailoring files")
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="empty file")
    if len(body) > UPLOAD_MAX_BYTES:
        raise HTTPException(status_code=413, detail="file exceeds the 15 MB limit")
    directory = APPLICATIONS_DIR / str(application_id)
    directory.mkdir(parents=True, exist_ok=True)
    # Never overwrite: a re-upload of "resume.pdf" lands as "resume-2.pdf".
    stem, suffix = Path(filename).stem, Path(filename).suffix
    target = directory / filename
    n = 2
    while target.exists():
        target = directory / f"{stem}-{n}{suffix}"
        n += 1
    target.write_bytes(body)
    return {"name": target.name, "size": len(body)}


@app.delete("/api/applications/{application_id}/files/{filename}")
async def delete_application_file(
    application_id: int, filename: str, db: sqlite3.Connection = Depends(get_db)
) -> dict:
    _fetch_application(db, application_id)
    if GENERATED_FILE_RE.fullmatch(filename):
        raise HTTPException(status_code=409, detail="generated tailoring files can't be deleted")
    if not safe_filename(filename):
        raise HTTPException(status_code=404, detail="file not found")
    path = APPLICATIONS_DIR / str(application_id) / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    try:
        path.unlink()
    except OSError:
        # Windows refuses to unlink a file with an open handle (the PDF is
        # open in a viewer, say) — that's a retryable conflict, not a 500.
        raise HTTPException(
            status_code=409,
            detail="file is in use — close it in your viewer and retry",
        )
    return {"deleted": filename}


@app.get("/api/applications/{application_id}/files/{filename}")
async def get_application_file(
    application_id: int, filename: str, db: sqlite3.Connection = Depends(get_db)
) -> FileResponse:
    """Serves application files (Apache only serves frontend/). The filename
    regex is the path-traversal guard — anything else is a plain 404."""
    _fetch_application(db, application_id)
    if not safe_filename(filename):
        raise HTTPException(status_code=404, detail="file not found")
    path = APPLICATIONS_DIR / str(application_id) / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    media_type = UPLOAD_MIME.get(Path(filename).suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=media_type, filename=filename)


# Reminders. ics_uid is server-generated and immutable so calendar
# re-imports update events instead of duplicating them. created_at/updated_at
# are written explicitly (the v4 column migration can't carry a default).

REMINDER_COLUMNS = ("title", "type", "entity_type", "entity_id", "due_date", "due_time", "notes")

# Resolves a human label for the linked entity so the frontend and ICS
# descriptions never need to cross-join.
REMINDER_SELECT = """
    SELECT r.*,
      CASE r.entity_type
        WHEN 'job' THEN (SELECT j.title || ' @ ' || c.name FROM jobs j
                         JOIN companies c ON c.id = j.company_id WHERE j.id = r.entity_id)
        WHEN 'contact' THEN (SELECT name FROM contacts WHERE id = r.entity_id)
        WHEN 'company' THEN (SELECT name FROM companies WHERE id = r.entity_id)
        WHEN 'application' THEN (SELECT j.title || ' @ ' || c.name FROM applications a
                                 JOIN jobs j ON j.id = a.job_id
                                 JOIN companies c ON c.id = j.company_id WHERE a.id = r.entity_id)
      END AS entity_label
    FROM reminders r
"""


def _reminder_rows(db: sqlite3.Connection, where: str = "", params: tuple = ()) -> list[dict]:
    rows = db.execute(
        f"{REMINDER_SELECT} {where} ORDER BY r.done, r.due_date, r.due_time, r.id", params
    ).fetchall()
    return [dict(row) | {"done": bool(row["done"])} for row in rows]


def _fetch_reminder(db: sqlite3.Connection, reminder_id: int) -> dict:
    found = _reminder_rows(db, "WHERE r.id = ?", (reminder_id,))
    if not found:
        raise HTTPException(status_code=404, detail="reminder not found")
    return found[0]


def _reminder_values(body: ReminderIn) -> list:
    data = body.model_dump()
    data["due_date"] = data["due_date"].isoformat()
    return [data[col] for col in REMINDER_COLUMNS]


ICS_MEDIA_TYPE = "text/calendar; charset=utf-8"


@app.get("/api/calendar.ics")
async def calendar_feed(db: sqlite3.Connection = Depends(get_db)) -> Response:
    """Rolling subscribable feed: pending reminders only. Done/deleted ones
    simply vanish; subscribed clients drop missing UIDs."""
    rows = _reminder_rows(db, "WHERE r.done = 0")
    return Response(content=build_calendar(rows), media_type=ICS_MEDIA_TYPE)


# Literal /ics paths are declared before any /api/reminders/{id} routes —
# Starlette matches in declaration order.
@app.get("/api/reminders/ics")
async def reminders_ics_batch(
    ids: str | None = None, db: sqlite3.Connection = Depends(get_db)
) -> Response:
    if ids:
        try:
            wanted = [int(x) for x in ids.split(",") if x.strip()]
        except ValueError:
            raise HTTPException(status_code=400, detail="ids must be comma-separated integers")
        rows = _reminder_rows(
            db, f"WHERE r.id IN ({', '.join('?' for _ in wanted)})", tuple(wanted)
        )
    else:
        rows = _reminder_rows(db, "WHERE r.done = 0")
    return Response(
        content=build_calendar(rows),
        media_type=ICS_MEDIA_TYPE,
        headers={"Content-Disposition": 'attachment; filename="reminders.ics"'},
    )


@app.get("/api/reminders/{reminder_id}/ics")
async def reminder_ics(reminder_id: int, db: sqlite3.Connection = Depends(get_db)) -> Response:
    reminder = _fetch_reminder(db, reminder_id)  # includes done — explicit download
    return Response(
        content=build_calendar([reminder]),
        media_type=ICS_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="reminder-{reminder_id}.ics"'},
    )


@app.get("/api/reminders")
async def list_reminders(db: sqlite3.Connection = Depends(get_db)) -> list[dict]:
    return _reminder_rows(db)


@app.post("/api/reminders", status_code=201)
async def create_reminder(body: ReminderIn, db: sqlite3.Connection = Depends(get_db)) -> dict:
    _check_entity(db, body.entity_type, body.entity_id)
    placeholders = ", ".join("?" for _ in REMINDER_COLUMNS)
    cursor = db.execute(
        f"""INSERT INTO reminders ({', '.join(REMINDER_COLUMNS)}, ics_uid, created_at, updated_at)
            VALUES ({placeholders}, ?, datetime('now'), datetime('now'))""",
        [*_reminder_values(body), f"{uuid4()}@jobsearchhq"],
    )
    db.commit()
    return _fetch_reminder(db, cursor.lastrowid)


@app.put("/api/reminders/{reminder_id}")
async def update_reminder(
    reminder_id: int, body: ReminderIn, db: sqlite3.Connection = Depends(get_db)
) -> dict:
    _check_entity(db, body.entity_type, body.entity_id)
    assignments = ", ".join(f"{col} = ?" for col in REMINDER_COLUMNS)
    cursor = db.execute(
        f"UPDATE reminders SET {assignments}, updated_at = datetime('now') WHERE id = ?",
        [*_reminder_values(body), reminder_id],
    )
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="reminder not found")
    db.commit()
    return _fetch_reminder(db, reminder_id)


@app.patch("/api/reminders/{reminder_id}")
async def patch_reminder(
    reminder_id: int, body: ReminderPatch, db: sqlite3.Connection = Depends(get_db)
) -> dict:
    fields = body.model_dump(exclude_unset=True)
    if "done" in fields:
        fields["done"] = int(fields["done"])
    if fields.get("due_date") is not None:
        fields["due_date"] = fields["due_date"].isoformat()
    assignments = ", ".join(f"{col} = ?" for col in fields)
    cursor = db.execute(
        f"UPDATE reminders SET {assignments}, updated_at = datetime('now') WHERE id = ?",
        [*fields.values(), reminder_id],
    )
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="reminder not found")
    db.commit()
    return _fetch_reminder(db, reminder_id)


@app.delete("/api/reminders/{reminder_id}")
async def delete_reminder(reminder_id: int, db: sqlite3.Connection = Depends(get_db)) -> dict:
    if db.execute("SELECT 1 FROM reminders WHERE id = ?", (reminder_id,)).fetchone() is None:
        raise HTTPException(status_code=404, detail="reminder not found")
    db.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
    db.commit()
    return {"deleted": reminder_id}


# ---------------------------------------------------------------- frontend

class NoCacheStaticFiles(StaticFiles):
    """Invariant (CLAUDE.md): Cache-Control: no-cache on ALL static responses.
    The ES-module graph is un-hashed — heuristic caching half-updates it after
    an upgrade. Subclassing get_response also covers 304 revalidations."""

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


# Registered last so every /api route above wins routing. NOTE: mounts bypass
# the app-level require_user() dependency — the static shell is public-read on
# 127.0.0.1 by design (upstream served it from Apache, outside FastAPI, with
# the same posture); every /api route stays guarded.
app.mount("/", NoCacheStaticFiles(directory=paths.FRONTEND_DIR, html=True), name="frontend")
