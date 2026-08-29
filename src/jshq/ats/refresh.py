"""Refresh pipeline (Phase 3b): fetch every API-addressable company's board,
upsert title-matched jobs, and decay listings gone for 2 consecutive refreshes.

Failure contract: a failing adapter logs to companies.ats_last_status and is
skipped — it never decays that company's jobs and never blocks the run.
settings.last_refresh is set even on partial failure (the run happened). The
exception is a total connectivity outage — every connectable board failing with
a network-class error (offline / DNS / asleep): that leaves last_refresh and
every company's health untouched and records settings.last_refresh_error, so the
on-load staleness backstop retries once connectivity returns rather than
treating the empty run as fresh.
"""

import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx

from .. import db, notify, scoring
from .adapters import ADAPTERS, CONFIG_AWARE
from .adapters._http import _send
from .detect import TIMEOUT, USER_AGENT, detect_company, write_result
from .normalize import (
    AdapterError,
    NormalizedJob,
    compile_title_filter,
    derive_level_band,
    make_dedupe_key,
)

CONCURRENCY = 5
MISS_LIMIT = 2  # consecutive refreshes absent -> closed ("no longer listed")

# Fetch-client timeout: connect keeps detect.py's fast-fail 10s (a dead board
# shouldn't hold a concurrency slot), but reads get 30s — Workday tarpits
# individual responses in slow moments, and a tarpitted read that completes
# beats an aborted company run. The onboarding/detect client below keeps the
# plain 10s probe TIMEOUT on purpose: probes should fail fast.
FETCH_TIMEOUT = httpx.Timeout(10.0, read=30.0)

# Markers that mean "the machine couldn't reach the host" (offline / DNS down /
# asleep / network timeout) as opposed to a board-specific failure (HTTP
# 404/403, bad JSON). Matched as substrings against the AdapterError text, which
# wraps httpx's exception type + message (e.g. "ConnectError: [Errno 8] nodename
# nor servname provided, or not known"). When EVERY connectable board fails this
# way the cause is almost certainly local connectivity, not the boards — see the
# outage guard in _run().
_CONNECTIVITY_MARKERS = (
    "ConnectError",
    "ConnectTimeout",
    "ReadTimeout",
    "PoolTimeout",
    "ConnectionError",
    "nodename nor servname",
    "Temporary failure in name resolution",
    "Name or service not known",
    "No address associated with hostname",
    "Network is unreachable",
)


def _is_connectivity_error(err: AdapterError) -> bool:
    text = str(err)
    return any(marker in text for marker in _CONNECTIVITY_MARKERS)

# Per-process guard so POST /api/refresh can't stack runs. launchd runs in its
# own process, but dedupe upserts make an overlapping run harmless anyway.
_refresh_lock = asyncio.Lock()

# True while a FULL board refresh (run_refresh) is in flight, including the brief
# window it's queued on _refresh_lock. A single-board refresh holds _refresh_lock
# too but does NOT set this, so per-board refreshes queue behind one another
# instead of bailing — only a full refresh genuinely covers every board, so a
# per-board request can safely defer to it (POST /api/companies/{id}/refresh).
_full_refresh_running = False

# Live progress of an in-flight rescore, read by GET /api/refresh/status in the
# same uvicorn process. None when no rescore is running.
SCORING_PROGRESS: dict | None = None

# Live progress of an in-flight ATS refresh (board fetches), read by the same
# status endpoint. {total, done, failed} — done = boards refreshed OK, failed =
# boards that errored; both climb as the parallel fetches land. None when idle.
REFRESH_PROGRESS: dict | None = None


def is_running() -> bool:
    return _refresh_lock.locked()


def is_full_refresh_running() -> bool:
    return _full_refresh_running


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _setting_list(conn: sqlite3.Connection, key: str, default: list[str]) -> list[str]:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row and row["value"] else default


