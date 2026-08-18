/* Applications view (Phase 7c): the pipeline as a status-grouped list pane +
   detail pane with edit-in-place. One application per job; rows are created
   from job detail ("Start application" / "Mark applied"), never here. */

import { api } from "../api.js";
import { openComposeModal } from "../lib/composeModal.js";
import {
  activityTimelineHtml,
  fmtReminderDue,
  localToday,
  openActivityModal,
  openReminderModal,
} from "../lib/reminderModal.js";
import {
  confirmModal,
  emptyState,
  esc,
  escUrl,
  fitChip,
  fmtFullDate,
  fmtStamp,
  getDetailScroll,
  getListScroll,
  hidePop,
  HQ_MARK,
  isDelisted,
  isPopOpen,
  renderLoadError,
  renderLoading,
  searchBoxHtml,
  setDetailHash,
  revealSelected,
  setDetailScroll,
  setListScroll,
  pluralize,
  setStats,
  setFocusOut,
  setRowKeys,
  showPop,
  toast,
} from "../lib/ui.js";
import {
  bindOutsideClose,
  closeDropdowns,
  ddTemplate,
  updateToggle,
} from "../lib/filterDd.js";
import { buzz, chime } from "../lib/notify.js";
import { helpHintHtml } from "../lib/helpHint.js";
import { dateFieldHtml } from "../lib/datepicker.js";
import { companyLogoHtml } from "../lib/logo.js";

/* Pipeline order; rejected/withdrawn are terminal and render de-emphasized
   at the bottom (same treatment as closed jobs). */
const STATUSES = [
  "drafting", "applied", "screen", "interview", "offer", "rejected", "withdrawn",
];
const TERMINAL = new Set(["rejected", "withdrawn"]);
const isOpen = (app) => !TERMINAL.has(app.status);

/* Sort + Status pills, carried over from Jobs (2026-08-10). Sort is Jobs'
   convention: the "" option IS the default (Fit), so the pill reads "Sort" at
   rest and swaps to the chosen label while active. Sorting happens WITHIN the
   status groups — the pipeline structure stays, only the in-group order moves. */
const SORT_DD = {
  key: "sortBy",
  label: "Sort",
  type: "radio",
  options: [
    { value: "", label: "Fit" },
    { value: "applied", label: "Applied date" },
    { value: "salary", label: "Salary" },
  ],
};
const STATUS_DD = {
  key: "status",
  label: "Status",
  type: "multi",
  options: STATUSES.map((s) => ({ value: s, label: s })),
};
const ALL_DD = [SORT_DD, STATUS_DD];
const ddByKey = (key) => ALL_DD.find((d) => d.key === key);

/* In-group comparators. Fit mirrors jobs.js: elevated pinned via the 1e9
   sentinel (two elevated rows tie to 0, not NaN), NULL (unscored) below any
   real score. Applied date falls back to created_at so drafting rows still
   order; Salary sinks comp-unknown rows below any stated range. */
const when = (a) => Date.parse(a.applied_date || a.created_at) || 0;
const byApplied = (a, b) => when(b) - when(a) || b.id - a.id;
const fitRank = (a) => (a.manually_elevated ? 1e9 : a.fit_score ?? -1);
const byFit = (a, b) => fitRank(b) - fitRank(a) || byApplied(a, b);
const bySalary = (a, b) => (b.salary_max ?? -1) - (a.salary_max ?? -1) || byApplied(a, b);
const sortCmp = () =>
  state.filters.sortBy === "applied" ? byApplied
  : state.filters.sortBy === "salary" ? bySalary
  : byFit;

const state = {
  applications: [],
  reminders: [],
  suggestions: [], // pending reminder suggestions (followup_application cards)
  selectedId: null,
  activityCache: new Map(), // app.id -> merged job + application activities
  filesCache: new Map(), // app.id -> [{name, size, modified, generated}]; absent = loading
  tailoringCache: new Map(), // app.id -> tailoring | null (none); absent = loading
  tailorChatCache: new Map(), // tailoring.id -> messages[]; regenerate = new id = fresh thread
  tailorBusy: null, // { id, kind: "generate" | "apply" | "rerender" | "refine" | "chat" } while a call is in flight
  filters: { q: "", sortBy: "", status: new Set() },
  mobileDetail: false,
  listScroll: 0,
  detailScroll: 0,
};

let root = null;

/* Mirrors ApplicationUpdate on the backend (job_id is immutable). */
function payload(app, overrides = {}) {
  const fields = [
    "status", "applied_date", "resume_version", "cover_note", "next_step", "next_step_date",
  ];
  const body = {};
  for (const field of fields) body[field] = app[field] ?? null;
  return { ...body, ...overrides };
}

async function load() {
  const [applications, reminders, suggestions] = await Promise.all([
    api.listApplications(),
    api.listReminders(),
    api.getSuggestions(),
  ]);
  state.applications = applications;
  state.reminders = reminders;
  state.suggestions = suggestions.reminders;
}

function selected() {
  return state.applications.find((a) => a.id === state.selectedId) || null;
}

function entityLabel(app) {
  return `${app.job_title} @ ${app.company_name}`;
}

function filtered() {
  const needle = state.filters.q.trim().toLowerCase();
  return state.applications.filter((a) => {
    if (state.filters.status.size && !state.filters.status.has(a.status)) return false;
    if (!needle) return true;
    const haystack = `${a.job_title} ${a.company_name} ${a.next_step || ""}`.toLowerCase();
    return haystack.includes(needle);
  });
}

function linkedReminders(app) {
  return state.reminders.filter(
    (r) =>
      (r.entity_type === "application" && r.entity_id === app.id) ||
      (r.entity_type === "job" && r.entity_id === app.job_id)
  );
}

/* The suggester keys followup suggestions by application id (backend
   reminder_suggest.py) — surface the pending card on the application itself. */
function pendingSuggestion(app) {
  return state.suggestions.find(
    (s) => s.key === `followup_application:application:${app.id}`
  );
}

