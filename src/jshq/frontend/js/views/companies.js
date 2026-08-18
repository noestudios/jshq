/* Companies view: filterable list pane + detail pane with edit-in-place,
   LinkedIn manual-check links, associated contacts, add/delete modals. */

import { api } from "../api.js";
import {
  bindOutsideClose,
  closeDropdowns,
  ddTemplate,
  updateToggle,
} from "../lib/filterDd.js";
import {
  closeModal,
  confirmModal,
  emptyState,
  esc,
  escUrl,
  fitChip,
  fmtAgo,
  fmtStamp,
  getDetailScroll,
  getListScroll,
  HQ_MARK,
  isResolvedApplication,
  isDelisted,
  openModal,
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
  hidePop,
  isPopOpen,
  toast,
} from "../lib/ui.js";
import { buzz, chime } from "../lib/notify.js";
import { openContactModal } from "../lib/contactModal.js";
import { titleSearchUrl, combinedSearchUrl, hasMultipleTitles, companyLookupUrl } from "../lib/linkedin.js";
import { BULK_RETRY_MIN, failReason } from "../lib/ats.js";
import { companyLogoHtml } from "../lib/logo.js";
import * as onboardingTracker from "../lib/onboardingTracker.js";

/* Statuses and values-fit live here rather than config: a pipeline stage and
   a values read are the same for every job seeker, so there is nothing for a
   user's doc to configure. */
export const STATUSES = ["prospect", "targeting", "outreach", "applied", "interviewing", "offer", "closed"];
export const VALUES_FIT = ["high", "medium", "low", "unknown"];

/* Multiselect dropdown pills (the jobs-view convention, shared via filterDd). */
const DD_FILTERS = [
  {
    key: "status",
    label: "Status",
    type: "multi",
    options: STATUSES.map((s) => ({ value: s, label: s })),
  },
  {
    key: "ats",
    label: "ATS",
    type: "multi",
    options: [
      { value: "ok", label: "connected" },
      { value: "none", label: "manual" },
      { value: "stale", label: "stale" },
      { value: "failing", label: "failing" },
    ],
  },
  {
    // Single-select: a company either has active listings (active_job_count > 0,
    // which already excludes Tier-1 hard fails) or it doesn't. "" = show all.
    key: "listings",
    label: "Listings",
    type: "radio",
    options: [
      { value: "", label: "Any listings" },
      { value: "active", label: "Has listings" },
      { value: "none", label: "No listings" },
    ],
  },
];
const ddByKey = (key) => DD_FILTERS.find((d) => d.key === key);

const state = {
  companies: [],
  contacts: [],
  selectedId: null,
  filters: { status: new Set(), ats: new Set(), listings: "", q: "" },
  mobileDetail: false,
  listScroll: 0,
  detailScroll: 0,
};

/* Company-settings collapse state (QA pass 1): ids stay expanded while
   browsing within a mount; everything starts collapsed. */
const expandedSettings = new Set();

/* Per-company job rows for the "Top jobs" detail section, fetched lazily on
   select (GET /api/jobs?company_id=<id>). Keyed by company id; a missing entry
   means "not fetched yet" and the section stays omitted until it lands. */
const topJobsCache = new Map();
const TOP_JOBS_FIT_MIN = 35; // promising jobs only — above the very-low scores

let root = null;

/* The 13 columns PUT accepts; mirrors CompanyIn on the backend. */
function payload(company, overrides = {}) {
  const fields = [
    "name", "location", "priority", "status", "values_fit", "website",
    "careers_url", "ats_type", "ats_slug", "notes",
    "linkedin_company_ids", "linkedin_title_searches",
  ];
  const body = {};
  for (const field of fields) body[field] = company[field] ?? null;
  body.linkedin_company_ids = company.linkedin_company_ids || [];
  body.linkedin_title_searches = company.linkedin_title_searches || [];
  return { ...body, ...overrides };
}

async function load() {
  [state.companies, state.contacts] = await Promise.all([api.listCompanies(), api.listContacts()]);
}

function selected() {
  return state.companies.find((c) => c.id === state.selectedId) || null;
}

/* Lazily fetch a company's jobs for the Top-jobs section, then repaint if it's
   still selected (mirrors pollAtsStatus's fetch-then-repaint). Cached per id; a
   failed fetch is left uncached so a later select retries. */
async function loadTopJobs(companyId) {
  if (topJobsCache.has(companyId)) return;
  let jobs;
  try {
    jobs = await api.listJobs({ company_id: companyId });
  } catch {
    return; // informational; leave uncached so reselect retries
  }
  topJobsCache.set(companyId, jobs);
  if (state.selectedId === companyId) paint();
}

/* Listings filter category: a company has active listings iff active_job_count
   > 0 (which already excludes Tier-1 hard fails). Mirrors jobCountPill's own
   `n ? … : "zero"` test so the filter and the pill never disagree. */
function listingStatus(company) {
  return (company.active_job_count ?? 0) > 0 ? "active" : "none";
}

function filtered() {
  const { status, ats, listings, q } = state.filters;
  const needle = q.trim().toLowerCase();
  return state.companies.filter((c) => {
    if (status.size && !status.has(c.status)) return false;
    if (ats.size && !ats.has(atsCategory(c))) {
      // A retried board flips to 'checking' the moment the retry starts and
      // would vanish from the 'failing' filter mid-run (the Today banner
      // deep-links exactly that filter) — keep watched rows visible until
      // they settle.
      const watched =
        c.ats_last_status === "checking" && (bulkWatch.has(c.id) || refreshingIds.has(c.id));
      if (!(watched && ats.has("failing"))) return false;
    }
    if (listings && listingStatus(c) !== listings) return false;
    if (needle) {
      const haystack = `${c.name} ${c.location || ""} ${c.notes || ""}`.toLowerCase();
      if (!haystack.includes(needle)) return false;
    }
    return true;
  });
}

function priorityDots(priority) {
  const dots = [1, 2, 3, 4, 5]
    .map((n) => `<span class="priority-dot${priority && n <= priority ? " on" : ""}"></span>`)
    .join("");
  return `<span class="priority-display" title="Priority ${priority ?? "—"}">${dots}</span>`;
}

/* A connected company's job list is "stale" when it may be silently out of
   date: it pulled 0 jobs on its last run, or hasn't been re-checked within the
   refresh window (a day). Mirrors the backend `stale` predicate in
   /api/refresh/status so the list badge, detail, and Today banner agree. */
const STALE_REFRESH_MS = 24 * 60 * 60 * 1000;
function isStaleList(company) {
  const status = company.ats_last_status;
  if (!company.ats_type || company.ats_type === "manual") return false;
  if (!status || status === "checking" || status.startsWith("error:")) return false;
  if (status === "ok: 0 matched") return true;
  const checked = company.ats_last_checked;
  return !!checked && Date.now() - Date.parse(checked) > STALE_REFRESH_MS;
}
function staleReason(company) {
  return company.ats_last_status === "ok: 0 matched"
    ? "Connected but pulling 0 jobs — the board may be empty or the adapter may be broken; verify."
    : `Connected but last checked ${fmtAgo(company.ats_last_checked)} — the job list may be out of date.`;
}

/* ATS connection category — drives the list indicator and the ATS filter.
   "none" = no connectable ATS (marked manual, or detection found none) → the
   user must pull jobs for it by hand. "stale" = connected but the list may be
   out of date (see isStaleList). */
function atsCategory(company) {
  const status = company.ats_last_status;
  if (status === "checking") return "checking";
  if (status && status.startsWith("error:")) return "failing";
  if (!company.ats_type || company.ats_type === "manual" || (status && status.startsWith("none"))) return "none";
  if (isStaleList(company)) return "stale";
  return "ok";
}

/* List-row ATS indicator: a clear "manual" tag for manual/undetected companies
   (previously silent → invisible), rose when the adapter is failing, a quiet
   tag when the last fetch was fine. */