def _title_filter(conn: sqlite3.Connection):
    """company_id -> TitleFilter resolver.

    The global include list, plus any per-company extras from
    settings.company_title_keywords — a JSON map of company id to extra include
    terms, e.g. {"49": ["product lead", "product manager"]}. Scoped inclusion
    exists because global adoption of PM-track keywords was tried (2026-08-10)
    and reverted on evidence (2026-08-11): 88 of 89 PM-titled ingests across
    46 boards read leads=product and clamped — the only boards where those
    titles belong are the ones named here. The global exclude list still wins
    over every include, per-company extras included."""
    keywords = _setting_list(conn, "title_keywords", [])
    exclude = _setting_list(conn, "title_exclude_keywords", [])
    row = conn.execute(
        "SELECT value FROM settings WHERE key = 'company_title_keywords'"
    ).fetchone()
    extras = json.loads(row["value"]) if row and row["value"] else {}
    base = compile_title_filter(keywords, exclude)
    # Extras WIDEN the global gate. With no global include list there is no
    # gate at all (everything ingests), and universe ∪ extras is still the
    # universe — compiling the extras alone would invert the setting into a
    # per-company NARROWING gate, so extras apply only while a global include
    # list exists.
    per_company = {
        int(cid): compile_title_filter(keywords + list(terms), exclude)
        for cid, terms in extras.items() if terms
    } if keywords else {}
    return lambda company_id: per_company.get(company_id, base)


log = logging.getLogger("jshq.ats")


def _fetch_config(conn: sqlite3.Connection) -> dict:
    """Per-run settings the adapters read. Resolved once here rather than
    inside each fetch, so a run makes no DB reads from async code.

    workday_search_terms derives from the live title_keywords include list
    unless the setting overrides it: Workday's API requires a searchText, so
    the include gate IS that board's fetch scope. Deriving at read time means
    every writer of title_keywords (rules, manual chips, the wizard's field
    step) scopes Workday for free and the two can never desync. Per-company
    extras join the union so a Workday board whose gate is widened by
    company_title_keywords can actually fetch those titles — the search terms
    are a fetch superset; the per-company filter of record still gates.
    """
    terms = _setting_list(conn, "workday_search_terms", [])
    if not terms:
        terms = _setting_list(conn, "title_keywords", [])
        if terms:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = 'company_title_keywords'"
            ).fetchone()
            extras = json.loads(row["value"]) if row and row["value"] else {}
            for extra_terms in extras.values():
                terms += [t for t in extra_terms if t and t not in terms]
    return {"workday_search_terms": terms}


def _level_bands():
    """(compiled bands, fallback) from the criteria doc, or the shipped
    defaults when the doc is broken.

    Ingestion must never be blocked by a criteria typo: a refresh that fails
    loses postings that may be gone from the board by the next run. Scoring
    fails loud on the very same doc, so the error is still surfaced.
    """
    from jshq.scoring.criteria import CriteriaError, load_criteria

    try:
        c = load_criteria()
        return c.level_bands, c.level_band_fallback
    except CriteriaError as exc:
        log.warning("criteria doc unreadable, using default level bands: %s", exc)
        return None, None