function fmtDue(iso) {
  const d = new Date(`${iso}T00:00:00`);
  return isNaN(d) ? iso : d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function nextStepBadge(app) {
  if (!app.next_step_date || !isOpen(app)) return "";
  const cls = app.next_step_date <= localToday() ? "rem-overdue" : "rem-upcoming";
  return `<span class="rem-badge ${cls}" title="${esc(fmtFullDate(app.next_step_date))}">due ${esc(fmtDue(app.next_step_date))}</span>`;
}

/* "No longer listed", carried over from Jobs (owner review, 2026-08-13). The listing
   state lives on the JOB, so this reconstructs the shape isDelisted() reads from
   the joined columns — job_status + miss_count, both on APPLICATION_SELECT.
   Gated on isOpen: a rejected/withdrawn row is already over, and Jobs
   deliberately suppresses the band in favour of the resolved one, so showing it
   here would contradict that precedence. Never pairs with the .closed fade — an
   open application against a pulled req is still live work. */
function delistedBandHtml(app) {
  if (!isOpen(app) || !isDelisted({ status: app.job_status, miss_count: app.miss_count })) return "";
  return `<span class="closed-band">no longer listed</span>`;
}

function listRow(app) {
  const isSelected = app.id === state.selectedId;
  return `
    <div class="company-row job-row${isSelected ? " selected" : ""}${TERMINAL.has(app.status) ? " closed" : ""}"
         data-action="select" data-id="${app.id}" role="button" tabindex="0">
      <div class="co-row-flex">
        ${companyLogoHtml({ name: app.company_name, logo: app.company_logo }, { size: "sm" })}
        <div class="co-row-rest">
          <div class="company-row-head">
            <span class="company-name">${fitChip(app)}${esc(app.job_title)}</span>
            ${app.applied_date ? `<span class="company-loc" title="${esc(fmtFullDate(app.applied_date))}">applied ${esc(fmtDue(app.applied_date))}</span>` : ""}
          </div>
          <div class="company-meta">
            <span class="job-company">${esc(app.company_name)}</span>
            ${delistedBandHtml(app)}
            ${nextStepBadge(app)}
            ${app.next_step ? `<span class="next-step-text">${esc(app.next_step)}</span>` : ""}
          </div>
        </div>
      </div>
    </div>`;
}

function groupedList(rows) {
  return STATUSES.map((status) => {
    const group = rows.filter((a) => a.status === status).sort(sortCmp());
    if (!group.length) return "";
    return `
      <div class="list-group-head${TERMINAL.has(status) ? " group-terminal" : ""}">
        <span><span class="appstatus-dot appstatus-${status}"></span>${status}</span>
        <span class="section-count">${group.length}</span>
      </div>
      ${group.map(listRow).join("")}`;
  }).join("");
}

function suggestionCard(s) {
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

function reminderRow(reminder) {
  return `
    <div class="reminder-row${reminder.done ? " reminder-done" : ""}" data-action="edit-reminder" data-id="${reminder.id}">
      <div class="reminder-main">
        <span class="rem-badge ${!reminder.done && reminder.due_date <= localToday() ? "rem-overdue" : "rem-upcoming"}" title="${esc(fmtReminderDue(reminder.due_date, reminder.due_time))}">${esc(fmtDue(reminder.due_date))}</span>
        <span class="reminder-title">${esc(reminder.title)}</span>
        ${reminder.due_time ? `<span class="reminder-time">${esc(reminder.due_time)}</span>` : ""}
      </div>
    </div>`;
}

function remindersSection(app) {
  const suggestion = pendingSuggestion(app);
  const rows = linkedReminders(app);
  return `
    <div class="section">
      <div class="section-head">
        <h2 class="section-title">Reminders</h2>
        <button class="btn btn-ghost" data-action="add-reminder">+ Reminder</button>
      </div>
      ${suggestion ? suggestionCard(suggestion) : ""}
      ${rows.length ? rows.map(reminderRow).join("") : suggestion ? "" : emptyState("No linked reminders.")}
    </div>`;
}

/* Tailoring (Phase 7e): the agent proposes a resume change plan +
   cover letter; the user approves any subset; Apply renders versioned PDFs and
   stamps resume_version/cover_note. Inline section, not a modal — the
   line-by-line diff needs the room. */

/* Lucide loader-circle, inline to match the nav icons (no icon dependency),
   spun slowly by .tailor-spin (reuses @keyframes ats-spin). Shown while any
   tailor call is in flight so a long generation reads as working, not stuck. */
const SPINNER = `<svg class="tailor-spin" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg>`;

function tailorChangeRow(change, lock) {
  return `
    <label class="tailor-change">
      <input type="checkbox" data-action="tailor-toggle" data-cid="${esc(change.id)}"${change.approved ? " checked" : ""}${lock} />
      <span class="tailor-change-body">
        <span class="tailor-old">${esc(change.old)}</span>
        <span class="tailor-new">${esc(change.new)}</span>
        ${change.rationale ? `<span class="tailor-rationale">${esc(change.rationale)}</span>` : ""}
      </span>
    </label>`;
}

function tailorChatMsgHtml(message) {
  return `<div class="tailor-chat-msg tailor-chat-${message.role === "user" ? "user" : "assistant"}">${esc(message.content)}</div>`;
}

/* Chat refinement (Phase 7f): pending-only thread; the server rewrites the
   plan/letter each turn and the whole section repaints from server truth. */
function tailorChatHtml(t, busy) {
  const messages = state.tailorChatCache.get(t.id);
  return `
    <div class="tailor-chat">
      <span class="field-label">Refine with the agent</span>
      ${messages === undefined ? emptyState("Loading thread…") : messages.map(tailorChatMsgHtml).join("")}
      ${busy === "chat" ? `<div class="tailor-chat-msg tailor-chat-assistant tailor-chat-thinking">${SPINNER}Thinking…</div>` : ""}
      <div class="tailor-chat-composer">
        <textarea class="notes-area" data-tailor-chat-input rows="2"
          placeholder="e.g. make the second bullet less salesy… (Enter sends)"${busy ? " disabled" : ""}></textarea>
        <button class="btn btn-ghost" data-action="tailor-chat-send"${busy ? " disabled" : ""}>${busy === "chat" ? "Thinking…" : "Send"}</button>
      </div>
    </div>`;
}

function tailorPendingHtml(t, busy) {
  const total = t.change_plan.length;
  const approved = t.change_plan.filter((c) => c.approved).length;
  const applyLabel = total ? `Apply (${approved} of ${total})` : "Apply (cover letter only)";
  // A chat turn rewrites the plan and letter server-side, so edits made while
  // one is in flight would be lost — lock the whole pending section.
  const lock = busy === "chat" ? " disabled" : "";
  return `
    ${t.analysis ? `<p class="tailor-analysis">${esc(t.analysis)}</p>` : ""}
    ${
      total
        ? `<div class="tailor-bulk">
            <span class="tailor-count">${approved} of ${total} approved</span>
            <button class="btn btn-ghost" data-action="tailor-bulk" data-verb="all"${lock}>Approve all</button>
            <button class="btn btn-ghost" data-action="tailor-bulk" data-verb="none"${lock}>None</button>
          </div>
          ${t.change_plan.map((c) => tailorChangeRow(c, lock)).join("")}`
        : emptyState("No resume changes proposed — cover letter only.")
    }
    <div class="field tailor-letter">
      <span class="field-label">Cover letter draft</span>
      <textarea class="notes-area" data-tailor-field="cover_letter"${lock}>${esc(t.cover_letter)}</textarea>
      <button type="button" class="btn btn-ghost btn-refine" data-action="tailor-refine"${busy ? " disabled" : ""}>Refine (remove AI tells)</button>
    </div>
    ${tailorChatHtml(t, busy)}
    <div class="title-chip-row">
      <button class="btn btn-accent" data-action="tailor-apply"${busy ? " disabled" : ""}>${busy === "apply" ? `${SPINNER}Rendering…` : applyLabel}</button>
      <button class="btn btn-ghost" data-action="tailor-regenerate"${lock}>Regenerate</button>
      <button class="btn btn-ghost btn-danger" data-action="tailor-discard"${lock}>Discard</button>
    </div>`;
}

function tailorAppliedHtml(app, t, busy) {
  // The cover and resume carry independent versions: "Re-render cover PDF"
  // advances only the cover, leaving the resume at the version it was last
  // rendered (the last full Apply). t.version is the cover version;
  // app.resume_version (e.g. "v2") is the resume's. When they differ, the
  // letter was re-rendered without a resume change — say so, and label each PDF
  // link with its own version so it's clear what's current.
  const coverVer = `v${t.version}`;
  const resumeVer = /^v\d+$/.test(app.resume_version || "") ? app.resume_version : coverVer;
  const coverOnly = resumeVer !== coverVer;
  // The letter stays editable after applying; the data-tailor-field autosave
  // handlers bail on non-pending rows, so this textarea is ephemeral until
  // re-render (no accidental PATCH on an applied row).
  const lock = busy ? " disabled" : "";
  return `
    <div class="tailor-applied">
      <span title="${esc(fmtStamp(t.applied_at))}">v${t.version} ${coverOnly ? "cover letter " : ""}generated ${esc(fmtDue((t.applied_at || "").slice(0, 10)))}</span>
      <a href="/api/applications/${app.id}/files/resume-${esc(resumeVer)}.pdf" target="_blank" rel="noopener">resume PDF ${esc(resumeVer)} ↗</a>
      <a href="/api/applications/${app.id}/files/cover-${coverVer}.pdf" target="_blank" rel="noopener">cover PDF ${coverVer} ↗</a>
    </div>
    <div class="field tailor-letter">
      <span class="field-label">Cover letter</span>
      <textarea class="notes-area" data-tailor-field="cover_letter"${lock}>${esc(t.cover_letter)}</textarea>
      <button type="button" class="btn btn-ghost btn-refine" data-action="tailor-refine"${lock}>Refine (remove AI tells)</button>
    </div>
    <div class="title-chip-row">
      <button class="btn btn-accent" data-action="tailor-rerender"${lock}>${busy === "rerender" ? `${SPINNER}Rendering…` : "Re-render cover PDF"}</button>
      <button class="btn btn-ghost" data-action="tailor-generate"${lock}>New tailoring</button>
    </div>`;
}

function tailoringSection(app) {
  const busy = state.tailorBusy?.id === app.id ? state.tailorBusy.kind : null;
  const t = state.tailoringCache.get(app.id); // undefined = loading, null = none
  let body;
  if (busy === "generate") {
    body = `<p class="tailor-analysis tailor-generating">${SPINNER}<span>Generating change plan + cover letter… (this can take up to a minute)</span></p>`;
  } else if (t === undefined) {
    body = emptyState("Loading…");
  } else if (t === null) {
    body = `
      <div class="tailor-start">
        <input class="text-input" data-tailor-input="instructions" placeholder="Optional instructions — tone, what to emphasize…" />
        <button class="btn btn-accent" data-action="tailor-generate">Tailor resume + cover letter</button>
      </div>`;
  } else if (t.status === "pending") {
    body = tailorPendingHtml(t, busy);
  } else {
    body = tailorAppliedHtml(app, t, busy);
  }
  return `
    <div class="section">
      <div class="section-head">
        <h2 class="section-title">Tailoring${helpHintHtml("tailoring")}</h2>
      </div>
      ${body}
    </div>`;
}

function detailPane(app) {
  if (!app) {
    return `
      <div class="detail-empty">
        <div class="detail-empty-mark">${HQ_MARK}</div>
        <p>Select an application to track its pipeline status, next step, and reminders.</p>
      </div>`;
  }

  return `
    <div class="detail-content" data-id="${app.id}">
      <div class="detail-head">
        <button class="detail-back" data-action="close-detail" title="Back to list">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="15 18 9 12 15 6"/></svg>
          <span>Back</span>
        </button>
        <div class="detail-head-id">
          <a class="co-logo-link" href="#/companies/${app.company_id}" aria-label="View ${esc(app.company_name)} in Companies">${companyLogoHtml({ name: app.company_name, logo: app.company_logo }, { size: "lg" })}</a>
          <div class="detail-head-id-main">
            <div class="detail-head-row">
              <div class="detail-eyebrow"><a class="detail-eyebrow-link" href="#/companies/${app.company_id}">${esc(app.company_name)}</a></div>
            </div>
            <h2 class="detail-title">${esc(app.job_title)}</h2>
          </div>
        </div>
        <div class="detail-subhead">
          <span class="status-pair"><span class="appstatus-dot appstatus-${esc(app.status)}"></span>${esc(app.status)}</span>
          ${delistedBandHtml(app)}
          <a href="#/jobs/${app.job_id}">view job →</a>
          ${app.job_url ? `<a href="${escUrl(app.job_url)}" target="_blank" rel="noopener">posting ↗</a>` : ""}
          ${fitChip(app)}
        </div>
      </div>

      <div class="control-row">
        <div class="field">
          <span class="field-label">Status${helpHintHtml("application-status")}</span>
          <select data-field="status" aria-label="Status">
            ${STATUSES.map((s) => `<option value="${s}"${s === app.status ? " selected" : ""}>${s}</option>`).join("")}
          </select>
        </div>
        <div class="field">
          <span class="field-label">Applied</span>
          ${dateFieldHtml(app.applied_date, { field: "applied_date", ariaLabel: "Applied", title: fmtFullDate(app.applied_date) })}
        </div>
        <div class="field">
          <span class="field-label">Next step</span>
          <input data-field="next_step" aria-label="Next step" value="${esc(app.next_step || "")}" placeholder="e.g. send work samples" />
        </div>
        <div class="field">
          <span class="field-label">Next step date</span>
          ${dateFieldHtml(app.next_step_date, { field: "next_step_date", ariaLabel: "Next step date", title: fmtFullDate(app.next_step_date) })}
        </div>
        <div class="field">
          <span class="field-label">Resume version</span>
          <input data-field="resume_version" aria-label="Resume version" value="${esc(app.resume_version || "")}" placeholder="stamped by Apply" />
        </div>
      </div>

      <div class="section">
        <div class="section-head">
          <h2 class="section-title">Actions</h2>
        </div>
        <div class="title-chip-row">
          <button class="btn" data-action="log-activity">Log activity</button>
          <button class="btn btn-ghost" data-action="compose">Compose</button>
          <button class="btn btn-ghost btn-danger detail-delete" data-action="delete">Delete</button>
        </div>
      </div>

      ${tailoringSection(app)}

      ${documentsSection(app)}

      ${remindersSection(app)}

      <div class="section">
        <div class="section-head">
          <h2 class="section-title">Cover note</h2>
        </div>
        <textarea class="notes-area" data-field="cover_note" aria-label="Cover note" placeholder="Cover letter / note — applying a tailoring stamps the final letter here.">${esc(app.cover_note || "")}</textarea>
      </div>

      <div class="section">
        <div class="section-head">
          <h2 class="section-title">Activity</h2>
        </div>
        ${activityTimelineHtml(state.activityCache.get(app.id) || [])}
      </div>
    </div>`;
}


/* Documents (2026-07-21): externally-customized resumes live beside the
   generated tailoring PDFs in data/applications/<id>/ — the user customizes
   every serious application, often off-app. Generated files have no X. */
function fmtBytes(n) {
  if (n >= 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  if (n >= 1024) return `${Math.round(n / 1024)} KB`;
  return `${n} B`;
}

function documentsSection(app) {
  const files = state.filesCache.get(app.id);
  const rows = files === undefined
    ? emptyState("Loading…")
    : files.length === 0
      ? emptyState("No documents yet — attach the resume you actually sent.")
      : files.map((f) => `
          <div class="doc-row">
            <a href="/api/applications/${app.id}/files/${encodeURIComponent(f.name)}" target="_blank" rel="noopener">${esc(f.name)} ↗</a>
            <span class="doc-meta">${fmtBytes(f.size)}${f.generated ? " · generated" : ""}</span>
            ${f.generated ? "" : `<button type="button" class="doc-delete" data-action="doc-delete" data-name="${esc(f.name)}" aria-label="Remove ${esc(f.name)}">✕</button>`}
          </div>`).join("");
  return `
    <div class="section">
      <div class="section-head">
        <h2 class="section-title">Documents</h2>
        <button class="btn btn-ghost" data-action="doc-upload">Add file…</button>
        <input type="file" data-doc-input accept=".pdf,.docx,.doc,.txt,.md,.html" hidden />
      </div>
      <div class="docs-list">${rows}</div>
    </div>`;
}

/* The 'applied' event and compose drafts log against the job, manual notes
   against the application — the timeline shows both, merge-sorted. */
async function loadActivities(app) {
  if (state.activityCache.has(app.id)) return;
  try {
    const [own, jobActs] = await Promise.all([
      api.listActivities({ entity_type: "application", entity_id: app.id }),
      api.listActivities({ entity_type: "job", entity_id: app.job_id }),
    ]);
    const merged = [...own, ...jobActs].sort(
      (a, b) => (b.date || "").localeCompare(a.date || "") || b.id - a.id
    );
    state.activityCache.set(app.id, merged);
  } catch {
    return; // timeline just stays empty; the rest of the pane works
  }
  if (state.selectedId === app.id) paint();
}

async function loadFiles(app, { force = false } = {}) {
  if (!force && state.filesCache.has(app.id)) return;
  try {
    state.filesCache.set(app.id, await api.listApplicationFiles(app.id));
  } catch {
    return; // section stays on Loading…; reselect retries
  }
  if (state.selectedId === app.id) paint();
}

async function loadTailoring(app) {
  if (state.tailoringCache.has(app.id)) {
    loadTailorChat(app, state.tailoringCache.get(app.id));
    return;
  }
  try {
    state.tailoringCache.set(app.id, await api.getTailoring(app.id));
  } catch (error) {
    if (error.status !== 404) return; // section stays on Loading…; reselect retries
    state.tailoringCache.set(app.id, null);
  }
  if (state.selectedId === app.id) paint();
  loadTailorChat(app, state.tailoringCache.get(app.id));
}

/* Lazy and pending-only: applied/discarded tailorings keep their thread in
   the DB but never show it (the owner's 7f decision — the applied view stays clean). */
async function loadTailorChat(app, t) {
  if (!t || t.status !== "pending" || state.tailorChatCache.has(t.id)) return;
  try {
    state.tailorChatCache.set(t.id, await api.getTailoringMessages(t.id));
  } catch {
    return; // thread stays on Loading…; reselect retries
  }
  if (state.selectedId === app.id) paint();
}

function scrollChatIntoView() {
  const messages = root.querySelectorAll(".tailor-chat-msg");
  if (messages.length) messages[messages.length - 1].scrollIntoView({ block: "nearest" });
}

async function tailorChatSend(app, t) {
  const input = root.querySelector("[data-tailor-chat-input]");
  const text = input ? input.value.trim() : "";
  if (!text) return;
  const thread = state.tailorChatCache.get(t.id) || [];
  state.tailorChatCache.set(t.id, thread);
  state.tailorBusy = { id: app.id, kind: "chat" };
  let pushed = false;
  try {
    // Flush an un-debounced letter edit first — the turn snapshots the letter
    // server-side, so an unflushed edit would be silently overwritten.
    const textarea = root.querySelector('[data-tailor-field="cover_letter"]');
    if (textarea && textarea.value !== t.cover_letter) {
      clearTimeout(tailorSaveTimer);
      const flushed = await api.patchTailoring(t.id, { cover_letter: textarea.value });
      state.tailoringCache.set(app.id, flushed);
    }
    thread.push({ role: "user", content: text }); // optimistic bubble
    pushed = true;
    if (state.selectedId === app.id) {
      paint();
      scrollChatIntoView();
    }
    const result = await api.chatTailoring(t.id, { message: text });
    thread.push(result.message);
    state.tailoringCache.set(app.id, result.tailoring); // diff rows + letter from server truth
    (result.warnings || []).forEach((w) => toast(w));
    state.tailorBusy = null;
    if (state.selectedId === app.id) {
      paint();
      scrollChatIntoView();
    }
  } catch (error) {
    if (pushed) thread.pop();
    toast(error.detail || error.message, { error: true });
    state.tailorBusy = null;
    if (state.selectedId === app.id) {
      paint();
      const again = root.querySelector("[data-tailor-chat-input]");
      if (again) again.value = text; // draft back so the user can just resend
      scrollChatIntoView();
    }
  }
}

// Tailor actions are single-flight — state.tailorBusy is one global slot, so a click that
// lands while any tailor action is in flight gets dropped. Toast it instead of silently
// no-op'ing (a stuck busy would otherwise read as a dead button; the api.js request timeout
// guarantees busy always clears, so this only fires during a genuine in-flight action).
function whenTailorIdle(run) {
  if (state.tailorBusy !== null) {
    toast("A tailoring action is still running — please wait.");
    return;
  }
  run();
}

async function tailorGenerate(app) {
  // capture before paint() wipes the input
  const instructions = root.querySelector("[data-tailor-input]")?.value.trim() || null;
  state.tailorBusy = { id: app.id, kind: "generate" };
  paint();
  try {
    const t = await api.tailorApplication(app.id, { instructions });
    state.tailoringCache.set(app.id, t);
    state.tailorChatCache.set(t.id, []); // fresh tailoring = empty thread, skip the GET
    (t.warnings || []).forEach((w) => toast(w));
    chime(); // generation runs 30s–3min — ding the tabbed-away user
  } catch (error) {
    toast(error.detail || error.message, { error: true });
    buzz();
  }
  state.tailorBusy = null;
  if (state.selectedId === app.id) paint();
}

async function tailorToggle(app, t, changeId, approved) {
  const change = t.change_plan.find((c) => c.id === changeId);
  if (!change) return;
  change.approved = approved; // optimistic; the PATCH below confirms
  paint();
  try {
    const updated = await api.patchTailoring(t.id, {
      changes: [{ id: changeId, approved }],
    });
    state.tailoringCache.set(app.id, updated); // no repaint — state matches
  } catch (error) {
    change.approved = !approved;
    paint();
    toast(error.detail || error.message, { error: true });
  }
}

async function tailorBulk(app, t, approveAll) {
  try {
    const updated = await api.patchTailoring(t.id, {
      changes: t.change_plan.map((c) => ({ id: c.id, approved: approveAll })),
    });
    state.tailoringCache.set(app.id, updated);
    paint();
  } catch (error) {
    toast(error.detail || error.message, { error: true });
  }
}

async function tailorApply(app, t) {
  // flush an un-debounced letter edit before rendering from server state
  const textarea = root.querySelector('[data-tailor-field="cover_letter"]');
  state.tailorBusy = { id: app.id, kind: "apply" };
  try {
    if (textarea && textarea.value !== t.cover_letter) {
      clearTimeout(tailorSaveTimer);
      const flushed = await api.patchTailoring(t.id, { cover_letter: textarea.value });
      state.tailoringCache.set(app.id, flushed);
    }
    paint();
    const result = await api.applyTailoring(t.id);
    state.tailoringCache.set(app.id, result.tailoring);
    const index = state.applications.findIndex((a) => a.id === app.id);
    if (index !== -1) state.applications[index] = result.application;
    state.activityCache.delete(app.id);
    toast(`Applied — resume v${result.tailoring.version} + cover letter rendered`);
    (result.warnings || []).forEach((w) => toast(w, { error: true }));
    chime(); // PDF render takes seconds-to-tens-of-seconds
  } catch (error) {
    toast(error.detail || error.message, { error: true });
    buzz();
  }
  state.tailorBusy = null;
  if (state.selectedId === app.id) {
    paint();
    const current = selected();
    if (current) loadActivities(current);
  }
}

async function tailorRerender(app, t) {
  // Capture the edited letter before paint() rebuilds (and resets) the textarea.
  const textarea = root.querySelector('[data-tailor-field="cover_letter"]');
  const letter = textarea ? textarea.value : t.cover_letter;
  if (letter.trim() === (t.cover_letter || "").trim()) {
    toast("Edit the letter before re-rendering — no changes yet.");
    return;
  }
  state.tailorBusy = { id: app.id, kind: "rerender" };
  paint();
  try {
    const result = await api.rerenderCover(t.id, { cover_letter: letter });
    state.tailoringCache.set(app.id, result.tailoring);
    const index = state.applications.findIndex((a) => a.id === app.id);
    if (index !== -1) state.applications[index] = result.application;
    state.activityCache.delete(app.id); // the re-render logs an activity row
    toast(`Cover letter re-rendered — v${result.tailoring.version}`);
  } catch (error) {
    toast(error.detail || error.message, { error: true });
    buzz();
  }
  state.tailorBusy = null;
  if (state.selectedId === app.id) {
    paint();
    const current = selected();
    if (current) loadActivities(current);
  }
}

async function tailorRefine(app, t) {
  // Opt-in AI-tell scrub of the letter, in place (one Sonnet call). No paint():
  // the applied textarea is ephemeral (paint would reseed it from t), so set the
  // value directly and, for a pending draft, persist the refined text.
  const textarea = root.querySelector('[data-tailor-field="cover_letter"]');
  if (!textarea || !textarea.value.trim()) return;
  const btn = root.querySelector('[data-action="tailor-refine"]');
  const label = btn?.textContent;
  state.tailorBusy = { id: app.id, kind: "refine" }; // blocks other tailor actions
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Refining…";
  }
  textarea.disabled = true;
  try {
    const result = await api.refineTells({ text: textarea.value });
    textarea.value = result.refined_text;
    if (t.status === "pending") {
      state.tailoringCache.set(app.id, await api.patchTailoring(t.id, { cover_letter: result.refined_text }));
    }
    const fixed = result.tells_fixed?.length ? ` (fixed: ${result.tells_fixed.join(", ")})` : "";
    toast(`Refined: reads-as-human ${result.score ?? "?"}/10${fixed}`);
  } catch (error) {
    toast(error.detail || error.message, { error: true });
    buzz();
  } finally {
    state.tailorBusy = null;
    if (btn) {
      btn.disabled = false;
      btn.textContent = label;
    }
    textarea.disabled = false;
  }
}

async function tailorDiscard(app, t) {
  const ok = await confirmModal({
    title: "Discard this tailoring?",
    message: "The change plan and cover letter draft are kept in history but won't be shown again.",
    confirmLabel: "Discard",
  });
  if (!ok) return;
  try {
    await api.discardTailoring(t.id);
    state.tailoringCache.delete(app.id);
    paint();
    loadTailoring(app); // an earlier applied version may exist
  } catch (error) {
    toast(error.detail || error.message, { error: true });
  }
}

async function tailorRegenerate(app, t) {
  const ok = await confirmModal({
    title: "Regenerate tailoring?",
    message: "Discards the current plan — approvals and letter edits included — and asks the agent again.",
    confirmLabel: "Regenerate",
  });
  if (!ok) return;
  try {
    await api.discardTailoring(t.id);
  } catch (error) {
    toast(error.detail || error.message, { error: true });
    return;
  }
  state.tailoringCache.delete(app.id);
  await tailorGenerate(app);
}

function template() {
  const rows = filtered();
  return `
    <div class="filters">
      <div class="filter-group">
        ${ALL_DD.map((dd) => ddTemplate(dd, state.filters)).join("")}
      </div>
      ${searchBoxHtml("Search applications…", state.filters.q)}
    </div>
    <div class="layout">
      <div class="list-pane${state.mobileDetail ? " mobile-hide" : ""}">
        ${
          rows.length
            ? groupedList(rows)
            : emptyState(
                state.applications.length
                  ? "No applications match the current filters."
                  : `No applications yet — start one from a <a href="#/jobs">job's</a> detail pane.`,
                { pad: true, html: !state.applications.length }
              )
        }
      </div>
      <div class="detail-pane${state.mobileDetail ? " mobile-show" : ""}">
        ${detailPane(selected())}
      </div>
    </div>`;
}

function renderStats() {
  const open = state.applications.filter(isOpen);
  const due = open.filter((a) => a.next_step_date && a.next_step_date <= localToday());
  setStats([
    { value: open.length, label: "Open" },
    { value: due.length, label: pluralize(due.length, "Step due", "Steps due") },
  ]);
}

function paint(opts = {}) {
  const top = getListScroll(root);
  if (top !== null) state.listScroll = top;
  const dtop = getDetailScroll(root);
  if (dtop !== null) state.detailScroll = dtop;
  root.innerHTML = template();
  setListScroll(root, state.listScroll);
  // Selecting a different item opens its detail at the top; every other repaint
  // (in-detail edits, status changes, async loaders) keeps the reader's place.
  if (opts.detailToTop) state.detailScroll = 0;
  setDetailScroll(root, state.detailScroll);
  renderStats();
}

function repaintList() {
  const pane = root.querySelector(".list-pane");
  if (!pane) return;
  const rows = filtered();
  pane.innerHTML = rows.length
    ? groupedList(rows)
    : emptyState("No applications match the current filters.", { pad: true });
}

let saveTimer = null;
let tailorSaveTimer = null;

async function save(app, overrides, { quiet = false } = {}) {
  try {
    const updated = await api.updateApplication(app.id, payload(app, overrides));
    const index = state.applications.findIndex((a) => a.id === app.id);
    if (index !== -1) state.applications[index] = updated;
    if (!quiet) {
      // status→applied flips the job and may spawn a follow-up suggestion;
      // refresh both cheap payloads so the card/timeline appear in place.
      if (overrides.status) {
        state.activityCache.delete(app.id);
        try {
          state.suggestions = (await api.getSuggestions()).reminders;
        } catch {
          /* card just appears on the next load */
        }
      }
      paint();
      if (overrides.status) loadActivities(state.applications[index] || app);
    }
  } catch (error) {
    if (quiet) return; // the focusout save retries and surfaces the error
    toast(error.detail || error.message, { error: true });
    paint();
  }
}

function fieldValue(field, element) {
  const raw = element.value.trim();
  return raw || null;
}

async function deleteSelected() {
  const app = selected();
  if (!app) return;
  const ok = await confirmModal({
    title: `Delete application for ${app.job_title}?`,
    message:
      "This removes the application, its notes, and its reminders. An applied job goes back to active.",
  });
  if (!ok) return;
  try {
    await api.deleteApplication(app.id);
    state.selectedId = null;
    state.mobileDetail = false;
    state.activityCache.delete(app.id);
    setDetailHash("applications", null); // Back must not return to the deleted id
    await load();
    paint();
    toast("Application deleted");
  } catch (error) {
    toast(error.detail || error.message, { error: true });
  }
}

async function onReminderSuggestion(key, verb) {
  try {
    const result = await api.actOnReminderSuggestion(key, verb);
    state.suggestions = state.suggestions.filter((s) => s.key !== key);
    if (result.reminder) state.reminders.push(result.reminder);
    paint();
    toast(verb === "accept" ? "Reminder added" : "Suggestion ignored");
  } catch (error) {
    if (error.status === 404) {
      // stale card — the underlying event changed; drop it
      state.suggestions = state.suggestions.filter((s) => s.key !== key);
      paint();
    }
    toast(error.detail || error.message, { error: true });
  }
}

bindOutsideClose(() => root);

function onClick(event) {
  const target = event.target.closest("[data-action]");
  if (!target || !root.contains(target)) return;
  switch (target.dataset.action) {
    case "select": {
      state.selectedId = Number(target.dataset.id);
      state.mobileDetail = true;
      paint({ detailToTop: true });
      const app = selected();
      if (app) {
        loadActivities(app);
        loadTailoring(app);
        loadFiles(app);
      }
      setDetailHash("applications", state.selectedId);
      break;
    }
    case "close-detail":
      // Our own history entry → back() pops it (popstate → hashchange → render);
      // cold deep-link entry → rewrite the hash in place and close locally.
      if (history.state?.hqDetail) {
        history.back();
      } else {
        state.mobileDetail = false;
        setDetailHash("applications", null);
        paint();
      }
      break;
    case "search-clear": {
      state.filters.q = "";
      const input = root.querySelector(".search-box");
      input.value = "";
      target.classList.add("hide");
      repaintList();
      input.focus();
      break;
    }
    case "dd-toggle": {
      const key = target.dataset.dd;
      closeDropdowns(root, key);
      const panel = root.querySelector(`.filter-dd-panel[data-dd="${key}"]`);
      // an exiting panel counts as closed, so a re-click mid-fade reopens
      if (isPopOpen(panel)) hidePop(panel);
      else showPop(panel);
      break;
    }
    case "dd-clear": {
      const key = target.dataset.dd;
      state.filters[key].clear();
      const panel = root.querySelector(`.filter-dd-panel[data-dd="${key}"]`);
      panel.querySelectorAll("input[type=checkbox]").forEach((box) => (box.checked = false));
      updateToggle(root, ddByKey(key), state.filters);
      repaintList(); // panel stays open
      break;
    }
    case "delete":
      deleteSelected();
      break;
    case "doc-upload": {
      const input = root.querySelector("[data-doc-input]");
      if (input) input.click();
      break;
    }
    case "doc-delete":
      deleteDocument(target.dataset.name);
      break;
    case "rem-suggestion":
      onReminderSuggestion(target.dataset.key, target.dataset.verb);
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
    case "add-reminder": {
      const app = selected();
      if (!app) break;
      openReminderModal({
        prefill: {
          title: `Follow up — ${entityLabel(app)}`,
          type: "followup_application",
          due_date: localToday(7),
          entity_type: "application",
          entity_id: app.id,
          entity_label: entityLabel(app),
        },
        onSaved: (saved) => {
          if (saved) state.reminders.push(saved);
          paint();
        },
      });
      break;
    }
    case "log-activity": {
      const app = selected();
      if (!app) break;
      openActivityModal({
        entity_type: "application",
        entity_id: app.id,
        entity_label: entityLabel(app),
        onSaved: () => {
          state.activityCache.delete(app.id);
          loadActivities(app);
        },
      });
      break;
    }
    case "tailor-generate": {
      const app = selected();
      if (app) whenTailorIdle(() => tailorGenerate(app));
      break;
    }
    case "tailor-toggle": {
      const app = selected();
      const t = app && state.tailoringCache.get(app.id);
      if (t) tailorToggle(app, t, target.dataset.cid, target.checked);
      break;
    }
    case "tailor-bulk": {
      const app = selected();
      const t = app && state.tailoringCache.get(app.id);
      if (t) tailorBulk(app, t, target.dataset.verb === "all");
      break;
    }
    case "tailor-apply": {
      const app = selected();
      const t = app && state.tailoringCache.get(app.id);
      if (t) whenTailorIdle(() => tailorApply(app, t));
      break;
    }
    case "tailor-rerender": {
      const app = selected();
      const t = app && state.tailoringCache.get(app.id);
      if (t) whenTailorIdle(() => tailorRerender(app, t));
      break;
    }
    case "tailor-refine": {
      const app = selected();
      const t = app && state.tailoringCache.get(app.id);
      if (t) whenTailorIdle(() => tailorRefine(app, t));
      break;
    }
    case "tailor-discard": {
      const app = selected();
      const t = app && state.tailoringCache.get(app.id);
      if (t) tailorDiscard(app, t);
      break;
    }
    case "tailor-regenerate": {
      const app = selected();
      const t = app && state.tailoringCache.get(app.id);
      if (t) whenTailorIdle(() => tailorRegenerate(app, t));
      break;
    }
    case "tailor-chat-send": {
      const app = selected();
      const t = app && state.tailoringCache.get(app.id);
      if (t) whenTailorIdle(() => tailorChatSend(app, t));
      break;
    }
    case "compose": {
      const app = selected();
      if (!app) break;
      // Compose context lives on the job (backend ComposeIn takes job/contact);
      // the draft activity lands on the job timeline, which this pane merges in.
      openComposeModal({
        entity_type: "job",
        entity_id: app.job_id,
        entity_label: entityLabel(app),
        onLogged: () => {
          state.activityCache.delete(app.id);
          loadActivities(app);
        },
      });
      break;
    }
  }
}

function onChange(event) {
  const element = event.target;
  if (element.hasAttribute("data-doc-input")) {
    uploadDocument(element);
    return;
  }
  // filter-pill controls carry data-dd and no data-field — handle before autosave
  if (element.dataset.dd && element.type === "radio") {
    state.filters[element.dataset.dd] = element.value;
    updateToggle(root, ddByKey(element.dataset.dd), state.filters);
    repaintList();
    closeDropdowns(root); // a radio choice is terminal — close the panel
    return;
  }
  if (element.dataset.dd && element.type === "checkbox") {
    const set = state.filters[element.dataset.dd];
    if (element.checked) set.add(element.value);
    else set.delete(element.value);
    updateToggle(root, ddByKey(element.dataset.dd), state.filters);
    repaintList(); // panel stays open for multi-checking
    return;
  }
  const field = element.dataset.field;
  const app = selected();
  if (!field || !app) return;
  // selects commit on change; date fields too (the picker writes the value
  // and dispatches a synthetic change — see lib/datepicker.js)
  if (element.tagName !== "SELECT" && !("datepicker" in element.dataset)) return;
  save(app, { [field]: fieldValue(field, element) });
}

async function uploadDocument(input) {
  const app = selected();
  const file = input.files && input.files[0];
  input.value = ""; // same file re-selectable after a failure
  if (!app || !file) return;
  try {
    const stored = await api.uploadApplicationFile(app.id, file);
    toast(`Added ${stored.name}`);
  } catch (error) {
    toast(error.message || "Upload failed");
    return;
  }
  loadFiles(app, { force: true });
}

async function deleteDocument(name) {
  const app = selected();
  if (!app) return;
  try {
    await api.deleteApplicationFile(app.id, name);
    toast(`Removed ${name}`);
  } catch (error) {
    toast(error.message || "Delete failed");
    return;
  }
  loadFiles(app, { force: true });
}

function onFocusOut(event) {
  const element = event.target;
  if (element.dataset.tailorField === "cover_letter") {
    const app = selected();
    const t = app && state.tailoringCache.get(app.id);
    if (!t || t.status !== "pending" || element.value === t.cover_letter) return;
    clearTimeout(tailorSaveTimer);
    api
      .patchTailoring(t.id, { cover_letter: element.value })
      .then((updated) => state.tailoringCache.set(app.id, updated))
      .catch((error) => toast(error.detail || error.message, { error: true }));
    return;
  }
  const field = element.dataset.field;
  const app = selected();
  if (!field || !app || element.tagName === "SELECT" || "datepicker" in element.dataset) return;
  clearTimeout(saveTimer);
  const value = fieldValue(field, element);
  if (value === (app[field] ?? null)) return;
  save(app, { [field]: value });
}

function onKeyDown(event) {
  // chat-conventional composer: Enter sends, Shift+Enter inserts a newline
  if (!event.target.matches?.("[data-tailor-chat-input]")) return;
  if (event.key !== "Enter" || event.shiftKey) return;
  event.preventDefault();
  const app = selected();
  const t = app && state.tailoringCache.get(app.id);
  if (t) whenTailorIdle(() => tailorChatSend(app, t));
}

function onInput(event) {
  const element = event.target;
  if (element.dataset.action === "search") {
    state.filters.q = element.value;
    root.querySelector(".search-clear")?.classList.toggle("hide", !state.filters.q);
    repaintList();
    return;
  }
  if (element.dataset.tailorField === "cover_letter") {
    const app = selected();
    const t = app && state.tailoringCache.get(app.id);
    if (!t || t.status !== "pending") return;
    clearTimeout(tailorSaveTimer);
    // same iOS-never-blurs safety net as the application fields below
    tailorSaveTimer = setTimeout(() => {
      api
        .patchTailoring(t.id, { cover_letter: element.value })
        .then((updated) => state.tailoringCache.set(app.id, updated))
        .catch(() => {}); // the focusout/apply-flush path retries and surfaces errors
    }, 700);
    return;
  }
  const field = element.dataset.field;
  if (!field || element.tagName === "SELECT" || "datepicker" in element.dataset) return;
  const appId = selected()?.id;
  if (!appId) return;
  clearTimeout(saveTimer);
  // iOS Safari often never blurs an input (tapping outside isn't a blur), so a
  // focusout-only save loses edits; autosave while typing as the safety net.
  saveTimer = setTimeout(() => {
    const app = state.applications.find((a) => a.id === appId);
    if (!app) return;
    const value = fieldValue(field, element);
    if (value === (app[field] ?? null)) return;
    save(app, { [field]: value }, { quiet: true });
  }, 700);
}

export async function render(container, preselectId = null) {
  root = container;
  renderLoading(container);
  container.onclick = onClick;
  container.onchange = onChange;
  container.oninput = onInput;
  setFocusOut(container, onFocusOut);
  container.onkeydown = onKeyDown;
  setRowKeys(container, onClick);
  try {
    await load();
  } catch (error) {
    renderLoadError(container, error, () => render(container, preselectId));
    setStats([]);
    return;
  }
  // Jobs-view actions (Mark applied, compose) feed this timeline, so a cache
  // held across view switches goes stale — fetch fresh per mount.
  state.activityCache.clear();
  state.tailoringCache.clear();
  state.tailorChatCache.clear();
  state.tailorBusy = null;
  if (preselectId && state.applications.some((a) => a.id === preselectId)) {
    state.selectedId = preselectId;
    state.mobileDetail = true;
  } else if (!preselectId) {
    // back/forward to the bare list: keep the desktop pane's selection, but
    // the phone must land on the list
    state.mobileDetail = false;
  }
  paint();
  // Route-driven selection only: an in-list click never yanks the pane.
  if (preselectId && state.selectedId === preselectId) revealSelected(root);
  const app = selected();
  if (app) {
    loadActivities(app);
    loadTailoring(app);
    loadFiles(app);
  }
}
