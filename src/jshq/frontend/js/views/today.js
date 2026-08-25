/* Today view: due/overdue reminders, what's new since the last
   visit — new jobs by fit, the near-miss "Maybe" band, recently closed
   listings — suggestions (title-exclude + reminder), and the next
   7 days of scheduled reminders/meetings.

   "Last visit" lives in localStorage (single user): read once per page load
   and immediately re-stamped, so the day's "New jobs" list stays filled all
   day and the "new" highlight persists across in-app nav but clears on a
   browser reload. */

import { api, refreshTriggerAgo } from "../api.js";
import { fmtReminderDue, localToday, openReminderModal } from "../lib/reminderModal.js";
import {
  emptyState,
  esc,
  fitChip,
  fmtAgo,
  fmtFullDate,
  fmtStamp,
  isHardFailFit,
  renderLoadError,
  renderLoading,
  setRowKeys,
  setStats,
  toast,
} from "../lib/ui.js";
import { buzz, chime } from "../lib/notify.js";
import { helpHintHtml } from "../lib/helpHint.js";
import { BULK_RETRY_MIN, failReason } from "../lib/ats.js";
import { companyLogoHtml } from "../lib/logo.js";
import { isNearMiss, parseFlags } from "./jobs.js";

const LAST_VISIT_KEY = "hq_last_visit";
const STALE_MS = 12 * 60 * 60 * 1000;
// A trigger issued this recently is still spinning up (status.running flips
// within one ~5s poll); past it, an issued trigger evidently didn't take.
const RECENT_TRIGGER_MS = 90 * 1000;
const BACKUP_STALE_MS = 26 * 60 * 60 * 1000; // nightly at 02:00 + slack
const FALLBACK_DAYS = 7; // first load ever: "recent" = last 7 days
const UPCOMING_DAYS = 7;

const state = {
  jobs: [],
  lastRefresh: null,
  running: false, // a refresh/rescore is in progress → the green "refreshing" bar
  scoringProgress: null, // {total, done, errors} while scoring runs (live tick)
  refreshProgress: null, // {total, done, failed} while an ATS board refresh runs
  refreshChecking: [], // [{company_id, name}] of boards being individually refreshed (per-board ↻ / onboarding)
  refreshReport: null, // {at, refreshed, total, failures} of the last finished refresh
  refreshError: null, // {at, reason, attempted} when the last refresh was a total connectivity outage
  adapterErrors: [], // per-company ATS failures from /api/refresh/status
  noAts: [], // companies with no connectable ATS (manual/undetected) — check by hand
  stale: [], // connected companies whose auto-pulled job list may be silently out of date
  backup: null, // /api/backup/status payload; null = fetch failed, stay quiet
  keyConfigured: null, // api-key status; null = unknown (fetch failed) → don't claim keyless
  suggestions: { title_exclude: [], reminders: [] },
  reminders: [],
  events: [], // logged meetings/interviews (calendar fodder)
};

let root = null;
// "Last visit" baseline for the "new" highlight: the previous page load's
// stamp, captured once per page load (see ensureBaseline) so the highlight is
// stable across in-app navigation and only resets on a browser reload.
let sessionBaseline = null;
// The report.at of a refresh we watched FINISH this page-load (Option A): the
// completion bar shows only for a completion observed this session, never a
// stale one on a cold load. Reset on reload (module-scope).
let completionSeen = null;

function ensureBaseline() {
  if (sessionBaseline !== null) return;
  const stored = localStorage.getItem(LAST_VISIT_KEY);
  sessionBaseline = stored || new Date(Date.now() - FALLBACK_DAYS * 86400000).toISOString();
  localStorage.setItem(LAST_VISIT_KEY, new Date().toISOString());
}

/* Local midnight — the cutoff for the day's "New jobs" list (retained all day). */
function startOfToday() {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  return d;
}

/* "New jobs" list cutoff: local midnight, or the last visit if that was earlier —
   skipping a day extends the window back so nothing lands unseen. */
function freshCutoff() {
  const dayStart = startOfToday();
  const baseline = new Date(sessionBaseline);
  return baseline < dayStart ? baseline : dayStart;
}

async function load() {
  const [jobs, refresh, suggestions, reminders, events, backup, keyStatus] = await Promise.all([
    api.listJobs(),
    api.refreshStatus(),
    api.getSuggestions(),
    api.listReminders(),
    api.listActivities({ types: "meeting,interview" }),
    api.backupStatus().catch(() => null), // a status hiccup must not blank Today
    // Only the day-one teaching card reads this — a failed fetch leaves it null
    // (unknown), so we never wrongly caption a keyed install as keyless.
    api.getApiKeyStatus().catch(() => null),
  ]);
  jobs.forEach(parseFlags);
  state.jobs = jobs;
  state.lastRefresh = refresh.last_refresh;
  state.running = !!refresh.running;
  state.scoringProgress = refresh.scoring_progress || null;
  state.refreshProgress = refresh.refresh_progress || null;
  state.refreshChecking = refresh.checking || [];
  state.refreshReport = refresh.refresh_report || null;
  state.refreshError = refresh.refresh_error || null;
  state.adapterErrors = refresh.adapter_errors || [];
  state.noAts = refresh.no_ats || [];
  state.stale = refresh.stale || [];
  state.backup = backup;
  // A key that last tested 401 is configured but useless — treat it as keyless
  // here so the day-one card never implies scoring is on (#33).
  state.keyConfigured = keyStatus ? !!keyStatus.configured && !keyStatus.rejected : null;
  state.suggestions = suggestions;
  state.reminders = reminders;
  state.events = events;
}