def _apply_jobs(
    conn: sqlite3.Connection, company_id: int, jobs: list[NormalizedJob], now: str
) -> dict:
    bands, fallback = _level_bands()
    seen: set[str] = set()
    new = 0
    for j in jobs:
        key = make_dedupe_key(company_id, j)
        if key in seen:  # intra-batch duplicate (e.g. repeated req id)
            continue
        seen.add(key)
        row = conn.execute(
            "SELECT id, status, description_text FROM jobs WHERE dedupe_key = ?", (key,)
        ).fetchone()
        if row:
            # Reactivate decay-closed listings; never overwrite the user-owned
            # applied/dismissed states. first_seen is preserved.
            status = "active" if row["status"] == "closed" else row["status"]
            # manually_edited jobs keep their user-corrected location/remote_type/salary
            # (the ATS values are wrong or missing — e.g. a recruiter-learned range); the
            # refresh still updates title/url/level_band/description/last_seen/status/decay.
            conn.execute(
                """UPDATE jobs SET title = ?, url = ?,
                       location      = CASE WHEN manually_edited THEN location      ELSE ? END,
                       remote_type   = CASE WHEN manually_edited THEN remote_type   ELSE ? END,
                       level_band = ?,
                       salary_min    = CASE WHEN manually_edited THEN salary_min    ELSE ? END,
                       salary_max    = CASE WHEN manually_edited THEN salary_max    ELSE ? END,
                       salary_stated = CASE WHEN manually_edited THEN salary_stated ELSE ? END,
                       description_text = ?, last_seen = ?, miss_count = 0, status = ?
                   WHERE id = ?""",
                (
                    j.title, j.url, j.location, j.remote_type,
                    derive_level_band(j.title, bands, fallback), j.salary_min, j.salary_max,
                    int(j.salary_stated), j.description_text, now, status, row["id"],
                ),
            )
            if (j.description_text or None) != (row["description_text"] or None):
                # Changed JD -> rescore: NULL tier1_results marks it pending.
                conn.execute(
                    """UPDATE jobs SET tier1_results = NULL, fit_score = NULL,
                           fit_quadrant = NULL, near_miss_flags = NULL,
                           scoring_notes = NULL, score_detail = NULL WHERE id = ?""",
                    (row["id"],),
                )
        else:
            new += 1
            conn.execute(
                """INSERT INTO jobs (company_id, external_id, title, url, location,
                       remote_type, level_band, salary_min, salary_max, salary_stated,
                       description_text, first_seen, last_seen, status, miss_count,
                       dedupe_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 0, ?)""",
                (
                    company_id, j.external_id, j.title, j.url, j.location,
                    j.remote_type, derive_level_band(j.title, bands, fallback), j.salary_min,
                    j.salary_max, int(j.salary_stated), j.description_text, now, now, key,
                ),
            )

    # Decay — only for a successful fetch, and only ATS-sourced jobs: manual
    # entries (source='manual') aren't on a board we poll, so they'd wrongly
    # vanish once their no-ATS company gains an adapter.
    #
    # The miss COUNT covers active AND applied rows; the status flip below covers
    # active only. An applied job can't be flipped to 'closed' without destroying
    # the user-owned state, so its miss_count is the only signal the UI has that a
    # req you applied to has since been pulled — the Jobs list reads miss_count >=
    # MISS_LIMIT on an applied row as "no longer listed" (frontend isDelisted()).
    # Dismissed rows stay exempt: you've already decided, so closure is moot.
    if seen:
        qmarks = ",".join("?" * len(seen))
        conn.execute(
            f"""UPDATE jobs SET miss_count = miss_count + 1
                WHERE company_id = ? AND status IN ('active', 'applied') AND source = 'ats'
                  AND dedupe_key NOT IN ({qmarks})""",
            (company_id, *seen),
        )
    else:
        conn.execute(
            "UPDATE jobs SET miss_count = miss_count + 1 "
            "WHERE company_id = ? AND status IN ('active', 'applied') AND source = 'ats'",
            (company_id,),
        )
    closed = conn.execute(
        "UPDATE jobs SET status = 'closed' "
        "WHERE company_id = ? AND status = 'active' AND source = 'ats' AND miss_count >= ?",
        (company_id, MISS_LIMIT),
    ).rowcount
    return {"new": new, "closed": closed}