/* The per-board "Refresh board" control — a compact ↻ on a failing/stale row,
   re-fetches just that one company's board (data-action handled in onClick). */
function refreshBtn(id) {
  return ` <button type="button" class="ats-refresh" data-action="refresh-board" data-id="${id}" title="Refresh this board now" aria-label="Refresh this board">↻</button>`;
}

/* The failing-row retry when there is NO connected adapter to re-fetch:
   /refresh 400s ("no connectable ATS board") for a company whose detection
   errored before an ats_type was ever written, so its ↻ must re-run DETECTION
   instead. */
function detectBtn(id) {
  return ` <button type="button" class="ats-refresh" data-action="detect-board" data-id="${id}" title="Re-check for a job board" aria-label="Re-check for a job board">↻</button>`;
}

function connectable(company) {
  return Boolean(company.ats_type && company.ats_type !== "manual");
}

/* Bulk retry-failed toolbar button — only when the failing count clears
   BULK_RETRY_MIN (below that the per-row ↻ is quicker than a bulk run); busy
   and disabled while a retry is being watched. Same btn-collapse pattern as
   the Jobs "Refresh boards" button (collapses to the icon on phones). */
function bulkRetryBtn(rows = state.companies) {
  const failing = rows.filter((c) => atsCategory(c) === "failing").length;
  const busy = bulkWatch.size > 0;
  if (!busy && failing <= BULK_RETRY_MIN) return "";
  const icon = busy ? `<span class="ats-spin">↻</span>` : "↻";
  const label = busy ? `Retrying ${bulkWatch.size}…` : `Refresh failing (${failing})`;
  return `<button class="btn btn-collapse" data-action="refresh-failing"${busy ? " disabled" : ""} aria-label="${busy ? "Retrying failing boards…" : `Refresh all ${failing} failing boards`}"><span aria-hidden="true">${icon}</span><span class="btn-label"> ${label}</span></button>`;
}

/* showOk=false suppresses the "ats ok" pill. That pill is the NULL state —
   "nothing is wrong" — and on a list row it was a badge competing with the
   status and the priority dots to say nothing at all. It stays in
   the detail pane's collapsed settings head, where it is the only ATS signal
   while the section is shut. (Rows whose category is ok but whose
   ats_last_status is falsy already rendered nothing, so this makes an existing
   silence consistent rather than inventing a new one.) */
function atsHealthTag(company, { showOk = true } = {}) {
  const status = company.ats_last_status;
  switch (atsCategory(company)) {
    case "checking":
      // An on-demand board re-fetch and add-time onboarding both sit at
      // 'checking'; refreshingIds tells them apart so the copy is honest.
      return refreshingIds.has(company.id)
        ? `<span class="ats-tag checking" title="Re-fetching this board…"><span class="ats-spin">↻</span> refreshing…</span>`
        : `<span class="ats-tag checking" title="Checking the careers URL for an ATS…">checking…</span>`;
    case "failing":
      return `<span class="ats-tag error" title="${esc(status)}">ats failing</span>${
        connectable(company) ? refreshBtn(company.id) : detectBtn(company.id)
      }`;
    case "none":
      // Carry the real reason when detection recorded one; generic otherwise.
      return `<span class="ats-tag none" title="${
        status && status.startsWith("none")
          ? esc(`${noAtsReason(status)} — pull jobs manually, or re-check from Company settings.`)
          : "No connectable ATS — pull jobs for this company manually."
      }">manual</span>`;
    case "stale":
      return `<span class="ats-tag stale" title="${esc(staleReason(company))}">stale</span>${refreshBtn(company.id)}`;
    default:
      return status && showOk ? `<span class="ats-tag" title="${esc(status)}">ats ok</span>` : "";
  }
}

/* Currently-available job listings (QA pass 1): active jobs only, counted by
   the backend. Faded when zero; neutral-but-discernible when ≥1. */
function jobCountPill(company, { labeled = false } = {}) {
  const n = company.active_job_count ?? 0;
  return `<span class="job-count-pill${n ? "" : " zero"}">${n}${labeled ? ` active job${n === 1 ? "" : "s"}` : ""}</span>`;
}

/* One compact row in the Top-jobs list — fit chip + title + location, with the
   remote tag. Clicking opens that job in the Jobs view (#/jobs/<id>). */
function topJobRow(job) {
  return `
    <div class="company-row job-row top-job-row" data-action="open-job" data-id="${job.id}" role="button" tabindex="0">
      <div class="company-row-head">
        <span class="company-name">${fitChip(job)}${esc(job.title)}</span>
        <span class="company-loc">${esc(job.location || "")}</span>
      </div>
      <div class="company-meta">
        ${job.source === "manual" ? `<span class="source-tag">manual</span>` : ""}
        <span class="remote-tag remote-${esc(job.remote_type || "unknown")}">${esc(job.remote_type || "unknown")}</span>
      </div>
    </div>`;
}

/* "Top jobs" (promising postings, logged 2026-06-14): the company's jobs scored
   above TOP_JOBS_FIT_MIN, plus any the user manually elevated, sorted fit desc.
   Fetched lazily on select; omitted entirely while loading or when there are
   none, so a company with nothing promising shows no empty box. A delisted
   listing (decay-closed, or applied+missed) is history, not a live opening: it
   renders "no longer listed" in the Jobs view but has no marker here, so it's
   excluded (#56) — this also reconciles the count with the "N active jobs"
   header, which never counted it. */
function topJobsSection(company) {
  const jobs = topJobsCache.get(company.id);
  if (!jobs) return ""; // not fetched yet
  const top = jobs
    .filter((j) => !isResolvedApplication(j) && !isDelisted(j) && ((j.fit_score ?? -1) > TOP_JOBS_FIT_MIN || j.manually_elevated))
    .sort(
      (a, b) =>
        (b.fit_score ?? -1) - (a.fit_score ?? -1) ||
        (Date.parse(b.last_seen) || 0) - (Date.parse(a.last_seen) || 0)
    );
  if (!top.length) return "";
  return `
    <div class="section top-jobs">
      <div class="section-head">
        <h2 class="section-title">Top jobs</h2>
        <span class="section-count">${top.length}</span>
      </div>
      ${top.map(topJobRow).join("")}
    </div>`;
}

function listRow(company) {
  const isSelected = company.id === state.selectedId;
  return `
    <div class="company-row${isSelected ? " selected" : ""}" data-action="select" data-id="${company.id}" role="button" tabindex="0">
      <div class="co-row-flex">
        ${companyLogoHtml({ name: company.name, logo: company.logo_url }, { size: "sm" })}
        <div class="co-row-rest">
          <div class="company-row-head">
            <span class="company-name">${esc(company.name)}${jobCountPill(company)}</span>
            <span class="company-loc">${esc(company.location || "")}</span>
          </div>
          <div class="company-meta">
            <span class="status-dot status-${esc(company.status || "")}"></span>
            <span>${esc(company.status || "—")}</span>
            ${priorityDots(company.priority)}
            ${atsHealthTag(company, { showOk: false })}
          </div>
        </div>
      </div>
    </div>`;
}

/* User-typed URLs may lack a scheme ("discord.com" — the add form and the
   wizard's own placeholders suggest exactly that shape). escUrl rejects those
   outright, which rendered <a href=""> — a link that just reloads the app.
   Assume https for scheme-less values; anything escUrl still rejects (a
   javascript: payload, say) renders the fallback instead of a dead anchor. */
function companyLink(url, label, fallback = "") {
  const raw = String(url || "").trim();
  const href = escUrl(/^[a-z][a-z0-9+.-]*:/i.test(raw) ? raw : raw && `https://${raw}`);
  return href ? `<a href="${href}" target="_blank" rel="noopener">${label}</a>` : fallback;
}

/* ATS health: last successful fetch per company. Manual companies
   get their careers link as the one-click check instead of fetch status.
   Renders inside the collapsible Company settings section (QA pass 1). */