/* Row shape mirrors the Jobs list (jobs.js listRow): title line, then company
   and location on their own line. Today used to hang the company off the RIGHT
   of the title in the same space-between head, which at a 919px row left 425px
   of dead space between a job and the company it belongs to (P4 measurement).

   showNearMiss is false inside the Maybe section: the "maybe" pill there
   restates the section heading on every single row. Same reason the closed
   band is gone from "No longer listed" — the heading already said it. */
function jobRow(job, { isNew = false, showNearMiss = true } = {}) {
  return `
    <div class="company-row job-row${isNew ? " job-new" : ""}" data-action="open-job" data-id="${job.id}" role="button" tabindex="0">
      <div class="co-row-flex">
        ${companyLogoHtml({ name: job.company_name, logo: job.company_logo }, { size: "sm" })}
        <div class="co-row-rest">
          <div class="company-row-head">
            <span class="company-name">${fitChip(job)}${esc(job.title)}</span>
          </div>
          <div class="company-row-head">
            <span class="job-company">${esc(job.company_name)}</span>
            <span class="company-loc">${esc(job.location || "")}</span>
          </div>
          <div class="company-meta">
            ${isNew ? `<span class="new-pill">new</span>` : ""}
            ${showNearMiss && isNearMiss(job) && !job.manually_elevated ? `<span class="nearmiss-tag">maybe</span>` : ""}
            <span class="remote-tag remote-${esc(job.remote_type || "unknown")}">${esc(job.remote_type || "unknown")}</span>
          </div>
        </div>
      </div>
    </div>`;
}

/* A fictional posting for the day-one teaching card — clearly not real (the
   caption says so), a high-band fit_score so the payoff reads at a glance. Kept
   deliberately generic (Exampleco convention, no personal data). */
const EXAMPLE_JOB = {
  title: "Senior Product Designer",
  company_name: "Exampleco",
  company_logo: null,
  location: "Remote (US)",
  fit_score: 88,
  remote_type: "remote",
};

/* Day-one teaching preview: the marquee payoff — a scored posting — before any
   real one exists, so a fresh install's empty board still shows what it's for.
   Built from the SAME row parts a real scored job uses (companyLogoHtml, fitChip,
   the company-row/job-row markup) so it reads as native, but deliberately INERT:
   no data-action, no data-id, no role/tabindex, so nothing opens or navigates —
   the onClick handler only fires for [data-action] elements. The caption makes
   it unmistakable, and the note ties it to why this user has no real jobs yet. */
function exampleCard() {
  const j = EXAMPLE_JOB;
  const keyless = state.keyConfigured === false;
  const noAuto = state.noAts.length > 0;
  const notes = [];
  if (keyless) notes.push("Scoring and the “why” switch on when you add an API key in Settings → System.");
  if (noAuto) notes.push("Postings you add by hand are scored the same way.");
  return `
    <div class="today-example" aria-label="Example of a scored posting">
      <div class="today-example-cap">Example — not a real posting</div>
      <div class="company-row job-row today-example-row">
        <div class="co-row-flex">
          ${companyLogoHtml({ name: j.company_name, logo: j.company_logo }, { size: "sm" })}
          <div class="co-row-rest">
            <div class="company-row-head">
              <span class="company-name">${fitChip(j)}${esc(j.title)}</span>
            </div>
            <div class="company-row-head">
              <span class="job-company">${esc(j.company_name)}</span>
              <span class="company-loc">${esc(j.location)}</span>
            </div>
            <div class="company-meta">
              <span class="remote-tag remote-${esc(j.remote_type)}">${esc(j.remote_type)}</span>
            </div>
          </div>
        </div>
      </div>
      <ul class="today-example-why">
        <li>Clears a salary floor and matches a remote preference.</li>
        <li>Strong overlap with a top-ranked wish-list criterion.</li>
      </ul>
      ${notes.length ? `<p class="today-example-note">${notes.map(esc).join(" ")}</p>` : ""}
    </div>`;
}

const COLLAPSE_AT = 10;
const expanded = new Set(); // section keys whose full list is shown

/* Long sections collapse to the first 10 rows + "Show all N" (QA item 7). */
function rowList(key, items, renderRow) {
  const rows = expanded.has(key) ? items : items.slice(0, COLLAPSE_AT);
  const hidden = items.length - rows.length;
  return (
    rows.map(renderRow).join("") +
    (hidden > 0
      ? `<button class="btn btn-ghost show-all" data-action="show-all" data-key="${key}">Show all ${items.length}</button>`
      : "")
  );
}

/* count rides the section head, the same idiom Applications/Companies/Settings
   already use — it was the one view that made you count rows yourself. */
function section(title, bodyHtml, { cls = "", count = null } = {}) {
  return `
    <div class="section today-section ${cls}">
      <div class="section-head"><h2 class="section-title">${esc(title)}</h2>${
        count == null ? "" : `<span class="section-count">${count}</span>`
      }</div>
      ${bodyHtml}
    </div>`;
}

function suggestionCard(s) {
  return `
    <div class="suggestion-card" data-keyword="${esc(s.keyword)}">
      <div class="suggestion-text">
        Dismissed ${s.count}× — exclude <strong>“${esc(s.keyword)}”</strong> from ingestion?
        <div class="suggestion-examples">${s.examples.map(esc).join("<br>")}</div>
      </div>
      <div class="suggestion-actions">
        <button class="btn" data-action="suggestion" data-verb="accept" data-keyword="${esc(s.keyword)}">Accept</button>
        <button class="btn btn-ghost" data-action="suggestion" data-verb="ignore" data-keyword="${esc(s.keyword)}">Ignore</button>
      </div>
    </div>`;
}