async def _fetch_all(companies: list[sqlite3.Row], title_filter, config=None) -> list:
    sem = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient(
        timeout=FETCH_TIMEOUT, follow_redirects=True, headers={"User-Agent": USER_AGENT}
    ) as client:

        async def guarded(c: sqlite3.Row):
            async with sem:
                try:
                    extra = (
                        {"config": config or {}}
                        if c["ats_type"] in CONFIG_AWARE
                        else {}
                    )
                    outcome = (
                        c,
                        await ADAPTERS[c["ats_type"]](
                            client, c["ats_slug"], title_filter(c["id"]), **extra
                        ),
                    )
                except AdapterError as e:
                    outcome = (c, e)
                except Exception as e:  # never let one company kill the run
                    outcome = (c, AdapterError(f"unexpected: {type(e).__name__}: {e}"))
                # Live per-board progress for the Today bar — climbs as each parallel
                # fetch lands. REFRESH_PROGRESS is None outside a full refresh run
                # (e.g. single-company onboarding), so this is a no-op there.
                if REFRESH_PROGRESS is not None:
                    REFRESH_PROGRESS["failed" if isinstance(outcome[1], AdapterError) else "done"] += 1
                return outcome

        return await asyncio.gather(*(guarded(c) for c in companies))


async def _check_manual_url(client: httpx.AsyncClient, url: str) -> bool | None:
    """Liveness read on one manually-tracked posting URL. True = alive, False =
    gone, None = no evidence either way (403/5xx/transport failure — a bot-block
    or outage is not proof the job closed).

    Gone is a hard 404/410 — or a 200 whose followed redirects landed on a
    DIFFERENT PATH: boards delist by redirecting the posting to the board root
    (greenhouse 302s dead jobs to /<board>?error=true — measured live on a
    delisted posting, 2026-08-25). Paths only, hosts ignored: live greenhouse
    jobs 301-hop boards.greenhouse.io → job-boards.greenhouse.io with the path
    intact, and a canonical slug append (/jobs/123 → /jobs/123-title) still
    counts as the same posting. Soft-404s (a 200 on the SAME path saying "no
    longer accepting applications") still read alive — accepted. Module-level
    so the test suite can stub it (tests never touch the network)."""
    try:
        r = await _send(lambda: client.get(url), f"GET {url}")
    except AdapterError:
        return None
    if r.status_code in (404, 410):
        return False
    if r.status_code != 200:
        return None
    asked = urlsplit(url).path.rstrip("/").lower()
    landed = r.url.path.rstrip("/").lower()
    if landed == asked or landed.startswith((asked + "/", asked + "-")):
        return True
    return False


async def _manual_liveness(conn: sqlite3.Connection, now: str) -> dict:
    """Decay for manually-added jobs. Board decay (_apply_jobs) is
    source='ats'-scoped — a manual row isn't on any polled board — so its
    "still listed?" evidence comes from fetching its own posting URL, feeding
    the same miss_count/MISS_LIMIT machinery: alive resets the count and bumps
    last_seen, gone increments it. A row flips to closed ONLY on a gone check
    that leaves it at the limit — never from a stale count alone, so a
    reactivated row can't re-close on an indeterminate check. Applied rows keep
    their status; their miss_count is the UI's "no longer listed" signal
    (isDelisted), same as board-tracked applies. Dismissed rows are exempt
    (already decided) and scoring columns are never touched."""
    rows = conn.execute(
        "SELECT id, url, status, miss_count FROM jobs WHERE source = 'manual'"
        " AND status IN ('active', 'applied') AND url IS NOT NULL AND url != ''"
    ).fetchall()
    if not rows:
        return {"checked": 0, "gone": 0, "closed": 0}

    sem = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient(
        timeout=FETCH_TIMEOUT, follow_redirects=True, headers={"User-Agent": USER_AGENT}
    ) as client:

        async def guarded(row: sqlite3.Row):
            async with sem:
                return row, await _check_manual_url(client, row["url"])

        results = await asyncio.gather(*(guarded(r) for r in rows))

    gone = closed = 0
    for row, verdict in results:
        if verdict is True:
            conn.execute(
                "UPDATE jobs SET miss_count = 0, last_seen = ? WHERE id = ?",
                (now, row["id"]),
            )
        elif verdict is False:
            gone += 1
            misses = row["miss_count"] + 1
            conn.execute(
                "UPDATE jobs SET miss_count = ? WHERE id = ?", (misses, row["id"])
            )
            if misses >= MISS_LIMIT and row["status"] == "active":
                conn.execute(
                    "UPDATE jobs SET status = 'closed' WHERE id = ?", (row["id"],)
                )
                closed += 1
    conn.commit()
    return {"checked": len(rows), "gone": gone, "closed": closed}