function atsBody(company) {
  const status = company.ats_last_status;
  // Onboarding states (QA pass 2): shown while/after the add-time ATS check,
  // possibly before an ats_type has been written.
  if (status === "checking") {
    return `<div class="ats-health">${
      refreshingIds.has(company.id)
        ? `<span class="ats-spin">↻</span> Refreshing the board…`
        : `<span class="ats-spin">↻</span> Finding the job board from the careers URL…`
    }</div>`;
  }
  if (status && status.startsWith("none")) {
    // Not a dead end — a legitimate manual-tracking mode. Plenty of real
    // companies (custom career sites, gated ATSes, ones that block bots)
    // can't be pulled automatically, so frame it as "track by hand" and point
    // at the affordances that make that work (add jobs, the LinkedIn role
    // checks, the careers link) rather than leading with "fix the URL". The
    // technical reason still shows, and re-check stays for the just-missing-
    // URL case.
    return `
      <div class="ats-health">
        <span class="ats-tag none">manual</span>
        <span>${esc(noAtsReason(status))}</span>
        ${company.ats_last_checked ? `<span class="ats-checked" title="${esc(fmtStamp(company.ats_last_checked))}">last checked ${esc(fmtAgo(company.ats_last_checked))}</span>` : ""}
        <button type="button" class="btn btn-ghost" data-action="detect-board" data-id="${company.id}">Check again</button>
      </div>
      <div class="ats-status">Postings here can't be pulled automatically — track it by hand: add jobs above and use the LinkedIn role checks below${
        (() => {
          const link = companyLink(company.careers_url, "open the careers page ↗");
          return link ? `, or ${link}` : "";
        })()
      }. A corrected careers URL above re-checks on save.</div>`;
  }
  const manual = !company.ats_type || company.ats_type === "manual";
  if (manual) {
    if (status && status.startsWith("error:")) {
      // A detection that errored before any ats_type was written (offline
      // add, DNS hiccup) previously rendered the raw status with no way to
      // retry — the only visible ↻ was refresh-board, which 400s without a
      // connected adapter. Same two ways out as the 'none' state.
      return `
        <div class="ats-health">
          <span class="ats-tag error">failing</span>
          <span>board check failed — ${esc(failReason(status))}</span>
          ${company.ats_last_checked ? `<span class="ats-checked" title="${esc(fmtStamp(company.ats_last_checked))}">last checked ${esc(fmtAgo(company.ats_last_checked))}</span>` : ""}
          <button type="button" class="btn btn-ghost" data-action="detect-board" data-id="${company.id}">Check again</button>
        </div>
        <div class="ats-status error" title="${esc(status)}">A corrected careers URL above gets re-checked when it saves.</div>`;
    }
    return `<div class="ats-health">Checked manually${
      (() => {
        const link = companyLink(company.careers_url, "open careers page ↗");
        return link ? ` — ${link}` : "";
      })()
    } · use the LinkedIn role checks below.</div>`;
  }
  if (!status) {
    return `
      <div class="ats-health">${esc(company.ats_type)}${company.ats_slug ? ` · ${esc(company.ats_slug)}` : ""}</div>
      ${emptyState("Not fetched yet — runs with the next refresh.")}`;
  }
  const error = status.startsWith("error:");
  const stale = !error && isStaleList(company);
  const tag = error ? " error" : stale ? " stale" : "";
  // Humanize a failure ("timed out") instead of the raw URL/exception; keep the
  // raw text on hover. error/stale get the on-demand "Refresh board" button.
  const statusText = error ? failReason(status) : stale ? staleReason(company) : status;
  return `
    <div class="ats-health">
      <span class="ats-tag${tag}">${error ? "failing" : stale ? "stale" : "ok"}</span>
      <span>${esc(company.ats_type)}${company.ats_slug ? ` · ${esc(company.ats_slug)}` : ""}</span>
      <span class="ats-checked" title="${esc(fmtStamp(company.ats_last_checked))}">last checked ${esc(fmtAgo(company.ats_last_checked))}</span>
      ${error || stale ? `<button type="button" class="btn btn-ghost" data-action="refresh-board" data-id="${company.id}">Refresh board</button>` : ""}
    </div>
    <div class="ats-status${error ? " error" : ""}"${error ? ` title="${esc(status)}"` : ""}>${esc(statusText)}${
      // On a connected board, surface the careers link detection found/backfilled
      // — the result of the search, mirroring the wizard's filled-in careers field.
      error
        ? ""
        : (() => {
            const link = companyLink(company.careers_url, "open the job board ↗");
            return link ? ` · ${link}` : "";
          })()
    }</div>`;
}

/* Collapsible "Company settings" (QA pass 1): the field grid + ATS health,
   collapsed by default behind a chevron header carrying the same ats pill as
   the list row. The body is rendered-or-omitted, not height-animated — the
   view repaints wholesale, so a CSS height transition has nothing to run on. */
function settingsSection(company) {
  const open = expandedSettings.has(company.id);
  return `
    <div class="section">
      <button type="button" class="collapse-head" data-action="toggle-settings" aria-expanded="${open}">
        <span class="section-title">Company settings</span>
        ${atsHealthTag(company)}
        <svg class="collapse-chevron${open ? " open" : ""}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="9 18 15 12 9 6"/></svg>
      </button>
      ${
        open
          ? `<div class="collapse-body">
      <div class="control-row">
        <div class="field">
          <span class="field-label">Status</span>
          <select data-field="status" aria-label="Status">
            <option value="">—</option>
            ${selectOptions(STATUSES, company.status)}
          </select>
        </div>
        <div class="field">
          <span class="field-label">Priority</span>
          <select data-field="priority" aria-label="Priority">
            <option value="">—</option>
            ${selectOptions(["1", "2", "3", "4", "5"], String(company.priority ?? ""))}
          </select>
        </div>
        <div class="field">
          <span class="field-label">Values fit</span>
          <select data-field="values_fit" aria-label="Values fit">
            <option value="">—</option>
            ${selectOptions(VALUES_FIT, company.values_fit)}
          </select>
        </div>
        <div class="field">
          <span class="field-label">Location</span>
          <input data-field="location" aria-label="Location" value="${esc(company.location || "")}" />
        </div>
        <div class="field">
          <span class="field-label">Website</span>
          <input data-field="website" aria-label="Website" value="${esc(company.website || "")}" />
        </div>
        <div class="field">
          <span class="field-label">Careers URL</span>
          <input data-field="careers_url" aria-label="Careers URL" value="${esc(company.careers_url || "")}" />
        </div>
        <div class="field">
          <span class="field-label">LinkedIn IDs (comma-sep)</span>
          <input data-field="linkedin_company_ids" aria-label="LinkedIn IDs (comma-sep)" value="${esc((company.linkedin_company_ids || []).join(", "))}" />
          <a class="item-link" href="${escUrl(companyLookupUrl(company.name))}" target="_blank" rel="noopener">find company ID ↗</a>
        </div>
      </div>
      <div class="ats-subhead field-label">ATS health</div>
      ${atsBody(company)}
    </div>`
          : ""
      }
    </div>`;
}

function selectOptions(options, current, { labels } = {}) {
  return options
    .map((o, i) => `<option value="${esc(o)}"${o === current ? " selected" : ""}>${esc(labels ? labels[i] : o)}</option>`)
    .join("");
}

function titleCountLabel(n) {
  return `${n} title${n === 1 ? "" : "s"}`;
}

/* The LinkedIn role-check chips, derived purely from the company's titles
   (the "All roles" sweep + each per-title link). Kept separate so a blur can
   re-render just this block in place — see refreshLinkedinChips. */