function reminderSuggestionCard(s) {
  return `
    <div class="suggestion-card">
      <div class="suggestion-text">
        ${esc(s.context)} — <strong>${esc(s.title)}</strong>?
        <div class="suggestion-examples" title="${esc(fmtFullDate(s.due_date))}">due ${esc(fmtDue(s.due_date))}</div>
      </div>
      <div class="suggestion-actions">
        <button class="btn" data-action="rem-suggestion" data-verb="accept" data-key="${esc(s.key)}">Accept</button>
        <button class="btn btn-ghost" data-action="rem-suggestion" data-verb="ignore" data-key="${esc(s.key)}">Ignore</button>
      </div>
    </div>`;
}

/* "Jun 15" for chrome; overdue/today get explicit badges instead. */
function fmtDue(iso) {
  const d = new Date(`${iso}T00:00:00`);
  return isNaN(d) ? iso : d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function dueBadge(reminder, today) {
  const due = esc(fmtReminderDue(reminder.due_date, reminder.due_time));
  if (reminder.due_date < today) {
    const days = Math.round((Date.parse(today) - Date.parse(reminder.due_date)) / 86400000);
    return `<span class="rem-badge rem-overdue" title="${due}">${days}d overdue</span>`;
  }
  return `<span class="rem-badge rem-today" title="${due}">today</span>`;
}

function reminderRow(reminder, today) {
  return `
    <div class="reminder-row">
      <div class="reminder-main" data-action="edit-reminder" data-id="${reminder.id}"
           role="button" tabindex="0" aria-label="Edit reminder: ${esc(reminder.title)}">
        ${dueBadge(reminder, today)}
        <span class="reminder-title">${esc(reminder.title)}</span>
        ${reminder.due_time ? `<span class="reminder-time">${esc(reminder.due_time)}</span>` : ""}
        ${reminder.entity_label ? `<span class="company-loc">${esc(reminder.entity_label)}</span>` : ""}
      </div>
      <div class="reminder-actions">
        <button class="btn btn-ghost" data-action="snooze" data-id="${reminder.id}" data-days="1" title="Snooze to tomorrow">+1d</button>
        <button class="btn btn-ghost" data-action="snooze" data-id="${reminder.id}" data-days="7" title="Snooze a week">+1w</button>
        <button class="btn" data-action="done" data-id="${reminder.id}">Done</button>
      </div>
    </div>`;
}

function upcomingRow(item) {
  return `
    <div class="reminder-row" data-action="open-calendar" data-date="${esc(item.date)}" role="button" tabindex="0">
      <div class="reminder-main">
        <span class="rem-badge ${item.kind === "event" ? "rem-event" : "rem-upcoming"}" title="${esc(fmtReminderDue(item.date, item.time))}">${esc(fmtDue(item.date))}</span>
        <span class="reminder-title">${esc(item.title)}</span>
        ${item.time ? `<span class="reminder-time">${esc(item.time)}</span>` : ""}
        ${item.label ? `<span class="company-loc">${esc(item.label)}</span>` : ""}
      </div>
    </div>`;
}

function upcoming(today) {
  const horizon = localToday(UPCOMING_DAYS);
  const items = [
    ...state.reminders
      .filter((r) => !r.done && r.due_date > today && r.due_date <= horizon)
      .map((r) => ({ kind: "reminder", date: r.due_date, time: r.due_time,
                     title: r.title, label: r.entity_label })),
    ...state.events
      .filter((a) => a.date && a.date > today && a.date <= horizon)
      .map((a) => ({ kind: "event", date: a.date, time: null,
                     title: a.type, label: a.content })),
  ];
  return items.sort((a, b) => a.date.localeCompare(b.date) || (a.time || "").localeCompare(b.time || ""));
}

/* Mirror of the backend's _CONNECTIVITY_MARKERS (ats/refresh.py): an error whose
   text contains one of these means "couldn't reach the host" (offline / DNS /
   asleep), not a board-specific failure. Used ONLY for the pre-fix-DB outage
   fallback in isOfflineOutage() now — the authoritative outage signal is
   state.refreshError. If you edit one list, edit both. */
const CONNECTIVITY_MARKERS = [
  "ConnectError", "ConnectTimeout", "ReadTimeout", "PoolTimeout", "ConnectionError",
  "nodename nor servname", "Temporary failure in name resolution",
  "Name or service not known", "No address associated with hostname", "Network is unreachable",
];
function isConnectivityError(status) {
  return !!status && CONNECTIVITY_MARKERS.some((m) => status.includes(m));
}

function retryBtn(label = "Retry now") {
  return ` <button type="button" class="banner-retry" data-action="retry-refresh">${label}</button>`;
}

/* Scoped variant for the adapter-errors banner when enough boards are failing:
   retries ONLY the failing boards (POST /api/refresh {scope:"failed"}) instead
   of re-pulling the whole estate. */
function retryFailedBtn(n) {
  return ` <button type="button" class="banner-retry" data-action="retry-failed">Retry failed (${n})</button>`;
}

/* Per-session banner dismissal (QA 2026-06-15): sessionStorage so a dismissed
   warning stays hidden across in-app nav + reloads but returns in a fresh
   browser session. Keyed by banner TYPE — dismissing hides that category for the
   session even if its specifics (counts/names) later change. */
const DISMISSED_BANNERS_KEY = "hq_today_dismissed_banners";

function dismissedBanners() {
  try {
    return new Set(JSON.parse(sessionStorage.getItem(DISMISSED_BANNERS_KEY) || "[]"));
  } catch {
    return new Set();
  }
}

function dismissBanner(key) {
  const set = dismissedBanners();
  set.add(key);
  sessionStorage.setItem(DISMISSED_BANNERS_KEY, JSON.stringify([...set]));
}

function dismissBtn(key) {
  return `<button type="button" class="banner-dismiss" data-action="dismiss-banner" data-banner-key="${key}" aria-label="Dismiss this warning">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
    </button>`;
}

/* A11Y-07/UI-05: the calm-green (info/progress) and amber (warning) banners
   differed almost only by hue — indistinguishable under a red-green deficiency.
   A leading tone icon adds a SHAPE cue that survives any colour-vision loss: a
   check for the positive green bars, a warning triangle for amber, an alert
   circle for the rose error bars. aria-hidden — the banner text already carries
   the meaning and the container is a live region — and stroke:currentColor, so
   it inherits the banner ink in both themes (no new token, no colour literal). */
function bannerIcon(html) {
  const svg = (body) =>
    `<svg class="banner-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${body}</svg>`;
  if (/\bbanner-error\b/.test(html)) return svg(`<circle cx="12" cy="12" r="9"/><line x1="12" y1="7.5" x2="12" y2="13"/><line x1="12" y1="16.5" x2="12" y2="16.5"/>`);
  if (/\bbanner-ok\b|\bbanner-progress\b/.test(html)) return svg(`<polyline points="20 6 9 17 4 12"/>`);
  return svg(`<path d="M12 3 L22 20 H2 Z"/><line x1="12" y1="10" x2="12" y2="14"/><line x1="12" y1="16.5" x2="12" y2="16.5"/>`);
}

/* Insert the tone icon as the banner's first child (tone read off its own class
   string). Shared by the refreshing bar, the completion bar and every warning. */
function withIcon(html) {
  return html.replace(/(<div\b[^>]*>)/, (m) => m + bannerIcon(html));
}

/* A muted "as of" timestamp appended to a time-sensitive banner (QA 2026-06-15):
   how stale the underlying condition is. Omitted when there's no meaningful time. */
/* The persisted verify detail (backup_status.json) is written for the log
   ("integrity_check failed", "row count mismatch: jobs"); translate the known
   shapes for the banner (error-audit F7). An unknown shape shows as-is —
   imperfect beats silent. The raw-exception shapes deliberately drop their
   {exc} tail; the backup.log has it. */
function backupDetailText(detail) {
  if (!detail) return "";
  if (detail === "backup file missing or empty") return "the backup file is missing or empty";
  if (detail === "integrity_check failed") return "the backup copy failed its integrity check";
  if (detail.startsWith("row count mismatch")) return "the backup copy is missing rows";
  if (detail.startsWith("backup unreadable")) return "the backup copy couldn't be read";
  if (detail.startsWith("live DB unreadable")) return "the live database couldn't be read";
  return detail;
}

function bannerTime(ts, { bullet = true } = {}) {
  if (!ts) return "";
  return ` <span class="banner-time" title="${esc(fmtStamp(ts))}">${bullet ? "· " : ""}${esc(fmtAgo(ts))}</span>`;
}

// failReason lives in ../lib/ats.js (imported above), shared with the Companies view.

/* The failing-board list for the partial-failure banner + completion bar: names
   only, capped so the banner stays one short line (per-board reasons live behind
   Review → and in the single-failure completion text). */
const FAIL_NAME_CAP = 3;

function formatFailNames(fails) {
  const shown = fails.slice(0, FAIL_NAME_CAP).map((f) => esc(f.name));
  const extra = fails.length - shown.length;
  return shown.join(", ") + (extra > 0 ? ` and ${extra} more` : "");
}

/* The currently-failing boards as [{name, reason}]. Prefer the per-run report
   (its reason is prefix-stripped); fall back to the live adapter rows (whose
   ats_last_status still carries the "error: " prefix — failReason tolerates it). */
function currentFails() {
  const rep = state.refreshReport;
  return rep && rep.failures && rep.failures.length
    ? rep.failures.map((f) => ({ name: f.name, reason: f.reason }))
    : state.adapterErrors.map((e) => ({ name: e.name, reason: e.ats_last_status }));
}

/* The live "refreshing" bar's text — shared by banners() and the poll's in-place
   update so the count can change without recreating the element (no re-flash). */
function refreshingLabel() {
  const rp = state.refreshProgress;
  const sp = state.scoringProgress;
  if (rp && rp.total) return `Refreshing job boards… ${rp.done}/${rp.total} refreshed`;
  // Single-board refresh / onboarding: name the in-flight board(s). Plain text —
  // the innerHTML render site esc()s it; the poll's textContent update is safe raw.
  const names = (state.refreshChecking || []).map((c) => c.name);
  if (names.length) {
    const shown = names.slice(0, 3);
    const more = names.length - shown.length;
    return `Refreshing ${shown.join(", ")}${more ? ` and ${more} more` : ""}…`;
  }
  if (sp && sp.total) return `Rescoring jobs ${sp.done}/${sp.total}…`;
  return "Refreshing job boards…";
}

/* A total connectivity outage on the last refresh — the backend marker, or the
   fallback of every adapter error being connectivity-class. Shared by banners()
   and the "New jobs" empty-state so the two never disagree about the state. */
function isOfflineOutage() {
  if (state.refreshError) return true; // backend's authoritative outage marker
  // Fallback when the marker is absent (a run under older logic, or before a new
  // marker propagates): a MAJORITY of attempted boards failed with connectivity
  // errors — the same test the backend applies. A minority (one slow provider) is
  // a normal partial failure, not an outage.
  // Scoped retry-failed reports are excluded: they re-ran only the failing
  // boards, a sample selection-biased toward timeouts, so the majority test
  // would cry "offline" on a healthy machine (the backend skips its outage
  // guard for scoped runs for the same reason).
  const rep = state.refreshReport;
  if (rep && rep.total && !rep.scope) {
    const conn = (rep.failures || []).filter((f) => isConnectivityError(f.reason)).length;
    return conn * 2 > rep.total;
  }
  return (
    state.adapterErrors.length > 0 &&
    state.adapterErrors.every((e) => isConnectivityError(e.ats_last_status))
  );
}

/* The "New jobs" empty-state text. "Nothing new today" is only TRUE when the
   boards were actually read — so when the last refresh failed (or one is in
   flight) reflect that instead of implying an up-to-date, empty result. Mirrors
   the failure modes banners() surfaces above. */
function newJobsEmptyText() {
  if (state.running) return "Checking for new jobs…";
  if (isOfflineOutage()) {
    const n = state.refreshError?.attempted || state.adapterErrors.length;
    return `Couldn't reach the job boards on the last refresh${n ? ` (${n})` : ""} — can't check for new jobs right now; it retries automatically once you're back online.`;
  }
  if (state.adapterErrors.length) {
    const n = state.adapterErrors.length;
    return `${n} job board${n === 1 ? "" : "s"} failed to refresh — new jobs from ${n === 1 ? "it" : "them"} may be missing. <a href="#/companies?ats=failing">Review →</a>`;
  }
  // Day one (boards never pulled): "last refreshed never" reads as a fault on a
  // machine that hasn't had its first run yet — mirror the calm day-one banner
  // instead.
  if (!state.lastRefresh) return "Nothing yet — openings land here once your first board is pulled.";
  const since =
    freshCutoff() < startOfToday() ? "since your last visit" : "today";
  return `Nothing new ${since} — boards last refreshed <span title="${esc(fmtStamp(state.lastRefresh))}">${esc(fmtAgo(state.lastRefresh))}</span>.`;
}

/* Banner principle: emphasis only for things needing action.
   Warnings (amber) for stale data; errors (rose) for failing machinery; a calm
   green bar (not dismissable) while a refresh/rescore is actually running. Each
   warning carries a stable key so it can be dismissed for the browser session. */
function banners() {
  const out = []; // {key, html} — dismissable warnings/errors
  const running = !!state.running;
  // A total connectivity outage: the backend marker, or (fallback for a stale
  // pre-fix DB) every adapter error being connectivity-class. Shown as ONE calm
  // banner with Retry — not 18 per-company "failing" flags — because nothing is
  // wrong with the companies; the machine was offline.
  const offline = isOfflineOutage();

  const stale = !state.lastRefresh || Date.now() - Date.parse(state.lastRefresh) > STALE_MS;
  // Day one: the boards have never been pulled at all — a fresh install's first
  // minutes, usually seconds after the onboarding wizard's "You're set." Amber
  // "stale" and "no backup yet" would greet a brand-new user with three
  // warnings over a page of zeros, when nothing existed to go stale and the
  // nightly job hasn't had a night. One calm connecting line instead; the
  // no-ATS banner still fires below (it reports a COMPLETED check that needs a
  // human), and everything reverts to normal once a first refresh has run.
  const dayOne = !state.lastRefresh;
  // Add-time onboarding pulls a company's jobs without stamping last_refresh
  // (only the scheduled full refresh does), so a brand-new install can have a
  // board full of jobs while dayOne is still true. Don't greet that with
  // "Nothing on the board yet" over a page that plainly has jobs — there is
  // nothing stale or empty to warn about until the first scheduled refresh.
  const hasJobs = state.jobs.some((j) => j.status === "active");
  // While a refresh is actually running the green bar below covers it, so the
  // stale amber would just be redundant. When it does show, only claim a
  // refresh is coming if a trigger really was issued just now — this copy used
  // to assert one unconditionally, which lied whenever a tab left mounted
  // through a sleep went stale without a page load (the auto-trigger runs in
  // app.js on load + visibility, never from here).
  if (stale && !offline && !running && !(dayOne && hasJobs)) {
    // "Connecting" only when something actually is (a board mid-onboarding);
    // a skipped-company install has nothing in flight, and claiming otherwise
    // would be the same false promise the wizard's done screen used to make.
    const connecting = state.refreshChecking.length > 0;
    out.push(
      dayOne
        ? {
            key: "stale-boards",
            html: connecting
              ? `<div class="stale-banner banner-progress">Connecting your first job board — openings land here as soon as they're pulled.</div>`
              : `<div class="stale-banner banner-progress">Nothing on the board yet — openings land here once your first company's job board is pulled.</div>`,
          }
        : {
            key: "stale-boards",
            html: refreshTriggerAgo() < RECENT_TRIGGER_MS
              ? `<div class="stale-banner">Job boards are stale${bannerTime(state.lastRefresh)} — refresh starting…</div>`
              : `<div class="stale-banner">Job boards are stale${bannerTime(state.lastRefresh)}.${retryBtn("Refresh now")}</div>`,
          }
    );
  }
  if (offline && !running) {
    const n = state.refreshError?.attempted || state.adapterErrors.length;
    out.push({ key: "offline", html:
      `<div role="alert" class="stale-banner banner-error">Last refresh couldn't reach ${n} job board${n === 1 ? "" : "s"} — this computer was likely offline or asleep. Your saved jobs are unchanged; it retries automatically once you're back online.${retryBtn()}${bannerTime(state.refreshError?.at || state.lastRefresh)}</div>`
    });
  } else if (state.adapterErrors.length && !running) {
    const fails = currentFails();
    const rep = state.refreshReport;
    const count = fails.length;
    const lead = rep && rep.total > 0
      ? (rep.scope
          ? `Retried ${rep.total} failing board${rep.total === 1 ? "" : "s"} — ${rep.refreshed} recovered`
          : `${rep.refreshed} of ${rep.total} boards refreshed`)
      : `${count} board${count === 1 ? "" : "s"} couldn't be read`;
    const detail = count === 1
      ? `${esc(fails[0].name)} failed (${esc(failReason(fails[0].reason))})`
      : `${count} failed: ${formatFailNames(fails)}`;
    // Enough failures to be worth a targeted bulk retry → scoped button;
    // a handful → the full "Retry now" stays (one board's fetch is cheap).
    const live = state.adapterErrors.length;
    const retry = live > BULK_RETRY_MIN ? retryFailedBtn(live) : retryBtn();
    out.push({ key: "adapter-errors", html:
      `<div role="alert" class="stale-banner banner-error">${lead} — ${detail}. <a href="#/companies?ats=failing">Review →</a>${retry}${bannerTime(state.lastRefresh)}</div>`
    });
  }
  if (state.stale.length && !running) {
    const n = state.stale.length;
    out.push({ key: "stale-lists", html:
      `<div class="stale-banner">${n} connected ${n === 1 ? "company has" : "companies have"} a stale or empty job list — <a href="#/companies?ats=stale">check Companies</a>.${bannerTime(state.lastRefresh)}</div>`
    });
  }
  if (state.noAts.length) {
    const n = state.noAts.length;
    out.push({ key: "no-ats", html:
      `<div class="stale-banner">${n} ${n === 1 ? "company has" : "companies have"} no connectable ATS — <a href="#/companies?ats=none">check them manually</a>.</div>`
    });
  }
  const backup = state.backup;
  if (backup) {
    if (!backup.present) {
      // Only nag about backups once there is real data to protect (#34). Gating
      // on hasJobs — not dayOne — means a no-op refresh stamping last_refresh
      // can't unmask this seconds in, and the copy doesn't imply a scheduled
      // "nightly job" the app never sets up (backups are run on demand).
      if (hasJobs) out.push({ key: "backup-missing", html: `<div class="stale-banner">No backup yet — run a backup to protect your saved jobs and applications.</div>` });
    } else if (backup.result === "failed") {
      const why = backupDetailText(backup.detail);
      out.push({ key: "backup-failed", html:
        `<div role="alert" class="stale-banner banner-error">Last backup failed its check${why ? ` — ${esc(why)}` : ""}. Run a fresh backup.${bannerTime(backup.checked_at)}</div>`
      });
    } else if (backup.checked_at && Date.now() - Date.parse(backup.checked_at) > BACKUP_STALE_MS) {
      out.push({ key: "backup-stale", html:
        `<div class="stale-banner">Last verified backup was <span title="${esc(fmtStamp(backup.checked_at))}">${esc(fmtAgo(backup.checked_at))}</span> — the nightly job may not be running.</div>`
      });
    }
  }
  // Live "refreshing" status — green, fades in, NOT dismissable (auto-clears when
  // done), leads. The poll updates its text in place each tick (no recreate) so it
  // doesn't re-flash its fade-in; refreshingLabel() is shared with that update.
  const refreshing = running
    ? withIcon(`<div class="stale-banner banner-progress">${esc(refreshingLabel())}</div>`)
    : "";

  // Completion bar (Option A): a dismissable summary shown ONLY for a refresh we
  // watched finish this page-load (completionSeen) — never a stale one on a cold
  // load. Keyed by report.at so each new completion reappears, a dismissed one stays gone.
  let completion = "";
  const rep = state.refreshReport;
  if (!running && rep && rep.total > 0 && rep.at === completionSeen) {
    const key = `refresh-complete:${rep.at}`;
    if (!dismissedBanners().has(key)) {
      const fails = rep.failures || [];
      let cls = " banner-ok";
      let msg = `${rep.refreshed} of ${rep.total} boards refreshed.`;
      if (fails.length === 1) {
        cls = "";
        msg = `${rep.refreshed} of ${rep.total} boards refreshed. ${esc(fails[0].name)} failed — ${esc(failReason(fails[0].reason))}.`;
      } else if (fails.length > 1) {
        cls = "";
        msg = `${rep.refreshed} of ${rep.total} boards refreshed. ${fails.length} failed: ${formatFailNames(fails)}. <a href="#/companies?ats=failing">review →</a>`;
      }
      completion = withIcon(`<div class="stale-banner banner-complete${cls}">${msg}${bannerTime(rep.at, { bullet: false })}${dismissBtn(key)}</div>`);
    }
  }

  const dismissed = dismissedBanners();
  const visible = out.filter((b) => !dismissed.has(b.key));
  // The help-hint "?" rides the first VISIBLE warning; every warning gets a ✕.
  const warnings = visible
    .map((b, i) =>
      withIcon(
        b.html
          .replace("<div ", `<div data-banner-key="${b.key}" `)
          .replace(
            "</div>",
            `${i === 0 ? helpHintHtml("stale-banners") : ""}${dismissBtn(b.key)}</div>`
          )
      )
    )
    .join("");
  return refreshing + completion + warnings;
}

function template() {
  const active = state.jobs.filter((j) => j.status === "active");
  const cutoff = freshCutoff();
  const baseline = new Date(sessionBaseline);
  // List = everything first seen since local midnight — or since the last visit
  // when that reaches further back (a skipped day must not hide arrivals) —
  // retained all day; the "new" highlight marks the subset unseen since the
  // last visit.
  const fresh = active
    .filter((j) => j.first_seen && new Date(j.first_seen) >= cutoff)
    .filter((j) => !isHardFailFit(j)) // hide Tier-1 hard-fails (e.g. onsite-abroad)
    .sort((a, b) => (b.fit_score ?? -1) - (a.fit_score ?? -1));
  const nearMisses = active
    .filter((j) => isNearMiss(j) && !j.manually_elevated) // elevated = already triaged out of "maybe"
    .sort((a, b) => (b.fit_score ?? -1) - (a.fit_score ?? -1));
  const gone = state.jobs.filter(
    (j) => j.status === "closed" && j.last_seen && new Date(j.last_seen) >= baseline
  );

  const today = localToday();
  const due = state.reminders.filter((r) => !r.done && r.due_date <= today);
  const coming = upcoming(today);
  const cards = [
    ...state.suggestions.reminders.map(reminderSuggestionCard),
    ...state.suggestions.title_exclude.map(suggestionCard),
  ];

  return `
    <div class="today-banners" role="status" aria-live="polite">${banners()}</div>
    <div class="today">
      ${
        due.length
          ? section("Reminders", rowList("reminders", due, (r) => reminderRow(r, today)), {
              cls: "today-reminders",
              count: due.length,
            })
          : ""
      }
      ${section(
        "New jobs",
        fresh.length
          ? rowList("fresh", fresh, (j) =>
              jobRow(j, { isNew: !!j.first_seen && new Date(j.first_seen) > baseline })
            )
          // An empty board (nothing pulled or added yet): the honest empty line,
          // then a teaching preview of a scored posting so the value is shown,
          // not just promised. Gated on the job count, not last_refresh — a
          // no-ATS user's first (fruitless) refresh stamps last_refresh but
          // leaves the board empty, and the payoff preview should persist until a
          // real posting lands. The empty line itself still adapts to whether a
          // pull has run (newJobsEmptyText reads last_refresh).
          : !state.jobs.length
            ? emptyState(newJobsEmptyText(), { html: true }) + exampleCard()
            : emptyState(newJobsEmptyText(), { html: true }),
        { count: fresh.length }
      )}
      ${section(
        "Maybe",
        nearMisses.length
          ? rowList("maybe", nearMisses, (j) => jobRow(j, { showNearMiss: false }))
          : emptyState("No maybes right now."),
        { cls: "today-nearmiss", count: nearMisses.length }
      )}
      ${section(
        "No longer listed",
        gone.length
          ? rowList("gone", gone, (j) => jobRow(j))
          : emptyState("Nothing closed since your last visit."),
        { count: gone.length }
      )}
      ${cards.length ? section("Suggestions", cards.join(""), { count: cards.length }) : ""}
      ${section(
        "Coming up",
        coming.length
          ? rowList("coming", coming, upcomingRow)
          : emptyState(`Nothing scheduled in the next ${UPCOMING_DAYS} days — <a href="#/calendar">open the calendar</a>.`, { html: true }),
        { count: coming.length }
      )}
    </div>`;
}

function renderStats() {
  const active = state.jobs.filter((j) => j.status === "active");
  const cutoff = freshCutoff();
  const fresh = active.filter(
    (j) => j.first_seen && new Date(j.first_seen) >= cutoff && !isHardFailFit(j)
  );
  setStats([
    { value: fresh.length, label: "New" },
    { value: active.filter((j) => isNearMiss(j) && !j.manually_elevated).length, label: "Maybe" },
  ]);
}

async function onSuggestion(keyword, verb) {
  try {
    await api.actOnSuggestion(keyword, verb);
    state.suggestions.title_exclude = state.suggestions.title_exclude.filter(
      (s) => s.keyword !== keyword
    );
    paint();
    toast(verb === "accept" ? "Will exclude at the next refresh" : "Suggestion ignored");
  } catch (error) {
    toast(error.detail || error.message, { error: true });
  }
}

async function onReminderSuggestion(key, verb) {
  try {
    const result = await api.actOnReminderSuggestion(key, verb);
    state.suggestions.reminders = state.suggestions.reminders.filter((s) => s.key !== key);
    if (result.reminder) state.reminders.push(result.reminder);
    paint();
    toast(verb === "accept" ? "Reminder added" : "Suggestion ignored");
  } catch (error) {
    if (error.status === 404) {
      // stale card — the underlying event changed; drop it
      state.suggestions.reminders = state.suggestions.reminders.filter((s) => s.key !== key);
      paint();
    }
    toast(error.detail || error.message, { error: true });
  }
}

async function patchReminder(id, body, message) {
  try {
    const updated = await api.patchReminder(id, body);
    state.reminders = state.reminders.map((r) => (r.id === id ? updated : r));
    paint();
    toast(message);
  } catch (error) {
    toast(error.detail || error.message, { error: true });
  }
}

async function reloadToday() {
  try {
    await load();
  } catch {
    return; // a status hiccup mustn't blank Today; the banner stays as-is
  }
  paint();
}

let refreshPoll = null;

function stopRefreshPoll() {
  if (refreshPoll) {
    clearInterval(refreshPoll);
    refreshPoll = null;
  }
}

/* Poll while a refresh/rescore runs so the green "refreshing" bar stays live and
   Today reloads (new jobs in, bar cleared) the moment it finishes. Self-terminates
   if the user navigates away: app.js renders every view into the same #view
   element, so a leaked timer would repaint Today over whatever view is showing. */
function startRefreshPoll() {
  stopRefreshPoll();
  let misses = 0;
  refreshPoll = setInterval(async () => {
    if (!root || !root.querySelector(".today")) {
      stopRefreshPoll(); // Today is no longer mounted
      return;
    }
    let s;
    try {
      s = await api.refreshStatus();
      misses = 0;
    } catch {
      // One failed tick is usually a blip (server mid-restart between polls);
      // keep watching. Dying on the first error used to leave the green
      // "Refreshing…" bar up forever with state.running stuck true. After
      // three in a row, stop pretending: clear the bar and say so — the
      // refresh itself continues server-side.
      if (++misses < 3) return;
      stopRefreshPoll();
      state.running = false;
      paint();
      toast("Lost contact with the refresh — reload to see its result.", { error: true });
      return;
    }
    if (!s.running) {
      stopRefreshPoll();
      // Option A: remember the completion we just witnessed so its bar shows.
      completionSeen = s.refresh_report?.at || null;
      // Keyed on the completion stamp so the app.js/jobs.js pollers watching the
      // same run don't double-ding. (This poll also witnesses rescores — those
      // chime under a stale refresh: key; right sound, benign key quirk.)
      if (s.refresh_error) buzz("refresh:" + s.refresh_error.at);
      else chime("refresh:" + (s.refresh_report?.at || "done"));
      await reloadToday();
      return;
    }
    state.running = true;
    state.scoringProgress = s.scoring_progress || null;
    state.refreshProgress = s.refresh_progress || null;
    state.refreshChecking = s.checking || [];
    // Update the green bar's count IN PLACE — no element recreate, so its fade-in
    // doesn't re-flash every 4s. If the bar isn't there yet, a full paint creates
    // it (and it fades in once).
    const bar = root.querySelector(".banner-progress");
    if (bar) bar.textContent = refreshingLabel();
    else paint();
  }, 4000);
}

// Banners superseded by an active refresh (they reflect the PREVIOUS run): faded
// out on Retry, and omitted by banners() while running.
const SUPPRESSED_WHEN_RUNNING = ["stale-boards", "offline", "adapter-errors", "stale-lists"];

/* Fade the superseded failure banners out (300ms, matching the app's fade idiom),
   resolving once the animation has run so the caller can repaint cleanly. */
function fadeOutSuppressed() {
  const els = SUPPRESSED_WHEN_RUNNING
    .map((k) => root.querySelector(`.today-banners [data-banner-key="${k}"]`))
    .filter(Boolean);
  if (!els.length) return Promise.resolve();
  els.forEach((el) => el.classList.add("banner-exit"));
  return new Promise((resolve) => setTimeout(resolve, 280));
}

/* Retry from a banner: kick a full refresh, then poll until it finishes (the
   green bar covers the wait). Same trigger as the Jobs "Refresh boards" button,
   surfaced where the failure is shown. */
async function retryRefresh() {
  try {
    const r = await api.triggerRefresh();
    toast(r.running ? "Refresh already running…" : "Retrying job boards…");
    // Only after the trigger succeeds: fade the superseded failure bar(s) out,
    // then paint the green "Refreshing…" bar (which fades in). On a failed trigger
    // we fall to catch and leave the failure bar in place.
    await fadeOutSuppressed();
    state.running = true;
    paint();
    startRefreshPoll();
  } catch (error) {
    toast(error.detail || error.message, { error: true });
  }
}

/* Scoped retry: only the failing boards. Same green-bar flow as retryRefresh;
   {none} means the failures cleared since the banner rendered — nothing is
   running, so the bar must NOT start. */
async function retryFailedRefresh() {
  try {
    const r = await api.triggerRefresh({ scope: "failed" });
    if (r.running) {
      toast("Refresh already running…");
      return;
    }
    if (r.none) {
      toast("No failing boards to retry.");
      return;
    }
    toast(`Retrying ${r.ids.length} failing board${r.ids.length === 1 ? "" : "s"}…`);
    await fadeOutSuppressed();
    state.running = true;
    paint();
    startRefreshPoll();
  } catch (error) {
    toast(error.detail || error.message, { error: true });
  }
}

function onClick(event) {
  const target = event.target.closest("[data-action]");
  if (!target || !root.contains(target)) return;
  switch (target.dataset.action) {
    case "open-job":
      location.hash = `#/jobs/${target.dataset.id}`;
      break;
    case "retry-refresh":
      retryRefresh();
      break;
    case "retry-failed":
      retryFailedRefresh();
      break;
    case "dismiss-banner":
      dismissBanner(target.dataset.bannerKey);
      paint();
      break;
    case "show-all":
      expanded.add(target.dataset.key);
      paint();
      break;
    case "suggestion":
      onSuggestion(target.dataset.keyword, target.dataset.verb);
      break;
    case "rem-suggestion":
      onReminderSuggestion(target.dataset.key, target.dataset.verb);
      break;
    case "done":
      patchReminder(Number(target.dataset.id), { done: true }, "Done");
      break;
    case "snooze":
      patchReminder(
        Number(target.dataset.id),
        { due_date: localToday(Number(target.dataset.days)) },
        `Snoozed to ${fmtDue(localToday(Number(target.dataset.days)))}`
      );
      break;
    case "edit-reminder": {
      const reminder = state.reminders.find((r) => r.id === Number(target.dataset.id));
      if (reminder) {
        openReminderModal({
          reminder,
          onSaved: (saved) => {
            state.reminders = saved
              ? state.reminders.map((r) => (r.id === saved.id ? saved : r))
              : state.reminders.filter((r) => r.id !== reminder.id);
            paint();
          },
        });
      }
      break;
    }
    case "open-calendar": {
      // Land on the item's own day, not whatever "today" is — opening Aug 11's
      // interview should show Aug 11. Bare #/calendar (the empty-state link and
      // the nav tab) still opens on today.
      const date = target.dataset.date;
      location.hash = date ? `#/calendar?date=${encodeURIComponent(date)}` : "#/calendar";
      break;
    }
  }
}

function paint() {
  // Preserve the .today scroll position across the innerHTML swap. The refresh
  // poll repaints every few seconds, and without this it would jerk the page
  // back to the top whenever the user has scrolled the list (QA 2026-06-15).
  const scroller = root.querySelector(".today");
  const top = scroller ? scroller.scrollTop : 0;
  root.innerHTML = template();
  const next = root.querySelector(".today");
  if (next) next.scrollTop = top;
  renderStats();
}

export async function render(container) {
  root = container;
  stopRefreshPoll(); // kill any poll leaked from a prior mount
  renderLoading(container);
  container.onclick = onClick;
  container.onchange = null;
  container.oninput = null;
  setRowKeys(container, onClick);
  try {
    await load();
  } catch (error) {
    renderLoadError(container, error, () => render(container));
    setStats([]);
    return;
  }
  ensureBaseline(); // capture the "last visit" baseline once per page load
  expanded.clear(); // each visit starts collapsed
  paint();
  if (state.running) startRefreshPoll(); // a refresh/rescore is already in flight
}