async def run_refresh(
    conn: sqlite3.Connection | None = None, *, company_ids: list[int] | None = None
) -> dict:
    """Refresh every connectable board, or — when company_ids is given (the
    bulk retry-failed path) — only those boards. A scoped run does NOT set
    _full_refresh_running: it doesn't cover every board, so per-board requests
    must queue behind it rather than defer to it."""
    global REFRESH_PROGRESS, _full_refresh_running
    scoped = company_ids is not None
    if not scoped:
        _full_refresh_running = True
    try:
        async with _refresh_lock:
            own_conn = conn is None
            if own_conn:
                conn = db.connect()
            try:
                return await _run(conn, company_ids)
            finally:
                REFRESH_PROGRESS = None
                if own_conn:
                    conn.close()
    finally:
        if not scoped:
            _full_refresh_running = False


async def run_rescore(conn: sqlite3.Connection | None = None) -> dict:
    """Re-score every active job against the current criteria (Phase 7h Settings
    edit). Shares _refresh_lock with run_refresh/onboarding so the two never
    overlap and double-score. Stamps settings.last_rescore (raw ISO string, like
    last_refresh) so the Settings UI can show when the last rescore finished.
    """
    global SCORING_PROGRESS
    async with _refresh_lock:
        own_conn = conn is None
        if own_conn:
            conn = db.connect()
        SCORING_PROGRESS = {"total": 0, "done": 0, "errors": 0}

        def _progress(done, total, errors):
            SCORING_PROGRESS.update(done=done, total=total, errors=errors)

        try:
            report = await scoring.run_scoring(conn, only_pending=False, on_progress=_progress)
            now = _now()
            conn.execute(
                "INSERT INTO settings (key, value) VALUES ('last_rescore', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (now,),
            )
            # A skip (no key, or a criteria error) is recorded too, so the System
            # tab can say why nothing scored instead of showing a stale success.
            stored = (
                {"at": now, "skipped": report["skipped"]}
                if "skipped" in report
                else {
                    "at": now,
                    "scored": report.get("scored", 0),
                    "tier1_failed": report.get("tier1_failed", 0),
                    "errors": report.get("errors", 0),
                    "rate_limited": report.get("rate_limited", 0),
                    "cost": report.get("cost", 0.0),
                }
            )
            conn.execute(
                "INSERT INTO settings (key, value) VALUES ('last_scoring_report', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (json.dumps(stored),),
            )
            conn.commit()
            if "skipped" not in report and notify.popups_enabled(conn):
                msg = f"Rescore complete — {report.get('scored', 0)} scored"
                if report.get("errors"):
                    msg += f", {report['errors']} errors"
                if report.get("rate_limited"):
                    msg += f", {report['rate_limited']} rate-limited"
                await asyncio.to_thread(
                    notify.send, msg,
                    sound="Basso" if report.get("errors") else "Glass",
                )
        finally:
            SCORING_PROGRESS = None
            if own_conn:
                conn.close()
    return {"last_rescore": now, "scoring": report}