function linkedinChipsHtml(company) {
  const titles = company.linkedin_title_searches || [];
  if (!titles.length) {
    return `<div class="titles-empty">No tracked titles yet — add some below.</div>`;
  }
  const combined = combinedSearchUrl(company);
  return `<div class="title-chip-row">
      ${titles
        .map(
          (t) => `<a class="title-chip" href="${escUrl(titleSearchUrl(company, t))}" target="_blank" rel="noopener">${esc(t)} <span class="title-chip-arrow">↗</span></a>`
        )
        .join("")}
      ${
        combined && hasMultipleTitles(company)
          ? `<a class="title-chip title-chip-all" href="${escUrl(combined)}" target="_blank" rel="noopener">All roles <span class="title-chip-arrow">↗</span></a>`
          : ""
      }
    </div>`;
}

function detailPane(company) {
  if (!company) {
    return `
      <div class="detail-empty">
        <div class="detail-empty-mark">${HQ_MARK}</div>
        <p>Select a company to see its details, LinkedIn checks, and contacts — or add a new one.</p>
      </div>`;
  }

  const contacts = state.contacts.filter((c) => c.company_id === company.id);
  const titles = company.linkedin_title_searches || [];

  return `
    <div class="detail-content" data-id="${company.id}">
      <div class="detail-head">
        <button class="detail-back" data-action="close-detail" title="Back to list">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="15 18 9 12 15 6"/></svg>
          <span>Back</span>
        </button>
        <div class="detail-head-id">
          <div class="detail-logo-wrap">
            ${companyLogoHtml({ name: company.name, logo: company.logo_url }, { size: "lg" })}
            <button type="button" class="ats-refresh logo-refresh" data-action="refresh-logo" data-id="${company.id}" title="Refresh the logo" aria-label="Refresh the logo">↻</button>
          </div>
          <div class="detail-head-id-main">
            <div class="detail-head-row">
              <div class="detail-eyebrow">${esc(company.location || "no location")}</div>
            </div>
            <h2 class="detail-title">
              <input data-field="name" value="${esc(company.name)}" aria-label="Company name" />
            </h2>
          </div>
        </div>
        <div class="detail-subhead">
          ${company.website ? companyLink(company.website, esc(company.website.replace(/^https?:\/\//, "")), esc(company.website)) : ""}
          ${company.careers_url ? companyLink(company.careers_url, "careers ↗") : ""}
          ${jobCountPill(company, { labeled: true })}
          ${company.active_job_count ? `<a class="item-link view-all-jobs" href="#/jobs?company=${company.id}">View all ${company.active_job_count} jobs →</a>` : ""}
          <span class="values-tag values-${esc(company.values_fit || "unknown")}">${esc(company.values_fit || "unknown")} fit</span>
          <button class="btn btn-ghost" data-action="add-job">+ Add job</button>
          <button class="btn btn-ghost btn-danger detail-delete" data-action="delete">Delete</button>
        </div>
      </div>

      ${topJobsSection(company)}

      ${settingsSection(company)}

      <div class="section">
        <div class="section-head">
          <h2 class="section-title">Notes</h2>
        </div>
        <textarea class="notes-area" data-field="notes" aria-label="Notes" placeholder="Notes…">${esc(company.notes || "")}</textarea>
      </div>

      <div class="section linkedin-section">
        <div class="section-head">
          <h2 class="section-title">LinkedIn role checks</h2>
          <span class="section-count">${titleCountLabel(titles.length)}</span>
        </div>
        <p class="linkedin-purpose">Each is a one-click LinkedIn search for people in that role at this
          company — a fast way to find someone to reach out to. Starts from the roles you set up; edit
          the list any time.</p>
        <div class="linkedin-chips">${linkedinChipsHtml(company)}</div>
        <textarea class="titles-edit" data-field="linkedin_title_searches" aria-label="LinkedIn role titles" placeholder="One title per line…">${esc(titles.join("\n"))}</textarea>
        <div class="titles-edit-hint">One title per line · saved on blur</div>
      </div>

      <div class="section">
        <div class="section-head">
          <h2 class="section-title">Contacts</h2>
          <span class="section-count">${contacts.length}</span>
          <button class="btn btn-ghost" data-action="add-contact">+ Add contact</button>
        </div>
        ${
          contacts.length
            ? `<div class="item-list">
                ${contacts
                  .map(
                    (c) => `
                    <div class="item-card">
                      <div class="item-head">
                        <span class="item-title">${esc(c.name)}</span>
                        <a class="item-link" href="#/contacts/${c.id}">open →</a>
                      </div>
                      <div class="item-meta">${esc(c.role || "")}</div>
                    </div>`
                  )
                  .join("")}
              </div>`
            : emptyState("No contacts linked to this company yet.")
        }
      </div>
    </div>`;
}

function template() {
  const rows = filtered();
  return `
    <div class="filters">
      <div class="filter-group">
        ${DD_FILTERS.map((dd) => ddTemplate(dd, state.filters)).join("")}
      </div>
      ${searchBoxHtml("Search companies…", state.filters.q)}
      <div class="actions-right">
        ${bulkRetryBtn()}
        <button class="btn btn-accent btn-collapse" data-action="add" aria-label="Add company"><span aria-hidden="true">+</span><span class="btn-label"> Add company</span></button>
      </div>
    </div>
    <div class="layout companies-layout">
      <div class="list-pane${state.mobileDetail ? " mobile-hide" : ""}">
        ${
          rows.length
            ? rows.map(listRow).join("")
            : emptyState(
                state.companies.length
                  ? "No companies match the current filters."
                  : "No companies yet — add one with the “+ Add company” button.",
                { pad: true }
              )
        }
      </div>
      <div class="detail-pane${state.mobileDetail ? " mobile-show" : ""}">
        ${detailPane(selected())}
      </div>
    </div>`;
}

function renderStats() {
  const active = state.companies.filter((c) => c.status && c.status !== "closed").length;
  setStats([
    { value: state.companies.length, label: pluralize(state.companies.length, "Company", "Companies") },
    { value: active, label: "Active" },
  ]);
}

/* No detail-pane fade — see the note above paint() in jobs.js. Short version:
   the rebuild destroys the outgoing content, so fading the replacement in
   leaves the pane blank for its opening frames, and that gap IS the flash.
   Instant swap has none. Do not re-add a bare fade-in. */
function paint(opts = {}) {
  const top = getListScroll(root);
  if (top !== null) state.listScroll = top;
  const dtop = getDetailScroll(root);
  if (dtop !== null) state.detailScroll = dtop;
  root.innerHTML = template();
  setListScroll(root, state.listScroll);
  // Selecting a different company opens its detail at the top; every other
  // repaint (expand/collapse settings, a settings-select autosave, top jobs
  // arriving) keeps the reader where they were in the detail pane.
  if (opts.detailToTop) state.detailScroll = 0;
  setDetailScroll(root, state.detailScroll);
  renderStats();
}

function repaintList() {
  const pane = root.querySelector(".list-pane");
  if (!pane) return;
  const rows = filtered();
  pane.innerHTML = rows.length
    ? rows.map(listRow).join("")
    : emptyState(
        state.companies.length
          ? "No companies match the current filters."
          : "No companies yet — add one with the “+ Add company” button.",
        { pad: true }
      );
}

async function reload({ keepSelection = true } = {}) {
  const keep = keepSelection ? state.selectedId : null;
  await load();
  state.selectedId = state.companies.some((c) => c.id === keep) ? keep : null;
  paint();
}

let saveTimer = null;

async function save(company, overrides, { quiet = false } = {}) {
  const wasChecking = company.ats_last_status === "checking";
  try {
    const updated = await api.updateCompany(company.id, payload(company, overrides));
    const index = state.companies.findIndex((c) => c.id === company.id);
    if (index !== -1) state.companies[index] = updated;
    if (!quiet) {
      // A wholesale repaint while ANOTHER field is mid-edit discards its
      // half-typed value and drops focus (tab Website → Careers page: the
      // website blur-save lands ~200ms into typing the next URL). The PUT
      // response is the fresh row, so sync state and repaint only the list
      // pane in that case — the watcher's settle paint catches the detail
      // pane up once editing stops.
      const editing =
        root.contains(document.activeElement) &&
        document.activeElement.matches?.("input, textarea, select");
      if (editing) repaintList();
      else paint();
    }
    // A changed website/careers URL makes the backend re-probe for an ATS (the
    // PUT response comes back pre-stamped 'checking'): watch it settle like an
    // add-time check, with an explicit outcome toast.
    if (!wasChecking && updated.ats_last_status === "checking") {
      settleHooks.set(company.id, onDetectSettled);
      startWatcher();
    }
  } catch (error) {
    if (quiet) return; // the focusout save retries and surfaces the error
    toast(error.detail || error.message, { error: true });
    paint(); // restore the last known-good values
  }
}