async def _run(conn: sqlite3.Connection, company_ids: list[int] | None = None) -> dict:
    now = _now()
    scoped = company_ids is not None
    title_filter = _title_filter(conn)
    ats_types = tuple(ADAPTERS)
    # Scoped runs select by explicit id, never by status: the route pre-stamps
    # the targets 'checking' before this task starts, so a status-based filter
    # here would match nothing.
    sql = f"""SELECT id, name, ats_type, ats_slug FROM companies
            WHERE ats_type IN ({",".join("?" * len(ats_types))}) AND ats_slug IS NOT NULL"""
    params: list = list(ats_types)
    if scoped:
        sql += f" AND id IN ({','.join('?' * len(company_ids))})"
        params += company_ids
    companies = conn.execute(sql + " ORDER BY id", params).fetchall()

    global REFRESH_PROGRESS
    REFRESH_PROGRESS = {"total": len(companies), "done": 0, "failed": 0}

    results = await _fetch_all(companies, title_filter, _fetch_config(conn))

    # Network-wide outage guard: if there were boards to fetch but EVERY one
    # failed with a connectivity-class error, the machine was almost certainly
    # offline or asleep — not the boards' fault. Treat it as one outage, not N
    # failures: leave each company's last-good health untouched, do NOT stamp
    # last_refresh (so app.js's on-load staleness backstop re-runs once
    # connectivity returns), and record a marker the UI surfaces. No jobs are
    # touched (we never reach _apply_jobs / decay).
    errors = [outcome for _, outcome in results if isinstance(outcome, AdapterError)]
    conn_failures = [e for e in errors if _is_connectivity_error(e)]
    # A MAJORITY of attempted boards unreachable ⇒ local outage (asleep/offline), not
    # the boards' fault: a sleeping/just-woke Mac drops most boards (DNS/timeout) while
    # a few cached/fast ones may still answer. A minority of connectivity failures (one
    # slow provider) stays a normal partial failure. (Was: required 100% to fail.)
    # Scoped runs never take this branch: a retry-failed sample is selection-biased
    # toward timeouts (that's why the boards are failing), and the early return
    # would strand the route's pre-stamped 'checking' rows with no final status.
    if not scoped and companies and len(conn_failures) * 2 > len(results):
        unreachable = len(conn_failures)
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('last_refresh_error', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (json.dumps({"at": now, "reason": "offline", "attempted": unreachable}),),
        )
        conn.commit()
        if notify.popups_enabled(conn):
            await asyncio.to_thread(
                notify.send,
                f"Job boards unreachable ({unreachable} attempted) — refresh skipped, will retry.",
                sound="Basso",
            )
        return {
            "outage": True,
            "at": now,
            "attempted": unreachable,
            "companies": [],
            "scoring": {"skipped": "offline"},
        }

    report: list[dict] = []
    for company, outcome in results:
        if isinstance(outcome, AdapterError):
            status = f"error: {outcome}"[:300]
            conn.execute(
                "UPDATE companies SET ats_last_checked = ?, ats_last_status = ? WHERE id = ?",
                (now, status, company["id"]),
            )
            conn.commit()  # per-company commit: crash-safe
            report.append({"company": company["name"], "status": status})
            continue
        stats = _apply_jobs(conn, company["id"], outcome, now)
        status = f"ok: {len(outcome)} matched"
        conn.execute(
            "UPDATE companies SET ats_last_checked = ?, ats_last_status = ? WHERE id = ?",
            (now, status, company["id"]),
        )
        conn.commit()
        report.append({"company": company["name"], "status": status, **stats})

    # Manual-row liveness: board decay can't see manually-added jobs, so their
    # posting URLs get fetched directly (see _manual_liveness). Isolated like
    # scoring below — a crash here must not break the last_refresh staleness
    # contract. Scoped retry runs skip it: they're board-scoped recovery, and
    # per-row fetches don't belong on that fast path.
    manual_report = {"checked": 0, "gone": 0, "closed": 0}
    if not scoped:
        try:
            manual_report = await _manual_liveness(conn, now)
        except Exception as e:
            manual_report["error"] = f"{type(e).__name__}: {e}"

    # A run that reached the internet (at least one board responded, or a real
    # HTTP error came back) is no longer an outage — clear any stale marker. A
    # scoped run clears it only on that evidence: a 7-board retry where every
    # fetch timed out proves nothing about the estate, so the marker stays.
    reached_internet = len(conn_failures) < len(results)
    if not scoped or reached_internet:
        conn.execute("DELETE FROM settings WHERE key = 'last_refresh_error'")
    # last_refresh is the "whole estate is fresh" stamp (app.js staleness
    # backstop + Today's stale banner key off it) — a scoped run must not
    # write it.
    if not scoped:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('last_refresh', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (now,),
        )
    # Completion report for the Today "N of M boards refreshed" bar. Each failure
    # carries the human reason (the recorded status minus the "error: " prefix).
    # Scoped reports are tagged so the frontend renders retry copy and skips the
    # offline-outage fallback heuristic (same selection bias as above).
    failures = [
        {"name": r["company"], "reason": r["status"][7:]}
        for r in report
        if r["status"].startswith("error:")
    ]
    report_payload = {
        "at": now,
        "refreshed": len(report) - len(failures),
        "total": len(report),
        "failures": failures,
        "manual": manual_report,
    }
    if scoped:
        report_payload["scope"] = "failed"
    conn.execute(
        "INSERT INTO settings (key, value) VALUES ('last_refresh_report', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (json.dumps(report_payload),),
    )
    conn.commit()

    # Fit scoring runs after last_refresh is stamped (staleness guarantee is
    # recorded even if scoring dies) and is isolated like a failing adapter:
    # a scoring crash never fails the refresh.
    try:
        scoring_report = await scoring.run_scoring(conn)
    except Exception as e:
        scoring_report = {"skipped": f"scoring crashed: {type(e).__name__}: {e}"}

    # One banner per run, fired at the same moment the frontend sees
    # running:false. Covers the scheduled launchd run and POST /api/refresh
    # alike (they share _run). Skip the pointless "0 of 0 boards" popup.
    if report and notify.popups_enabled(conn):
        if scoped:
            msg = f"{len(report) - len(failures)} of {len(report)} failing boards recovered"
            if failures:
                msg += f" — {len(failures)} still failing"
        else:
            msg = f"{len(report) - len(failures)} of {len(report)} boards refreshed"
            if failures:
                msg += f" — {len(failures)} failed"
        if "scored" in scoring_report:
            msg += f" · {scoring_report['scored']} jobs scored"
        await asyncio.to_thread(notify.send, msg)

    return {
        "last_refresh": now,
        "companies": report,
        "scoring": scoring_report,
        "manual": manual_report,
    }


async def _fetch_and_score_one(conn: sqlite3.Connection, company_id: int) -> str:
    """Pull + score ONE already-enrolled company's board (no detection), under
    _refresh_lock so it serializes with the scheduled run and never double-scores.
    Returns the recorded ats_last_status. Shared by add-time onboarding
    (detect_and_fetch_company) and the on-demand single-board refresh."""
    async with _refresh_lock:
        company = conn.execute(
            "SELECT id, name, ats_type, ats_slug FROM companies WHERE id = ?",
            (company_id,),
        ).fetchone()
        now = _now()
        _, outcome = (
            await _fetch_all([company], _title_filter(conn), _fetch_config(conn))
        )[0]
        if isinstance(outcome, AdapterError):
            status = f"error: {outcome}"[:300]
        else:
            _apply_jobs(conn, company_id, outcome, now)
            status = f"ok: {len(outcome)} matched"
        conn.execute(
            "UPDATE companies SET ats_last_checked = ?, ats_last_status = ? WHERE id = ?",
            (now, status, company_id),
        )
        conn.commit()
        # Score the just-inserted jobs now (isolated like _run): a scoring crash
        # must not undo the successful pull/status.
        if not isinstance(outcome, AdapterError):
            try:
                await scoring.run_scoring(conn)
            except Exception:
                pass
    return status