function unchanged(company, field, value) {
  return JSON.stringify(value) === JSON.stringify(company[field] ?? (Array.isArray(value) ? [] : null));
}

function fieldValue(field, element) {
  const raw = element.value.trim();
  switch (field) {
    case "priority":
      return raw ? Number(raw) : null;
    case "linkedin_company_ids":
      return raw ? raw.split(",").map((s) => s.trim()).filter(Boolean) : [];
    case "linkedin_title_searches":
      return element.value.split("\n").map((s) => s.trim()).filter(Boolean);
    case "name":
      return raw; // backend rejects empty — caught in save()
    default:
      return raw || null;
  }
}

const ATS_POLL_MS = 2500;
const ATS_POLL_MAX = 48; // ~2 min of NO change before giving up (each settle resets)
// Explicit refreshes (single ↻ or bulk retry) get a longer leash: the board
// queues on the backend's _refresh_lock, so behind a huge fetch (a big
// Oracle board's pagination + its scoring) it can sit silent for minutes before
// its own fetch even starts. Add-time onboarding keeps the short ceiling — a
// stuck detection shouldn't spin for six minutes.
const ATS_POLL_MAX_REFRESH = 144; // ~6 min
let atsPollTimer = null;
// Companies whose board is being re-fetched on demand (the "Refresh board"
// action). Add-time onboarding and a refresh share ats_last_status === "checking",
// so this Set is what lets the UI say "refreshing…" vs "checking…".
const refreshingIds = new Set();
// Companies in an in-flight bulk retry-failed run: drives the toolbar button's
// busy state, keeps mid-retry rows visible under the 'failing' filter, and
// feeds the drain toast. Survives navigation (render() prunes + resumes).
const bulkWatch = new Set();
// id -> callback registered by refreshCompanyBoard; fires once when that
// company TRULY settles (never on the give-up ceiling).
const settleHooks = new Map();

function stopWatcher() {
  clearInterval(atsPollTimer);
  atsPollTimer = null;
}

function anyChecking() {
  return state.companies.some((c) => c.ats_last_status === "checking");
}

/* Unified checking watcher (replaces the old single-company pollAtsStatus):
   while ANY company sits at 'checking' — add-time onboarding, a single ↻, or a
   bulk retry — poll the companies list and merge changed rows in place until
   everything settles. Mount-guarded: app.js renders every view into the same
   #view element, so a leaked timer's paint() would clobber whatever view is
   showing (the today.js/settings.js pollers carry the same guard, 209d7df;
   this one was the missing case — the "pulled back to Companies" bug).
   Navigating away only PARKS the watch: refreshingIds/bulkWatch survive and
   render() prunes + resumes them on re-entry. */
function startWatcher() {
  stopWatcher();
  let idleTicks = 0;
  atsPollTimer = setInterval(async () => {
    if (!root || !root.querySelector(".companies-layout")) {
      stopWatcher(); // Companies is no longer mounted — render() resumes the watch
      return;
    }
    let fresh;
    try {
      fresh = await api.listCompanies();
    } catch {
      return; // transient fetch error — try again next tick
    }
    // Per-id merge, swapping a row only when its ATS stamps moved: a refetch
    // that raced an in-flight quiet-autosave must never revert an edit on an
    // unrelated company. A settling row takes the full fresh payload (add-time
    // onboarding also writes ats_type/ats_slug/logo server-side).
    const changed = [];
    const byId = new Map(fresh.map((c) => [c.id, c]));
    state.companies = state.companies.map((old) => {
      const next = byId.get(old.id);
      if (
        !next ||
        (next.ats_last_status === old.ats_last_status &&
          next.ats_last_checked === old.ats_last_checked)
      )
        return old;
      changed.push({ old, next });
      return next;
    });
    for (const { old, next } of changed) {
      if (old.ats_last_status === "checking" && next.ats_last_status !== "checking")
        settleCompany(next);
    }
    idleTicks = changed.length ? 0 : idleTicks + 1;
    // Ceiling picked per tick, not at start: a ↻ can join a watch that began
    // as add-time onboarding (both share the one watcher).
    const maxIdle =
      refreshingIds.size || bulkWatch.size ? ATS_POLL_MAX_REFRESH : ATS_POLL_MAX;
    const drained = !anyChecking();
    if (drained || idleTicks >= maxIdle) {
      stopWatcher();
      if (!drained) {
        // Gave up (a run queued behind something long) — clear the busy
        // affordances, fire no hooks; the run continues server-side and a
        // later render re-syncs. Say so instead of stopping the spinner
        // silently (settles are guaranteed server-side, we just quit watching).
        const stuck = state.companies.filter(
          (c) =>
            c.ats_last_status === "checking" &&
            (refreshingIds.has(c.id) || bulkWatch.has(c.id)),
        );
        if (stuck.length === 1)
          toast(`${stuck[0].name} is still refreshing — reload to see the result.`);
        else if (stuck.length)
          toast(`${stuck.length} boards are still refreshing — reload to see the results.`);
        refreshingIds.clear();
        settleHooks.clear();
        bulkWatch.clear();
      } else {
        if (bulkWatch.size) endBulkWatch();
        // Every observed settle already fired (and deleted) its hook via
        // settleCompany; anything left belongs to a transition this watcher
        // never saw. Clearing here keeps it from firing on some later
        // coincidental checking→settled flip.
        settleHooks.clear();
      }
    }
    if (!changed.length && !drained && idleTicks < maxIdle) return; // nothing new
    const editing =
      root.contains(document.activeElement) &&
      document.activeElement.matches?.("input, textarea, select");
    const touchedSelected = changed.some(({ next }) => next.id === state.selectedId);
    if (touchedSelected && (!editing || drained)) paint();
    else repaintList();
  }, ATS_POLL_MS);
}

/* One company left 'checking': clear its spinner, invalidate its Top-jobs cache
   (its rows just changed), fire its registered settle hook. */
function settleCompany(company) {
  refreshingIds.delete(company.id);
  topJobsCache.delete(company.id);
  if (state.selectedId === company.id) loadTopJobs(company.id);
  const hook = settleHooks.get(company.id);
  if (hook) {
    settleHooks.delete(company.id);
    hook(company);
  }
}

/* The bulk retry finished (every watched board settled): one drain toast. */
function endBulkWatch() {
  const rows = [...bulkWatch]
    .map((id) => state.companies.find((c) => c.id === id))
    .filter(Boolean);
  bulkWatch.clear();
  if (!rows.length) return;
  const recovered = rows.filter((c) => (c.ats_last_status || "").startsWith("ok:")).length;
  const still = rows.length - recovered;
  toast(
    `${recovered} of ${rows.length} board${rows.length === 1 ? "" : "s"} recovered` +
      (still ? ` — ${still} still failing` : ""),
    { error: recovered === 0 },
  );
  if (recovered) chime();
  else buzz();
}

/* Re-fetch ONE company's board on demand (the ↻ / "Refresh board" buttons):
   202 + poll, reusing the onboarding 'checking' plumbing; toasts the outcome. */
const logoRefreshing = new Set();

async function refreshLogo(id, btn) {
  const company = state.companies.find((c) => c.id === id);
  if (!company || logoRefreshing.has(id)) return;
  logoRefreshing.add(id);
  if (btn) btn.disabled = true;
  const had = company.logo_url;
  try {
    // Best-effort by design (logos.py): a miss returns the company unchanged.
    const fresh = await api.refreshCompanyLogo(id);
    Object.assign(company, fresh);
    if (!fresh.logo_url) {
      toast(company.website || company.careers_url
        ? "No logo found — the monogram stays"
        : "No website on file — nothing to look a logo up by");
    } else if (fresh.logo_url === had) {
      toast("Logo refreshed");
    }
  } catch (error) {
    toast(error.detail || error.message, { error: true });
  } finally {
    logoRefreshing.delete(id);
    if (state.selectedId === id) paint();
    else repaintList();
  }
}

async function refreshCompanyBoard(id) {
  const company = state.companies.find((c) => c.id === id);
  if (!company) return;
  if (company.ats_last_status === "checking" || refreshingIds.has(id)) {
    toast("Already refreshing…");
    return;
  }
  const prevCount = company.active_job_count ?? 0;
  let r;
  try {
    r = await api.refreshCompanyBoard(id);
  } catch (error) {
    toast(error.detail || error.message, { error: true });
    return;
  }
  if (r.running) {
    // A FULL refresh is mid-run and will re-pull every board, this one included —
    // no need to queue a redundant single-board fetch; just reassure.
    toast(`A full refresh is running — ${company.name} will refresh with it.`);
    return;
  }
  if (r.checking) {
    toast("Already refreshing…");
    return;
  }
  refreshingIds.add(id);
  company.ats_last_status = "checking"; // optimistic local stamp drives "refreshing…"
  settleHooks.set(id, (c) => onRefreshSettled(c, prevCount));
  if (state.selectedId === id) paint();
  else repaintList();
  startWatcher();
}

/* Bulk retry of every failing board (the toolbar button): one scoped backend
   run (POST /api/refresh {scope:"failed"}); the route pre-stamps the targets
   'checking' and returns their ids, the watcher shows live per-row settling,
   endBulkWatch toasts the drain. */
async function bulkRefreshFailing() {
  if (bulkWatch.size) return; // already retrying
  let r;
  try {
    r = await api.triggerRefresh({ scope: "failed" });
  } catch (error) {
    toast(error.detail || error.message, { error: true });
    return;
  }
  if (r.running) {
    toast("A refresh is already running…");
    return;
  }
  if (r.none) {
    toast("No failing boards to retry.");
    return;
  }
  // Seed from the ROUTE's ids (connectable failing only — the client-side
  // failing count also includes manual companies nothing can re-fetch).
  const now = new Date().toISOString();
  for (const id of r.ids) {
    bulkWatch.add(id);
    refreshingIds.add(id);
    const company = state.companies.find((c) => c.id === id);
    if (company) {
      company.ats_last_status = "checking"; // mirror the route's pre-stamp
      company.ats_last_checked = now;
    }
  }
  toast(`Retrying ${r.ids.length} failing board${r.ids.length === 1 ? "" : "s"}…`);
  paint();
  startWatcher();
}

/* The explicit success/failure + results toast after a single-board refresh. */
function onRefreshSettled(company, prevCount) {
  const status = company.ats_last_status || "";
  if (status.startsWith("ok:")) {
    const n = company.active_job_count ?? 0;
    const gained = n - prevCount;
    const extra = gained > 0 ? ` (${gained} new)` : "";
    toast(`${company.name} refreshed — ${n} active job${n === 1 ? "" : "s"}${extra}`);
    chime(); // keyless — each board settle is its own event
  } else if (status.startsWith("error:")) {
    toast(`${company.name} couldn't refresh — ${failReason(status)}`, { error: true });
    buzz();
  }
}

/* Humanize a 'none: …' detection outcome (the counterpart of failReason for
   errors): "none: lever detected but no adapter supports it" → the part after
   the prefix, so the UI states the actual reason instead of a generic "no ATS". */
function noAtsReason(status) {
  const detail = String(status || "").replace(/^none:?\s*/, "").trim();
  return detail || "no ATS detected";
}

/* The explicit outcome toast after a URL-edit or "Check again" detection —
   the settle counterpart of onRefreshSettled. */
function onDetectSettled(company) {
  const status = company.ats_last_status || "";
  if (status.startsWith("ok:")) {
    const n = company.active_job_count ?? 0;
    toast(`Found ${company.name}'s job board — ${n} active job${n === 1 ? "" : "s"}`);
    chime();
  } else if (status.startsWith("none")) {
    toast(`${company.name}: ${noAtsReason(status)}`, { error: true });
    buzz();
  } else if (status.startsWith("error:")) {
    toast(`${company.name}: board check failed — ${failReason(status)}`, { error: true });
    buzz();
  }
}

/* Re-run ATS detection on demand (the "Check again" button on a none/failed
   board). Mirrors refreshCompanyBoard's optimistic-stamp + watcher pattern. */
async function detectBoard(id) {
  const company = state.companies.find((c) => c.id === id);
  if (!company) return;
  if (company.ats_last_status === "checking") {
    toast("Already checking…");
    return;
  }
  let r;
  try {
    r = await api.detectCompanyBoard(id);
  } catch (error) {
    toast(error.detail || error.message, { error: true });
    return;
  }
  if (r.checking) {
    toast("Already checking…");
    return;
  }
  company.ats_last_status = "checking"; // mirror the route's pre-stamp
  company.ats_last_checked = new Date().toISOString();
  settleHooks.set(id, onDetectSettled);
  if (state.selectedId === id) paint();
  else repaintList();
  startWatcher();
}

function addModal() {
  openModal({
    title: "Add company",
    body: `
      <p class="form-req-note"><span class="req-mark" aria-hidden="true">*</span> required</p>
      <div class="form-field"><label>Name <span class="req-mark" aria-hidden="true">*</span></label><input name="name" required /></div>
      <div class="form-field"><label>Website</label><input name="website" type="url" placeholder="https://…" /></div>
      <div class="form-field"><label>Careers page URL</label><input name="careers_url" type="url" placeholder="https://… (the jobs page — the most reliable way to find the board)" /></div>
      <div class="form-field"><label>Location</label><input name="location" /></div>
      <div class="form-field"><label>Priority</label><select name="priority"><option value="">—</option>${selectOptions(["1", "2", "3", "4", "5"], null)}</select></div>
      <div class="form-field"><label>Status</label><select name="status">${selectOptions(STATUSES, "prospect")}</select></div>
      <div class="form-field"><label>Values fit</label><select name="values_fit"><option value="">—</option>${selectOptions(VALUES_FIT, null)}</select></div>`,
    footer: `
      <button type="button" class="btn" data-action="modal-close">Cancel</button>
      <button type="submit" class="btn btn-accent">Add company</button>`,
    onSubmit: async (form) => {
      const data = Object.fromEntries(new FormData(form));
      try {
        const created = await api.createCompany({
          name: data.name,
          status: data.status || null,
          priority: data.priority ? Number(data.priority) : null,
          values_fit: data.values_fit || null,
          location: data.location || null,
          website: data.website || null,
          careers_url: data.careers_url || null,
        });
        closeModal();
        state.selectedId = created.id;
        expandedSettings.add(created.id); // show the ATS check live, not collapsed
        await reload();
        // A just-added company must be VISIBLE: if the current filters would
        // hide its row (e.g. a leftover status/search narrowing), clear
        // them — an invisible fresh add reads as "didn't take" and invites a
        // duplicate.
        if (!filtered().some((c) => c.id === created.id)) {
          state.filters = { status: new Set(), ats: new Set(), listings: "", q: "" };
          paint(); // reload() painted BEFORE the reset — repaint or the row stays hidden
        }
        setDetailHash("companies", created.id);
        toast(`Added ${created.name}`);
        // The required onboarding step may have just flipped, and setDetailHash's
        // pushState never fires hashchange/render() — nudge the Setup pill here.
        onboardingTracker.refresh();
        if (created.ats_last_status === "checking") {
          settleHooks.set(created.id, onDetectSettled); // outcome toast on settle
          startWatcher();
        }
      } catch (error) {
        toast(error.detail || error.message, { error: true });
      }
    },
  });
}