async def refresh_company_board(company_id: int) -> dict:
    """On-demand re-fetch of one company's EXISTING board (no detection) — the
    worker behind POST /api/companies/{id}/refresh. Opens its own connection (the
    request-scoped one is long gone by the time this fire-and-forget task runs).
    Never raises; any failure is recorded as an 'error:' status."""
    conn = db.connect()
    try:
        status = await _fetch_and_score_one(conn, company_id)
        return {"status": status}
    except Exception as e:
        status = f"error: {type(e).__name__}: {e}"[:300]
        try:
            conn.execute(
                "UPDATE companies SET ats_last_checked = ?, ats_last_status = ? WHERE id = ?",
                (_now(), status, company_id),
            )
            conn.commit()
        except Exception:
            pass
        return {"status": status}
    finally:
        conn.close()


async def detect_and_fetch_company(company_id: int) -> dict:
    """Onboard a freshly-added company (QA pass 2): detect its ATS from the
    careers/website URL and, if one resolves, pull + score its jobs right away.

    The outcome is written to companies.ats_last_status so the detail pane can
    poll it ('checking' -> 'ok: N matched' | 'none: …' | 'error: …'). Detection
    is lock-free network discovery; the pull+score runs under _refresh_lock so
    it serializes with scheduled refreshes and never double-scores. Persisting a
    resolved ats_type/ats_slug also enrolls the company in the regular _run set.
    Never raises — any failure is recorded as an 'error:' status.
    """
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT id, name, careers_url, website FROM companies WHERE id = ?",
            (company_id,),
        ).fetchone()
        if row is None:
            return {"status": "gone"}

        # Best-effort: cache the brand logo for the avatars (keyless; a miss just
        # means the UI shows a monogram). Runs for manual/no-ATS companies too,
        # and never blocks or fails onboarding.
        from .. import logos

        await logos.refresh_company_logo(conn, company_id)

        async with httpx.AsyncClient(
            timeout=TIMEOUT, follow_redirects=True, headers={"User-Agent": USER_AGENT}
        ) as client:
            result = await detect_company(client, row)

        if not result["ats_type"]:
            # Also disconnect any previously-detected board. Re-detection can
            # run on a CONNECTED company now (a URL edit, "Check again"): if
            # the new URL yields nothing, leaving the old ats_type/ats_slug
            # would keep the company enrolled in the scheduled-refresh query —
            # the board the pane says doesn't exist quietly reappears as
            # "ok: N matched" on the next run, fetched via the stale slug.
            status = "none: no ATS detected"
            conn.execute(
                "UPDATE companies SET ats_last_checked = ?, ats_last_status = ?, "
                "ats_type = NULL, ats_slug = NULL WHERE id = ?",
                (_now(), status, company_id),
            )
            conn.commit()
            return {"status": status}

        # Persist the detected ATS first — this alone enrolls the company in the
        # regular refresh query (ats_type IN adapters AND ats_slug IS NOT NULL).
        write_result(conn, result)
        conn.commit()

        if result["ats_type"] not in ADAPTERS:
            # Known URL pattern, no adapter built (the pre-2026-08 Lever gap —
            # a tracked company got "error: unexpected: KeyError: 'lever'"): record what
            # was detected and stop cleanly instead of KeyError-ing in the
            # fetch. Detection stays persisted so the gap is visible.
            status = f"none: {result['ats_type']} detected but no adapter supports it"
            conn.execute(
                "UPDATE companies SET ats_last_checked = ?, ats_last_status = ? WHERE id = ?",
                (_now(), status, company_id),
            )
            conn.commit()
            return {"status": status}

        status = await _fetch_and_score_one(conn, company_id)
        return {"status": status}
    except Exception as e:
        status = f"error: {type(e).__name__}: {e}"[:300]
        try:
            conn.execute(
                "UPDATE companies SET ats_last_checked = ?, ats_last_status = ? WHERE id = ?",
                (_now(), status, company_id),
            )
            conn.commit()
        except Exception:
            pass
        return {"status": status}
    finally:
        conn.close()