/* Add-job dedupe (409) carries the existing job's id + status (api error.info),
   so a blocked add isn't a dead end: a dismissed/closed duplicate (hidden from
   the default list, uncounted) can be reactivated in place; an active/applied
   one is just reported. The add modal is already closed by the caller. */
async function handleAlreadyTracked(info, company) {
  const { job_id, status, title } = info;
  if (status === "dismissed" || status === "closed") {
    const stateLabel = status === "dismissed" ? "dismissed" : "no longer listed";
    const ok = await confirmModal({
      title: "Already tracked",
      message: `“${title}” is already tracked here but currently ${stateLabel}. Reactivate it?`,
      confirmLabel: "Reactivate",
      confirmClass: "btn-accent",
    });
    if (!ok) return;
    try {
      await api.setJobStatus(job_id, "active");
      topJobsCache.delete(company.id); // refetch so it lands back in Top jobs
      await reload(); // active_job_count + list refresh in place
      loadTopJobs(company.id);
      toast(`Reactivated “${title}”`);
    } catch (error) {
      toast(error.detail || error.message, { error: true });
    }
  } else {
    // active / applied — already visible / in the pipeline, nothing to reactivate
    const where = status === "applied" ? "applications" : "jobs list";
    toast(`Already tracked — “${title}” is already in your ${where}.`);
  }
}

/* Manual job entry for a company with no connectable ATS (the user found it via
   the LinkedIn role links / careers page). POST /api/jobs stores it
   source='manual', scores it immediately, and exempts it from refresh decay. */
function addJobModal(company) {
  const overlay = openModal({
    title: `Add a job at ${company.name}`,
    body: `
      <p class="form-req-note"><span class="req-mark" aria-hidden="true">*</span> required</p>
      <div class="form-field">
        <label>Posting URL</label>
        <div class="url-fetch-row">
          <input name="url" type="url" placeholder="https://… (paste, then Fetch)" />
          <button type="button" class="btn" data-action="fetch-url">Fetch</button>
        </div>
        <div class="form-hint">Paste a posting URL and Fetch to auto-fill the fields below — best-effort, review before saving.</div>
      </div>
      <div class="form-field"><label>Title <span class="req-mark" aria-hidden="true">*</span></label><input name="title" required placeholder="e.g. Senior Product Designer" /></div>
      <div class="form-row">
        <div class="form-field"><label>Location</label><input name="location" placeholder="City, ST or Remote" /></div>
        <div class="form-field"><label>Remote</label><select name="remote_type">${selectOptions(["remote", "hybrid", "onsite", "unknown"], "remote")}</select></div>
      </div>
      <div class="form-row">
        <div class="form-field"><label>Salary min</label><input name="salary_min" type="number" min="0" inputmode="numeric" placeholder="e.g. 150000" /></div>
        <div class="form-field"><label>Salary max</label><input name="salary_max" type="number" min="0" inputmode="numeric" placeholder="e.g. 190000" /></div>
      </div>
      <div class="form-field"><label>Job description</label><textarea name="description_text" rows="6" placeholder="Paste the JD here — it's what the fit score reads."></textarea></div>`,
    footer: `
      <button type="button" class="btn" data-action="modal-close">Cancel</button>
      <button type="submit" class="btn btn-accent">Add job</button>`,
    onSubmit: async (form) => {
      const data = Object.fromEntries(new FormData(form));
      // The create scores the job inline (a model call, a few seconds) — show
      // that work is happening instead of an unexplained delay.
      const btn = form.querySelector('button[type="submit"]');
      if (btn) {
        btn.disabled = true;
        btn.textContent = "Adding & scoring…";
      }
      try {
        const created = await api.createJob({
          company_id: company.id,
          title: data.title,
          url: data.url || null,
          location: data.location || null,
          remote_type: data.remote_type || "unknown",
          salary_min: data.salary_min ? Number(data.salary_min) : null,
          salary_max: data.salary_max ? Number(data.salary_max) : null,
          description_text: data.description_text || null,
        });
        closeModal();
        topJobsCache.delete(company.id); // refetch so it lands in Top jobs
        await reload(); // active_job_count + list refresh in place
        loadTopJobs(company.id);
        toast(`Added “${created.title}”${created.fit_score != null ? ` — fit ${created.fit_score}` : ""}`);
      } catch (error) {
        // Already-tracked (409): the dedupe carries the existing job, so offer to
        // reactivate a dismissed/closed duplicate instead of dead-ending.
        if (error.status === 409 && error.info?.job_id) {
          closeModal();
          await handleAlreadyTracked(error.info, company);
          return;
        }
        toast(error.detail || error.message, { error: true });
        if (btn) {
          btn.disabled = false;
          btn.textContent = "Add job";
        }
      }
    },
  });

  // Wire the "Fetch" button: pull the posting and prefill the fields. The modal
  // mounts on document.body (outside the view root), so attach the handler here.
  const fetchBtn = overlay.querySelector('[data-action="fetch-url"]');
  const urlInput = overlay.querySelector('[name="url"]');
  const hint = overlay.querySelector(".form-hint");
  const showHint = (text, isError) => {
    if (!hint) return;
    hint.textContent = text;
    hint.classList.toggle("form-hint-error", !!isError);
  };
  const runFetch = async () => {
    const url = urlInput.value.trim();
    if (!url) {
      toast("Paste a posting URL first", { error: true });
      urlInput.focus();
      return;
    }
    fetchBtn.disabled = true;
    fetchBtn.textContent = "Fetching…";
    try {
      const parsed = await api.parseJobUrl(url);
      const set = (name, val) => {
        if (val === null || val === undefined || val === "") return;
        const el = overlay.querySelector(`[name="${name}"]`);
        if (el) el.value = val;
      };
      set("title", parsed.title);
      set("location", parsed.location);
      set("remote_type", parsed.remote_type);
      set("salary_min", parsed.salary_min);
      set("salary_max", parsed.salary_max);
      set("description_text", parsed.description_text);
      if (parsed.title || parsed.description_text) {
        toast("Pulled the posting — review and save");
        showHint("Pulled what we could — review the fields before saving.", false);
      } else {
        const reason = parsed.detail || "Couldn’t read that page — fill the fields in below.";
        toast(reason, { error: true });
        showHint(reason, true);
      }
    } catch (error) {
      const reason = error.detail || error.message;
      toast(reason, { error: true });
      showHint(reason, true);
    } finally {
      fetchBtn.disabled = false;
      fetchBtn.textContent = "Fetch";
    }
  };
  fetchBtn.addEventListener("click", runFetch);
  urlInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      runFetch();
    }
  });
}

async function deleteSelected() {
  const company = selected();
  if (!company) return;
  let jobs = 0;
  try {
    jobs = (await api.listJobs()).filter((j) => j.company_id === company.id).length;
  } catch {
    /* count is informational; the confirm still spells out the rest */
  }
  const linked = state.contacts.filter((c) => c.company_id === company.id).length;
  const parts = ["its activity notes and reminders"];
  if (jobs) parts.unshift(`its ${jobs} tracked job${jobs === 1 ? "" : "s"}`);
  const consequences = `${parts.join(" and ")} will be deleted${
    linked ? `; ${linked} linked contact${linked === 1 ? "" : "s"} will be unassigned` : ""
  }.`;
  const ok = await confirmModal({
    title: `Delete ${company.name}?`,
    message: `This permanently removes the company — ${consequences}`,
  });
  if (!ok) return;
  try {
    await api.deleteCompany(company.id);
    state.selectedId = null;
    state.mobileDetail = false;
    setDetailHash("companies", null); // Back must not return to the deleted id
    await reload();
    toast(`Deleted ${company.name}`);
  } catch (error) {
    toast(error.detail || error.message, { error: true });
  }
}

bindOutsideClose(() => root);

function onClick(event) {
  const target = event.target.closest("[data-action]");
  if (!target || !root.contains(target)) return;
  switch (target.dataset.action) {
    case "select":
      state.selectedId = Number(target.dataset.id);
      state.mobileDetail = true;
      paint({ detailToTop: true });
      loadTopJobs(state.selectedId);
      setDetailHash("companies", state.selectedId);
      break;
    case "open-job":
      location.hash = "#/jobs/" + target.dataset.id;
      break;
    case "close-detail":
      // Our own history entry → back() pops it (popstate → hashchange → render);
      // cold deep-link entry → rewrite the hash in place and close locally.
      if (history.state?.hqDetail) {
        history.back();
      } else {
        state.mobileDetail = false;
        setDetailHash("companies", null);
        paint();
      }
      break;
    case "add":
      addModal();
      break;
    case "add-job": {
      const company = selected();
      if (company) addJobModal(company);
      break;
    }
    case "add-contact": {
      const company = selected();
      if (!company) break;
      openContactModal({
        companies: state.companies,
        companyId: company.id,
        onCreated: () => reload(), // refetches contacts → detail section refreshes
      });
      break;
    }
    case "delete":
      deleteSelected();
      break;
    case "refresh-board":
      refreshCompanyBoard(Number(target.dataset.id));
      break;
    case "detect-board":
      detectBoard(Number(target.dataset.id));
      break;
    case "refresh-logo":
      refreshLogo(Number(target.dataset.id), target);
      break;
    case "refresh-failing":
      bulkRefreshFailing();
      break;
    case "toggle-settings": {
      const company = selected();
      if (!company) break;
      const opening = !expandedSettings.has(company.id);
      if (opening) expandedSettings.add(company.id);
      else expandedSettings.delete(company.id);
      paint();
      // Reveal animation only on the user's toggle — the template must not
      // carry it, or every unrelated repaint of an expanded section (autosave,
      // ATS poll) would re-animate (P5; see .collapse-enter in app.css).
      if (opening) root.querySelector(".collapse-body")?.classList.add("collapse-enter");
      break;
    }
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
  }
}

function onChange(event) {
  const element = event.target;
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
  const company = selected();
  if (!field || !company || element.tagName !== "SELECT") return;
  save(company, { [field]: fieldValue(field, element) });
}

/* Swap the role-check chips + count in place from the textarea's current
   value. The chips are a pure function of the titles, so no server round-trip
   is needed — this is why blur shows them instantly even when the 700ms quiet
   autosave already synced state (which makes the focusout save a no-op). The
   in-place swap leaves the textarea untouched, so focus moving to another field
   is never stolen. */
function refreshLinkedinChips(company, titles) {
  const section = root.querySelector(".linkedin-section");
  if (!section) return;
  const view = { ...company, linkedin_title_searches: titles };
  section.querySelector(".linkedin-chips").innerHTML = linkedinChipsHtml(view);
  section.querySelector(".section-count").textContent = titleCountLabel(titles.length);
}

function onFocusOut(event) {
  const element = event.target;
  const field = element.dataset.field;
  const company = selected();
  if (!field || !company || element.tagName === "SELECT") return;
  clearTimeout(saveTimer);
  const value = fieldValue(field, element);
  if (field === "name" && !value) {
    toast("Name can't be empty", { error: true });
    paint();
    return;
  }
  if (field === "linkedin_title_searches") {
    refreshLinkedinChips(company, value); // appear on blur, not on next refresh
  }
  if (unchanged(company, field, value)) return;
  save(company, { [field]: value });
}

function onInput(event) {
  const element = event.target;
  if (element.dataset.action === "search") {
    state.filters.q = element.value;
    root.querySelector(".search-clear")?.classList.toggle("hide", !state.filters.q);
    repaintList();
    return;
  }
  const field = element.dataset.field;
  if (!field || element.tagName === "SELECT") return;
  // URL fields commit on blur/change only: the quiet mid-typing save would PUT
  // a half-typed URL every pause — and a URL change now fires the backend's
  // ATS re-probe, so partial saves would probe garbage and strand the real
  // URL behind the 'checking' guard. (They give up the iOS never-blurs net;
  // the app is localhost-first, so blur is reliable.)
  if (field === "careers_url" || field === "website") return;
  const companyId = selected()?.id;
  if (!companyId) return;
  clearTimeout(saveTimer);
  // iOS Safari often never blurs an input (tapping outside isn't a blur), so a
  // focusout-only save loses edits; autosave while typing as the safety net.
  saveTimer = setTimeout(() => {
    const company = state.companies.find((c) => c.id === companyId);
    if (!company) return;
    const value = fieldValue(field, element);
    // Role chips track the live edit, not just a blur: the quiet autosave repaints
    // nothing and focusout can be unreliable, so without this the LinkedIn search
    // buttons wouldn't appear until a full reload. Focus-safe (textarea untouched).
    if (field === "linkedin_title_searches") refreshLinkedinChips(company, value);
    if (field === "name" && !value) return; // focusout owns the empty-name error
    if (unchanged(company, field, value)) return;
    save(company, { [field]: value }, { quiet: true });
  }, 700);
}

export async function render(container, preselectId = null, params = {}) {
  root = container;
  stopWatcher(); // restarted below once the fresh list is in
  renderLoading(container);
  container.onclick = onClick;
  container.onchange = onChange;
  container.oninput = onInput;
  setFocusOut(container, onFocusOut);
  setRowKeys(container, onClick);
  // Deep-link from a Today banner (#/companies?ats=failing|stale|none) pre-applies
  // the ATS filter so the user lands on exactly the companies that need a look.
  // These are per-navigation DIAGNOSTIC drill-downs, so a plain visit resets
  // them: module state persists across mounts, and a banner filter left armed
  // silently hid rows on later visits — including a company added seconds ago
  // (still 'checking', invisible under ats=none), which read as "my add didn't
  // take" and invited a duplicate. Browsing filters (status/search) keep
  // their stickiness.
  state.filters.ats = params.ats ? new Set([params.ats]) : new Set();
  state.filters.listings = params.listings || "";
  try {
    await load();
  } catch (error) {
    // params rides along or a Retry after a failed deep-linked load
    // (#/companies?ats=failing) silently drops the pre-applied banner filter.
    renderLoadError(container, error, () => render(container, preselectId, params));
    setStats([]);
    return;
  }
  if (preselectId && state.companies.some((c) => c.id === preselectId)) {
    state.selectedId = preselectId;
    state.mobileDetail = true;
  } else if (!preselectId) {
    // back/forward to the bare list: keep the desktop pane's selection, but
    // the phone must land on the list
    state.mobileDetail = false;
  }
  // Prune parked watch state to companies still 'checking' — a board that
  // settled while we were unmounted must not fire a stale hook or toast on
  // some later coincidental transition — then resume the watch. settleHooks
  // is pruned by its OWN keys: the URL-edit save, "Check again", and the add
  // modal all register hooks without adding to refreshingIds, so pruning only
  // through refreshingIds leaked theirs — parked forever, firing a bogus
  // outcome toast on the next coincidental checking→settled transition (e.g.
  // a scheduled refresh days later).
  const stillChecking = new Set(
    state.companies.filter((c) => c.ats_last_status === "checking").map((c) => c.id),
  );
  for (const id of [...refreshingIds]) if (!stillChecking.has(id)) refreshingIds.delete(id);
  for (const id of [...settleHooks.keys()]) if (!stillChecking.has(id)) settleHooks.delete(id);
  for (const id of [...bulkWatch]) if (!stillChecking.has(id)) bulkWatch.delete(id);
  paint();
  // Route-driven selection only: an in-list click never yanks the pane.
  if (preselectId && state.selectedId === preselectId) revealSelected(root);
  if (state.selectedId) loadTopJobs(state.selectedId);
  if (stillChecking.size) startWatcher();
}
