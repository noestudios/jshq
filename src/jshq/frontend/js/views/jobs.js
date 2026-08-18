/* Jobs view: ingested ATS postings. Filterable list pane + read-only detail
   pane (JD text, scoring placeholder, dismiss/reactivate). Jobs are written
   by the refresh pipeline; the only user-editable thing here is status. */

import { api } from "../api.js";
import { openComposeModal } from "../lib/composeModal.js";
import {
  activityTimelineHtml,
  openActivityModal,
  openReminderModal,
} from "../lib/reminderModal.js";
import {
  bindOutsideClose,
  closeDropdowns,
  ddTemplate,
  optionsHtml,
  summaryPillHtml,
  updateSummaryPill,
  updateToggle,
} from "../lib/filterDd.js";
import {
  closeModal,
  emptyState,
  esc,
  escUrl,
  fitChip,
  fmtStamp,
  getDetailScroll,
  getListScroll,
  HQ_MARK,
  isDelisted,
  isHardFailFit,
  isResolvedApplication,
  mdToHtml,
  openModal,
  POSITIVE_FIT,
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
import { helpHintHtml } from "../lib/helpHint.js";
import { companyLogoHtml } from "../lib/logo.js";
import { levelBands, levelLabel, quadrantLabel, tensionLabel } from "../lib/vocab.js";

const REMOTE_TYPES = ["remote", "hybrid", "onsite", "unknown"];

/* Multiselect dropdown filters. Empty selection = filter off = show all. */
const DD_FILTERS = [
  {
    key: "status",
    label: "Status",
    type: "multi",
    field: "status",
    options: [
      { value: "active", label: "active" },
      { value: "applied", label: "applied" },
      { value: "dismissed", label: "dismissed" },
      { value: "closed", label: "no longer listed" },
    ],
  },
  {
    key: "level",
    label: "Level",
    type: "multi",
    field: "level_band",
    // Getters, not values: this config is built when the module loads, which is
    // before app.js has awaited loadVocab(). filterDd reads dd.options at paint
    // time, so a getter always hands it the live vocabulary — a captured array
    // would freeze the offline fallback into the UI for the whole session.
    get options() {
      return levelBands();
    },
  },
  {
    key: "remote",
    label: "Remote",
    type: "multi",
    field: "remote_type",
    options: REMOTE_TYPES.map((r) => ({ value: r, label: r })),
  },
];

/* Bands are derived from the criteria doc's comp floor/target at load (see
   applySalaryBands) so they bracket the range actually being searched — a
   fixed ladder is either all-or-nothing for one seeker and useless for the
   next. These literals are the fallback when the criteria call fails; the
   array is mutated in place because RADIO_FILTERS captures the reference. */
const SALARY_FLOORS = [
  { value: "", label: "Any salary" },
  { value: "120000", label: "$120k+" },
  { value: "150000", label: "$150k+" },
  { value: "180000", label: "$180k+" },
  { value: "200000", label: "$200k+" },
];

/* floor×0.8 (reach below), floor, target, target×1.25 (stretch) — rounded to
   $5k, deduped. Floor 180k/target 200k → 145/180/200/250; 100k/125k →
   80/100/125/155. */
function applySalaryBands(params) {
  const floor = Number(params?.comp_floor) || 0;
  const target = Number(params?.comp_target) || floor;
  if (!floor) return; // no usable params — keep the fallback ladder
  const to5k = (n) => Math.round(n / 5000) * 5000;
  const steps = [...new Set([to5k(floor * 0.8), to5k(floor), to5k(target), to5k(target * 1.25)])]
    .filter((n) => n > 0)
    .sort((a, b) => a - b);
  SALARY_FLOORS.splice(
    1,
    SALARY_FLOORS.length - 1,
    ...steps.map((n) => ({ value: String(n), label: `$${Math.round(n / 1000)}k+` }))
  );
  // A band that just disappeared would leave the pill filtering on a value no
  // option offers, with no way to clear it from the UI.
  if (!SALARY_FLOORS.some((o) => o.value === state.filters.salaryFloor)) {
    state.filters.salaryFloor = "";
  }
}

const FIT_FLOORS = [
  { value: "", label: "Any fit" },
  { value: "40", label: "Fit 40+" },
  { value: "60", label: "Fit 60+" },
  { value: "80", label: "Fit 80+" },
];

/* Single-select pills: the "" option is the off state; the pill label swaps
   to the chosen option while active. The comp-unknown switch lives inside the
   salary panel (it interacts with salary semantics — unknown comp can't pass
   a floor) and marks the pill active when toggled off its default. */
const RADIO_FILTERS = [
  {
    key: "salaryFloor",
    label: "Salary",
    type: "radio",
    options: SALARY_FLOORS,
    switches: [{ key: "unknownComp", label: "comp unknown ok", default: true }],
  },
  {
    key: "fitFloor",
    label: "Fit",
    type: "radio",
    options: FIT_FLOORS,
    switches: [{ key: "hideZeroFit", label: "hide 0-fit jobs", default: true }],
    footer: { label: "How scoring works", action: "fit-help" },
  },
];

/* Sort order (default Fit-desc). The "" option is the Fit default; the pill
   label swaps to the chosen option while active. The elevated-pin + Tier-1-fail
   partition live in filtered(); this only chooses the comparator. Placed first
   in the bar so the active sort is the leftmost, most-visible control. */
const SORT_FILTER = {
  key: "sortBy",
  label: "Sort",
  type: "radio",
  options: [
    { value: "", label: "Fit" },
    { value: "newest", label: "Newest" },
    { value: "salary", label: "Salary" },
  ],
};

const ALL_DD = [SORT_FILTER, ...DD_FILTERS, ...RADIO_FILTERS];
const ddByKey = (key) => ALL_DD.find((d) => d.key === key);

const state = {
  jobs: [],
  lastRefresh: null,
  selectedId: null,
  detailCache: new Map(),
  activityCache: new Map(),
  filters: {
    status: new Set(),
    level: new Set(),
    remote: new Set(),
    salaryFloor: "",
    unknownComp: true,
    fitFloor: "",
    // Default ON: a Tier-1 hard-fail (fit_score===0 — location/comp/etc.) isn't a
    // real candidate, so it's hidden by default; the switch reveals it on demand.
    // NULL (not-yet-scored) is exempt (strict === 0 at the filter) and stays visible.
    hideZeroFit: true,
    sortBy: "",
    // Company scope (set from #/jobs?company=<id> on each mount; hash is the
    // source of truth, so leaving the view clears it). null = unscoped.
    company: null,
    // Flag scope (from clicking a flag-rollup row): show only the jobs the strip
    // counted for that flag. UI-only (no hash), one flag at a time. null = off.
    flag: null,
    q: "",
  },
  mobileDetail: false,
  listScroll: 0,
  detailScroll: 0,
  // Flag-rollup strip: collapsed by default on every load (the headline is the
  // payload — the histogram is opt-in). UI-only, not persisted.
  rollupExpanded: false,
};

let root = null;

async function load() {
  const [jobs, refresh, criteria] = await Promise.all([
    api.listJobs(),
    api.refreshStatus(),
    // Only the salary bands depend on this; a broken criteria doc must not
    // take the whole board down with it.
    api.getCriteria().catch(() => null),
  ]);
  jobs.forEach(parseFlags);
  state.jobs = jobs;
  state.lastRefresh = refresh.last_refresh;
  applySalaryBands(criteria?.tier1_params);
}

/* near_miss_flags arrives as a JSON string column; normalize to an array. */
export function parseFlags(job) {
  if (typeof job.near_miss_flags === "string") {
    try {
      job.near_miss_flags = JSON.parse(job.near_miss_flags);
    } catch {
      job.near_miss_flags = [];
    }
  }
  job.near_miss_flags = job.near_miss_flags || [];
  return job;
}

/* A near-miss is an active job flagged for one soft gap — UNLESS its score is
   already positive (POSITIVE_FIT+), in which case the strong score overrides the
   flag. One definition drives the "maybe" tag, the Today "Maybe" band, and the
   Elevate gate, so they can't disagree (e.g. a 72 with a flag is not a maybe). */
export function isNearMiss(job) {
  return (
    job.status === "active" &&
    job.near_miss_flags.length > 0 &&
    job.fit_score !== 0 && // a Tier-1 hard-fail is excluded, not a "maybe"
    (job.fit_score == null || job.fit_score < POSITIVE_FIT)
  );
}

/* Plain-language pill text for the near-miss flags (PEER-02): a lay user
   couldn't tell whether a raw "wrong function" chip blamed the app or the job.
   The internal snake_case token stays the source of truth everywhere — scoring,
   storage, and the red-fail styling key on it — this map is display-only, and
   the text is kept pill-short. An unmapped flag (e.g. a free-form one the model
   emits) falls back to the old snake→space so nothing ever renders blank. */
const FLAG_GLOSSES = {
  wrong_function: "role mismatch",
  function_unclear: "role unclear",
  scope_gap: "seniority gap",
  below_band: "below your level",
  comp_below_target: "below target pay",
  comp_unknown: "no salary listed",
  location_unknown: "location unclear",
  thin_posting: "light on detail",
};

/* Internal provenance, not a concern about the job — never shown as a pill.
   sibling_override marks a score cross-corrected against sibling postings at the
   same company; its explanation already rides the scoring notes. */
const HIDDEN_FLAGS = new Set(["sibling_override"]);

/* Canonical display label for a near-miss flag — the ONE transform shared by the
   per-role Scoring panel and the flag-rollup strip, so the aggregate and the
   detail can never show a flag under different names. */
export function flagLabel(flag) {
  return FLAG_GLOSSES[flag] || flag.replaceAll("_", " ");
}

function selected() {
  return state.jobs.find((j) => j.id === state.selectedId) || null;
}

function isNew(job) {
  return state.lastRefresh && job.first_seen === state.lastRefresh;
}

/* Lowercase and flatten punctuation to spaces so "Sr. Director, UI/UX"
   matches the terms "sr director" or "ui ux". */
function normSearch(text) {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, " ")
    .trim();
}

/* Search groups: comma separates alternatives, "+" joins requirements:
   "director, ux" = either; "director + remote" = both. */
function searchGroups(q) {
  return q
    .split(",")
    .map((g) => g.split("+").map(normSearch).filter(Boolean))
    .filter((g) => g.length);
}

/* The per-job filter predicate, pulled out so the hidden-by-filters count (#58)
   can re-run it with hideZeroFit forced off without duplicating the rules. */
function matchesFilters(j, f, groups) {
  // Company scope (from #/jobs?company=<id>): show only that company's jobs.
  if (f.company && j.company_id !== f.company) return false;
  // Flag scope (from a flag-rollup row): match exactly the jobs the strip
  // counted for that flag — isNearMiss carriers — so the strip count and the
  // visible row count always agree (and every strip flag yields ≥1 row).
  if (f.flag && !(isNearMiss(j) && j.near_miss_flags.includes(f.flag))) return false;
  // A resolved application (rejected/withdrawn) follows the DISMISSED rule
  // (owner review, 2026-08-13): you're done with it either way, so it filters exactly
  // as a dismissed job does — hidden by default, surfaced by the same Status →
  // dismissed pick rather than disappearing with no way back. jobs.status is
  // never rewritten (advancing an application must not write back to the job),
  // so this mapping is view-level only. Dismissal itself still outranks it: a
  // job the user dismissed stays dismissed whatever became of its application,
  // which is why showsResolved() excludes dismissed rows.
  const status = showsResolved(j) ? "dismissed" : j.status;
  for (const dd of DD_FILTERS) {
    const set = f[dd.key];
    if (set.size && !set.has(dd.key === "status" ? status : j[dd.field])) return false;
  }
  // Dismissed needs an explicit Status → dismissed pick to show (QA pass 1);
  // an empty status selection used to mean "show all".
  if (!f.status.size && status === "dismissed") return false;
  if (f.salaryFloor) {
    if (!j.salary_stated) {
      if (!f.unknownComp) return false;
    } else if ((j.salary_max ?? 0) < Number(f.salaryFloor)) {
      return false;
    }
  } else if (!f.unknownComp && !j.salary_stated) {
    return false;
  }
  // Manually elevated jobs bypass the fit floor / hide-0-fit — the user has
  // pulled them into positive fit regardless of the model score.
  if (f.fitFloor && !j.manually_elevated && (j.fit_score ?? -1) < Number(f.fitFloor)) return false;
  // Strict === 0: that's the Tier 1 hard-fail sentinel. NULL = not scored
  // yet (pending), which must stay visible.
  if (f.hideZeroFit && isHardFailFit(j)) return false;
  if (groups.length) {
    const haystack = normSearch(`${j.title} ${j.company_name} ${j.location || ""}`);
    if (!groups.some((g) => g.every((t) => haystack.includes(t)))) return false;
  }
  return true;
}

/* #58: how many roles the default "hide 0-fit jobs" switch is holding back
   within the current scope — the Tier-1 hard-excludes the user most wants to see
   credited. Zero once the switch is already off (nothing is being hidden by it).
   Respects every OTHER active filter so the count matches what "Show" reveals. */
function hiddenByHardFilters() {
  const f = state.filters;
  if (!f.hideZeroFit) return 0;
  const groups = searchGroups(f.q);
  const shown = { ...f, hideZeroFit: false };
  return state.jobs.filter((j) => isHardFailFit(j) && matchesFilters(j, shown, groups)).length;
}

function filtered() {
  const f = state.filters;
  const groups = searchGroups(f.q);
  const rows = state.jobs.filter((j) => matchesFilters(j, f, groups));
  // Tier 1 hard-fails (fit_score===0, not elevated) always sink to the bottom,
  // regardless of sort. The rest ("top") is ordered by the chosen sort:
  //   Fit (default): elevated pinned first, then fit_score desc with NULL
  //     (unscored) below any positive score, last_seen breaking ties.
  //   Newest / Salary: that key desc, last_seen tiebreak (no elevated pin).
  const top = rows.filter((j) => !isHardFailFit(j));
  const bottom = rows.filter(isHardFailFit);
  const seen = (j) => Date.parse(j.last_seen) || 0;
  const byNewest = (a, b) => seen(b) - seen(a);
  const bySalary = (a, b) => (b.salary_max ?? -1) - (a.salary_max ?? -1) || byNewest(a, b);
  // 1e9 sentinel (not Infinity) so two elevated rows tie to 0, not NaN.
  const fitRank = (j) => (j.manually_elevated ? 1e9 : j.fit_score ?? -1);
  const byFit = (a, b) => fitRank(b) - fitRank(a) || byNewest(a, b);
  const cmp =
    f.sortBy === "newest" ? byNewest
    : f.sortBy === "salary" ? bySalary
    : byFit;
  return [...top.sort(cmp), ...bottom.sort(byNewest)];
}

/* "Modal failure" rollup: across the whole active set (NOT the filtered view —
   this is a pipeline-wide diagnostic), tally how often each soft near-miss flag
   fires, to answer "which single constraint is most often blocking me."
   - Denominator (total) = the "Active jobs" stat: active, not a Tier-1 hard-fail.
   - A flag's count = active jobs in the isNearMiss set carrying it — the exact
     flags the per-role panel would display (a job that scored positive despite a
     soft flag isn't "blocked", so it isn't counted), so the strip can't disagree
     with the detail pane. A multi-flag job counts once per distinct flag; the
     per-flag percentages are independent and won't sum to 100% — that's intended.
   Pure over its argument so it's unit-testable without the DOM/state. */
export function computeFlagRollup(jobs) {
  const active = jobs.filter((j) => j.status === "active" && !isHardFailFit(j));
  const total = active.length;
  const counts = new Map();
  for (const j of active) {
    if (!isNearMiss(j)) continue;
    for (const flag of j.near_miss_flags) {
      if (HIDDEN_FLAGS.has(flag)) continue; // internal provenance, never a rollup row
      counts.set(flag, (counts.get(flag) || 0) + 1);
    }
  }
  const rows = [...counts.entries()]
    .map(([flag, n]) => ({ flag, n, pct: total ? Math.round((n / total) * 100) : 0 }))
    .sort((a, b) => b.n - a.n || a.flag.localeCompare(b.flag));
  return { total, rows };
}

const flagRollup = () => computeFlagRollup(state.jobs);

function fmtSalary(job) {
  if (!job.salary_stated) return `<span class="job-salary unknown">comp unknown</span>`;
  const k = (n) => `${Math.round(n / 1000)}`;
  const text =
    job.salary_min === job.salary_max
      ? `$${k(job.salary_min)}k`
      : `$${k(job.salary_min)}–${k(job.salary_max)}k`;
  return `<span class="job-salary">${esc(text)}</span>`;
}

function fmtDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

/* A resolved application (rejected/withdrawn) that the user has NOT also
   dismissed. Dismissal outranks the application's fate (owner review, 2026-08-13): a
   dismissed job stays dismissed — same marker, same hidden-unless-filtered rule
   — whatever became of any application against it. */
function showsResolved(job) {
  return isResolvedApplication(job) && job.status !== "dismissed";
}

/* The row's lifecycle marker(s), shared by the list row and the detail subhead.
   Three cases, in precedence order:
     1. Resolved application (rejected/withdrawn) — the application is over, and
        that outranks whatever the listing is doing; a second "no longer listed"
        pill next to it would be noise. These rows also take the .closed fade.
     2. Applied + delisted — BOTH markers. The application is live, so the row
        must NOT fade (owner review, 2026-08-13), but the posting is gone and the pill
        has to say so beside the status.
     3. Everything else — the status pair, as before.
   Deliberately local to Jobs: today.js and companies.js render related but
   intentionally different treatments (Today omits the closed band entirely). */
function statusMarkerHtml(job) {
  if (showsResolved(job))
    return `<span class="resolved-band">${esc(job.application_status)}</span>`;
  if (job.status === "closed") return `<span class="closed-band">no longer listed</span>`;
  const applied = job.status === "applied" ? " status-pair-applied" : "";
  const pair = `<span class="status-pair${applied}"><span class="jobstatus-dot jobstatus-${esc(job.status)}"></span>${esc(job.status)}</span>`;
  return isDelisted(job) ? `${pair}<span class="closed-band">no longer listed</span>` : pair;
}

function listRow(job) {
  const isSelected = job.id === state.selectedId;
  // .closed (fade + muted title) marks a row that's over: a closed listing, a
  // Tier-1 hard fail, or a resolved application. NOT isDelisted — an applied job
  // whose req was pulled still has a live application and stays full strength.
  const dead = job.status === "closed" || isHardFailFit(job) || showsResolved(job);
  return `
    <div class="company-row job-row${isSelected ? " selected" : ""}${dead ? " closed" : ""}"
         data-action="select" data-id="${job.id}" role="button" tabindex="0">
      <div class="co-row-flex">
        ${companyLogoHtml({ name: job.company_name, logo: job.company_logo }, { size: "sm" })}
        <div class="co-row-rest">
          <div class="company-row-head">
            <span class="company-name">${fitChip(job)}${esc(job.title)}</span>
            ${fmtSalary(job)}
          </div>
          <div class="company-row-head">
            <span class="job-company">${esc(job.company_name)}</span>
            <span class="company-loc">${esc(job.location || "")}</span>
          </div>
          <div class="company-meta">
            ${isNew(job) ? `<span class="new-marker">new</span>` : ""}
            ${job.source === "manual" ? `<span class="source-tag">manual</span>` : ""}
            ${statusMarkerHtml(job)}
            ${isNearMiss(job) && !job.manually_elevated ? `<span class="nearmiss-tag">maybe</span>` : ""}
            <span class="remote-tag remote-${esc(job.remote_type || "unknown")}">${esc(job.remote_type || "unknown")}</span>
            <span class="company-loc">${esc(levelLabel(job.level_band))}</span>
          </div>
        </div>
      </div>
    </div>`;
}

function detailPane(job, detail) {
  if (!job) {
    return `
      <div class="detail-empty">
        <div class="detail-empty-mark">${HQ_MARK}</div>
        <p>Select a job to read the posting, see its scoring, and act on it.</p>
      </div>`;
  }

  /* Phase 7c: a drafting application starts here (job stays active); once one
     exists the row links to it instead. Mark applied promotes the draft. */
  // Manual elevate (QA): promote any scored-but-not-positive active job into
  // positive fit. Offered iff below POSITIVE_FIT (covers Tier-1 fails at 0,
  // near-misses, and plain mid scores); an unscored or already-positive job
  // gets no button — the single threshold that the chip + maybe tag also use.
  const elevateAction = job.manually_elevated
    ? `<button class="btn" data-action="elevate" data-elevated="0">Remove elevation</button>`
    : job.status === "active" && job.fit_score != null && job.fit_score < POSITIVE_FIT
      ? `<button class="btn" data-action="elevate" data-elevated="1">Elevate to positive fit</button>`
      : "";
  // The primary (amber) action tracks the LIKELY next step, and once an
  // application exists the application detail owns pipeline state — job detail
  // defers to it rather than showing a competing "Mark applied" (#57).
  const hasApp = !!job.application_id;
  const dismissBtn = `<button class="btn" data-action="dismiss-dialog">Dismiss</button>`;
  const statusActions =
    job.status === "dismissed" || job.status === "closed"
      ? `<button class="btn" data-action="set-status" data-status="active">Reactivate</button>
         ${hasApp ? `<a class="btn btn-ghost" href="#/applications/${job.application_id}">View application →</a>` : ""}`
      : hasApp
        ? // an application exists: lead with it, don't fight its status
          `<a class="btn btn-accent" href="#/applications/${job.application_id}">View application →</a>
           ${elevateAction}
           ${dismissBtn}`
        : isHardFailFit(job)
          ? // a Tier-1 hard-excluded role: no loud "apply" primary (#62-coupled) —
            // the override (elevate) and Dismiss are the coherent actions
            `${elevateAction}
             <button class="btn" data-action="start-application">Start application</button>
             ${dismissBtn}`
          : // a normal active role: the evaluative next step is the primary;
            // logging it applied-elsewhere is a quiet secondary
            `<button class="btn btn-accent" data-action="start-application">Start application</button>
             <button class="btn" data-action="set-status" data-status="applied" title="Applied somewhere else? Log it here.">Mark applied</button>
             ${elevateAction}
             ${dismissBtn}`;

  return `
    <div class="detail-content" data-id="${job.id}">
      <div class="detail-head">
        <button class="detail-back" data-action="close-detail" title="Back to list">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="15 18 9 12 15 6"/></svg>
          <span>Back</span>
        </button>
        <div class="detail-head-id">
          <a class="co-logo-link" href="#/companies/${job.company_id}" aria-label="View ${esc(job.company_name)} in Companies">${companyLogoHtml({ name: job.company_name, logo: job.company_logo }, { size: "lg" })}</a>
          <div class="detail-head-id-main">
            <div class="detail-head-row">
              <div class="detail-eyebrow"><a class="detail-eyebrow-link" href="#/companies/${job.company_id}">${esc(job.company_name)}</a> · ${esc(job.location || "no location")}</div>
            </div>
            <h2 class="detail-title">${esc(job.title)}</h2>
          </div>
        </div>
        <div class="detail-subhead">
          ${job.url ? `<a href="${escUrl(job.url)}" target="_blank" rel="noopener">view posting ↗</a>` : ""}
          ${job.source === "manual" ? `<span class="source-tag">manual</span>` : ""}
          ${statusMarkerHtml(job)}
          <span class="remote-tag remote-${esc(job.remote_type || "unknown")}">${esc(job.remote_type || "unknown")}</span>
        </div>
      </div>

      <div class="control-row">
        <div class="field"><span class="field-label">Level band</span><span>${esc(levelLabel(job.level_band))}</span></div>
        <div class="field"><span class="field-label">Salary</span>${fmtSalary(job)}</div>
        <div class="field"><span class="field-label">First seen</span><span title="${esc(fmtStamp(job.first_seen))}">${esc(fmtDate(job.first_seen))}</span></div>
        <div class="field"><span class="field-label">Last seen</span><span title="${esc(fmtStamp(job.last_seen))}">${esc(fmtDate(job.last_seen))}</span></div>
        <div class="field"><span class="field-label">Fit${helpHintHtml("fit-score")}</span><span>${fitChip(job)}${job.fit_quadrant ? `<span class="fit-quadrant">${esc(quadrantLabel(job.fit_quadrant))}</span>` : ""}</span></div>
      </div>

      <div class="section">
        <div class="section-head">
          <h2 class="section-title">Actions</h2>
        </div>
        <!-- Two groups, not eight flat controls (P4): the first moves the job
             through the pipeline, the second works on the record. Spacing
             alone carries the split — a divider rule breaks badly when the
             row wraps, and these are already outlined buttons. -->
        <div class="action-groups">
          <div class="action-group">
            ${statusActions}
          </div>
          <div class="action-group">
            <button class="btn btn-ghost" data-action="edit-details">Edit details</button>
            <button class="btn btn-ghost" data-action="log-activity">Log activity</button>
            <button class="btn btn-ghost" data-action="add-reminder">+ Reminder</button>
            <button class="btn btn-ghost" data-action="compose">Compose</button>
            ${detail?.description_text ? `<button class="btn btn-ghost" data-action="propose-rule">Propose exclusion rule</button>` : ""}
          </div>
        </div>
      </div>

      ${scoringSection(job, detail)}

      <div class="section">
        <div class="section-head">
          <h2 class="section-title">Activity</h2>
        </div>
        ${activityTimelineHtml(state.activityCache.get(job.id) || [])}
      </div>

      <div class="section">
        <div class="section-head">
          <h2 class="section-title">Posting</h2>
        </div>
        ${
          detail
            ? `<div class="jd-text">${esc(detail.description_text || "No description captured.")}</div>`
            : emptyState("Loading posting…")
        }
      </div>
    </div>`;
}

/* Scoring breakdown (Phase 4): Tier 1 results + near-miss flags + the model's
   notes. tier1_results/scoring_notes live on the detail row only; the tension
   classification is a "[tension: x] " prefix on scoring_notes (no schema
   column — see backend/app/scoring/__init__.py). */
function scoringSection(job, detail) {
  if (!detail) return "";
  if (!detail.tier1_results) {
    return `
      <div class="section">
        <div class="section-head"><h2 class="section-title">Scoring</h2></div>
        ${emptyState("Not scored yet — scores arrive with the next refresh.")}
      </div>`;
  }
  let tier1 = {};
  try {
    tier1 = JSON.parse(detail.tier1_results);
  } catch {
    /* malformed json: render nothing rather than crash the pane */
  }
  const notesRaw = detail.scoring_notes || "";
  const tensionMatch = notesRaw.match(/^\[tension: (\w+)\] ?/);
  const tension = tensionMatch ? tensionMatch[1] : null;
  const notes = tensionMatch ? notesRaw.slice(tensionMatch[0].length) : notesRaw;
  const flags = parseFlags({ ...job, near_miss_flags: detail.near_miss_flags }).near_miss_flags.filter(
    (f) => !HIDDEN_FLAGS.has(f)
  );

  const T1_LABELS = { comp: "Comp", location: "Location", sector: "Sector", title_band: "Title band" };
  // Dimension-aware wording for a Tier-1 cell (#62): the outcome value alone
  // (pass/fail/unknown) doesn't say which gate, and a "flag:<reason>" title band
  // rendered its raw token beside the glossed near-miss chips. Gloss both — the
  // internal token stays the styling key.
  const T1_FAIL = { comp: "below your floor", location: "out of range", sector: "excluded sector" };
  const T1_UNKNOWN = { comp: "no salary listed", location: "location unclear" };
  const t1cell = (key, value) => {
    const v = value || "unknown";
    if (v.startsWith("flag:")) {
      return `<span class="tier1-tag tier1-flag">${esc(flagLabel(v.slice(5)))}</span>`;
    }
    const text =
      v === "fail" ? T1_FAIL[key] || "fail"
      : v === "unknown" ? T1_UNKNOWN[key] || "unclear"
      : "pass";
    return `<span class="tier1-tag tier1-${esc(v)}">${esc(text)}</span>`;
  };

  return `
    <div class="section">
      <div class="section-head"><h2 class="section-title">Scoring</h2></div>
      <div class="tier1-row">
        ${Object.entries(T1_LABELS)
          .map(([k, label]) => `<div class="field"><span class="field-label">${label}</span>${t1cell(k, tier1[k])}</div>`)
          .join("")}
      </div>
      ${
        flags.length
          ? `<div class="scoring-flags">${flags.map((f) => `<span class="nearmiss-tag${f === "wrong_function" ? " nearmiss-fail" : ""}">${esc(flagLabel(f))}</span>`).join("")}</div>`
          : ""
      }
      ${tension ? `<div class="scoring-tension"><span class="field-label">Central tension</span> ${esc(tensionLabel(tension))}</div>` : ""}
      ${notes ? `<div class="scoring-notes">${mdToHtml(notes)}</div>` : ""}
    </div>`;
}

/* Company scope indicator (from #/jobs?company=<id>): a dismissible chip shown
   only while scoped. Clearing it drops the param from the hash, which re-renders
   the unscoped list. The name comes from any loaded job for that company. */
function scopeChipHtml() {
  const cid = state.filters.company;
  if (!cid) return "";
  const name = state.jobs.find((j) => j.company_id === cid)?.company_name || "this company";
  return `<button type="button" class="scope-chip" data-action="clear-company" title="Show all jobs">
      <span class="scope-chip-label">Company: ${esc(name)}</span>
      <span class="scope-chip-x" aria-hidden="true">✕</span>
    </button>`;
}

/* Flag scope indicator (from a flag-rollup row): a dismissible chip, twin of the
   company scope chip. Clearing it drops the flag filter and repaints the list. */
function flagScopeChipHtml() {
  const flag = state.filters.flag;
  if (!flag) return "";
  return `<button type="button" class="scope-chip" data-action="clear-flag" title="Show all jobs">
      <span class="scope-chip-label">Flag: ${esc(flagLabel(flag))}</span>
      <span class="scope-chip-x" aria-hidden="true">✕</span>
    </button>`;
}

/* The "modal failure" rollup strip: a collapsed one-liner at the top of Jobs
   pushing the single most-frequent soft flag at the reader, expanding inline to
   the full descending histogram. One component — full-width, stacks in normal
   flow on mobile (no separate mobile pattern). Lives between .filters and
   .layout so repaintList() (which swaps only .list-pane) never clobbers its
   open/closed state; a full paint() rebuilds it from state.rollupExpanded. */
function flagRollupHtml() {
  const { total, rows } = flagRollup();
  if (!rows.length) {
    const msg = total
      ? `No soft-flag patterns across ${total} active ${total === 1 ? "job" : "jobs"}`
      : "No active jobs to analyze";
    return `<div class="flag-rollup flag-rollup-empty">${esc(msg)}</div>`;
  }
  const cap = (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : s);
  // A flag with ≥1 carrier that rounds to 0% (large active set) reads "<1%",
  // never a bare "0%" that looks like nothing fired.
  const pctText = (r) => (r.pct === 0 && r.n > 0 ? "<1%" : `${r.pct}%`);
  const top = rows[0];
  const expanded = state.rollupExpanded;
  const chevron = `<svg class="flag-rollup-chevron${expanded ? " open" : ""}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="9 18 15 12 9 6"/></svg>`;
  const head = `
    <button type="button" class="flag-rollup-head" data-action="toggle-rollup" aria-expanded="${expanded}">
      <span class="flag-rollup-mark" aria-hidden="true">⚑</span>
      <span class="flag-rollup-headline">${esc(cap(flagLabel(top.flag)))} flagged on ${pctText(top)} of active set</span>
      ${chevron}
    </button>`;
  if (!expanded) return `<div class="flag-rollup">${head}</div>`;
  // Bars scale to the top flag (the most-frequent fills the track) so the shape
  // of the distribution is legible at a glance, not the absolute percentages.
  const maxN = top.n || 1;
  const body = rows
    .map((r) => {
      const w = Math.round((r.n / maxN) * 100);
      const active = state.filters.flag === r.flag ? " is-active" : "";
      return `
        <button type="button" class="flag-rollup-row${active}" data-action="filter-by-flag" data-flag="${esc(r.flag)}" title="Filter the list to roles flagged ${esc(flagLabel(r.flag))}">
          <span class="flag-rollup-label">${esc(flagLabel(r.flag))}</span>
          <span class="flag-rollup-track"><span class="flag-rollup-bar" style="width:${w}%"></span></span>
          <span class="flag-rollup-pct">${pctText(r)}</span>
          <span class="flag-rollup-count">(${r.n})</span>
        </button>`;
    })
    .join("");
  return `
    <div class="flag-rollup">
      ${head}
      <div class="flag-rollup-body">
        <div class="flag-rollup-caption">Across ${total} active ${total === 1 ? "job" : "jobs"} · sorted by frequency</div>
        ${body}
      </div>
    </div>`;
}

/* #58: a quiet, affirmative line that the hard filters DID something — otherwise
   an excluded role just silently vanishes and a skeptical user has no evidence
   the exclusion engine ran. "Show" flips the existing hide-0-fit reveal. Lives
   at the top of the list pane so repaintList() (the switch-toggle path) rebuilds
   it in step with the rows. */
function hiddenNoticeHtml() {
  const n = hiddenByHardFilters();
  if (!n) return "";
  return `<div class="jobs-hidden-note">
      <span class="jobs-hidden-count">${n} ${n === 1 ? "role" : "roles"} hidden by your hard filters.</span>
      <button type="button" class="jobs-hidden-show" data-action="reveal-hidden">Show</button>
    </div>`;
}

function listBodyHtml() {
  const rows = filtered();
  const list = rows.length
    ? rows.map(listRow).join("")
    : emptyState(
        state.jobs.length
          ? "No jobs match the current filters."
          : "No jobs yet — they'll appear here once your first board is pulled.",
        { pad: true }
      );
  return hiddenNoticeHtml() + list;
}

function template() {
  return `
    <div class="filters filters-collapsible">
      <div class="filter-group">
        ${ALL_DD.map((dd) => ddTemplate(dd, state.filters)).join("")}
      </div>
      ${scopeChipHtml()}${flagScopeChipHtml()}
      ${searchBoxHtml("Search jobs…", state.filters.q)}${helpHintHtml("search-syntax")}
      ${summaryPillHtml(ALL_DD, state.filters)}
      <div class="actions-right">
        <button class="btn btn-collapse" data-action="refresh" aria-label="Refresh boards"><span aria-hidden="true">↻</span><span class="btn-label"> Refresh boards</span></button>
      </div>
    </div>
    ${flagRollupHtml()}
    <div class="layout jobs-layout">
      <div class="list-pane${state.mobileDetail ? " mobile-hide" : ""}">
        ${listBodyHtml()}
      </div>
      <div class="detail-pane${state.mobileDetail ? " mobile-show" : ""}">
        ${detailPane(selected(), state.detailCache.get(state.selectedId))}
      </div>
    </div>`;
}

function renderStats() {
  const active = state.jobs.filter((j) => j.status === "active" && !isHardFailFit(j)).length;
  const fresh = state.jobs.filter((j) => j.status === "active" && !isHardFailFit(j) && isNew(j)).length;
  setStats([
    { value: active, label: pluralize(active, "Active job", "Active jobs") },
    { value: fresh, label: "New" },
  ]);
}

/* No detail-pane fade — tried in P5, removed the same day (owner live review).
   paint() destroys the outgoing content synchronously via innerHTML, so a
   fade-IN on the replacement renders the pane EMPTY for its opening frames:
   content, blank, content. That gap is the "flash", and it is created BY the
   animation — an instant swap has no blank frame at all. Fading a replacement
   without a gap needs a real crossfade (snapshot the outgoing node, overlay it,
   fade the two past each other), which is the only fix worth trying if this is
   ever revisited. Do not re-add a bare fade-in here. */
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
  pane.innerHTML = listBodyHtml();
}

async function loadDetail(id) {
  if (state.detailCache.has(id) && state.activityCache.has(id)) return;
  try {
    const [detail, activities] = await Promise.all([
      state.detailCache.get(id) || api.getJob(id),
      api.listActivities({ entity_type: "job", entity_id: id }),
    ]);
    state.detailCache.set(id, detail);
    state.activityCache.set(id, activities);
  } catch (error) {
    toast(error.detail || error.message, { error: true });
    return;
  }
  if (state.selectedId === id) paint();
}

async function reload() {
  const keep = state.selectedId;
  await load();
  state.selectedId = state.jobs.some((j) => j.id === keep) ? keep : null;
  paint();
}

async function setStatus(status) {
  const job = selected();
  if (!job) return;
  try {
    const updated = await api.setJobStatus(job.id, status);
    state.detailCache.set(job.id, updated);
    state.activityCache.delete(job.id); // applied writes an activity row
    await reload();
    if (state.selectedId === job.id) loadDetail(job.id);
    const messages = { dismissed: "Dismissed", applied: "Application logged" };
    toast(messages[status] || "Reactivated");
  } catch (error) {
    toast(error.detail || error.message, { error: true });
  }
}

async function elevate(elevated) {
  const job = selected();
  if (!job) return;
  try {
    const updated = await api.elevateJob(job.id, elevated);
    state.detailCache.set(job.id, updated);
    await reload();
    if (state.selectedId === job.id) loadDetail(job.id);
    toast(elevated ? "Elevated to positive fit" : "Elevation removed");
  } catch (error) {
    toast(error.detail || error.message, { error: true });
  }
}

/* Correct an ATS job's facts (wrong location, missing salary). The edit sticks
   across board refreshes (server sets manually_edited) and re-scores the job, so a
   location/salary fix can move it out of the fit-0 hard-fail. Mirrors the company
   add-job modal; pre-filled from the current job. */
function openEditDetailsModal(job) {
  const num = (v) => (v == null ? "" : String(v));
  const remoteOptions = REMOTE_TYPES.map(
    (r) => `<option value="${r}"${r === (job.remote_type || "unknown") ? " selected" : ""}>${r}</option>`
  ).join("");
  openModal({
    title: "Edit job details",
    body: `
      <div class="form-hint">Correct a wrong/missing location or add a salary learned from a recruiter. Edits stick across board refreshes and re-score the job.</div>
      <div class="form-row">
        <div class="form-field"><label>Location</label><input name="location" value="${esc(job.location || "")}" placeholder="City, ST or Remote" /></div>
        <div class="form-field"><label>Remote</label><select name="remote_type">${remoteOptions}</select></div>
      </div>
      <div class="form-row">
        <div class="form-field"><label>Salary min</label><input name="salary_min" type="number" min="0" inputmode="numeric" value="${esc(num(job.salary_min))}" placeholder="e.g. 150000" /></div>
        <div class="form-field"><label>Salary max</label><input name="salary_max" type="number" min="0" inputmode="numeric" value="${esc(num(job.salary_max))}" placeholder="e.g. 190000" /></div>
      </div>`,
    footer: `
      <button type="button" class="btn" data-action="modal-close">Cancel</button>
      <button type="submit" class="btn btn-accent">Save &amp; re-score</button>`,
    onSubmit: async (form) => {
      const data = Object.fromEntries(new FormData(form));
      const btn = form.querySelector('button[type="submit"]');
      if (btn) {
        btn.disabled = true;
        btn.textContent = "Saving & scoring…";
      }
      try {
        const updated = await api.updateJobDetails(job.id, {
          location: data.location.trim() || null,
          remote_type: data.remote_type || "unknown",
          salary_min: data.salary_min ? Number(data.salary_min) : null,
          salary_max: data.salary_max ? Number(data.salary_max) : null,
        });
        closeModal();
        state.detailCache.set(job.id, updated);
        await reload();
        if (state.selectedId === job.id) loadDetail(job.id);
        toast(`Details updated${updated.fit_score != null ? ` — fit ${updated.fit_score}` : ""}`);
      } catch (error) {
        toast(error.detail || error.message, { error: true });
        if (btn) {
          btn.disabled = false;
          btn.textContent = "Save & re-score";
        }
      }
    },
  });
}

/* "Start application" → drafting row, then jump to it (Phase 7c). A 409
   means an application already exists — the cache is stale, so resync. */
async function startApplication(job) {
  try {
    const created = await api.createApplication({ job_id: job.id });
    location.hash = `#/applications/${created.id}`;
  } catch (error) {
    toast(error.detail || error.message, { error: true });
    if (error.status === 409) {
      state.detailCache.delete(job.id);
      await reload();
    }
  }
}

/* On-demand semantic exclusion (Phase 7i): ask Claude to read this JD and
   propose a scoring-layer role-mismatch rule. The proposal lands in the review
   queue under Settings → Scoring; it is never auto-applied. The Sonnet call
   takes a few seconds — toast on completion. */
async function proposeRule(job) {
  if (!job) return;
  toast("Reading the JD…");
  try {
    await api.proposeScoringRule(job.id);
    toast("Proposal added — review under Settings → Scoring");
  } catch (error) {
    toast(error.detail || error.message, { error: true });
  }
}

let dismissReasons = null; // fetched once per session; editable via settings

/* Dismiss confirmation with a reason. The reason feeds the
   scoring digest and the title-exclude suggestions; skipping it is allowed. */
async function openDismissDialog() {
  const job = selected();
  if (!job) return;
  // The list row has no JD; the cached detail (loaded on select) does. Only
  // offer the "propose a rule" opt-in when a description is actually available.
  const hasJd = !!state.detailCache.get(job.id)?.description_text;
  if (!dismissReasons) {
    try {
      dismissReasons = (await api.getSetting("dismiss_reasons")).value;
    } catch {
      dismissReasons = ["other"];
    }
  }
  openModal({
    title: `Dismiss “${job.title}”`,
    body: `
      <div class="form-field">
        <label for="dismiss-reason">Reason</label>
        <select id="dismiss-reason" name="reason">
          ${dismissReasons.map((r) => `<option value="${esc(r)}">${esc(r)}</option>`).join("")}
        </select>
      </div>
      <div class="form-field">
        <label for="dismiss-note">Note (optional)</label>
        <textarea id="dismiss-note" name="note" rows="2" placeholder="Anything worth remembering…"></textarea>
      </div>
      ${
        hasJd
          ? `<label class="form-check"><input type="checkbox" id="dismiss-propose" name="propose" /> Also propose a scoring rule from this JD</label>`
          : ""
      }`,
    footer: `
      <button type="button" class="btn" data-action="modal-close">Cancel</button>
      <button type="submit" class="btn btn-danger">Dismiss</button>`,
    onSubmit: async (form) => {
      const wantRule = form.propose && form.propose.checked;
      try {
        const updated = await api.dismissJob(job.id, form.reason.value, form.note.value.trim());
        closeModal();
        state.detailCache.set(job.id, updated);
        state.activityCache.delete(job.id); // dismissal writes an activity row
        // A dismissed job leaves the default list, so drop the selection too —
        // the detail returns to its empty state (QA pass 2). Status → dismissed
        // remains the escape hatch to find and reactivate it.
        state.selectedId = null;
        state.mobileDetail = false;
        setDetailHash("jobs", null); // Back must not return to the dismissed job
        await reload();
        toast("Dismissed");
        // Fire-and-forget after the dismiss lands; the proposal queues under
        // Settings → Scoring (the dismissed job row still carries the JD).
        if (wantRule) {
          proposeRule(job);
        }
      } catch (error) {
        toast(error.detail || error.message, { error: true });
      }
    },
  });
}

let rubricDoc = null; // fetched once per session; editing is file-first

/* How the score shows up in the app (QA 2026-06-15). Rendered above the rubric
   doc, NOT stored in fit_criteria.md — this is UI presentation, so it must never
   reach the Haiku prompt. Anchors POSITIVE_FIT for the reader. */
const UI_BANDS_NOTE = `
  <div class="rubric-bands">
    <p class="rubric-bands-lead">How a job's fit shows up in the app</p>
    <ul>
      <li><span class="fit-chip fit-high">${POSITIVE_FIT}+</span> positive fit — worth pursuing</li>
      <li><span class="fit-chip fit-mid">60–${POSITIVE_FIT - 1}</span> mixed — worth a closer look</li>
      <li><span class="fit-chip fit-low">&lt;60</span> weak fit (<span class="fit-chip fit-low">0</span> means it failed a hard gate — pay, location, or sector)</li>
      <li><span class="nearmiss-tag">maybe</span> a near-miss: otherwise strong but for one soft criterion, scored below ${POSITIVE_FIT}</li>
      <li><span class="fit-chip fit-elevated">elevated</span> you pinned it as positive by hand</li>
      <li><span class="fit-chip fit-none">–</span> not scored yet</li>
    </ul>
  </div>`;

/* "How scoring works" (QA pass 1): read-only render of DATA_DIR/fit_criteria.md
   so the rubric is inspectable without opening the repo. */
async function openRubricModal() {
  if (rubricDoc === null) {
    try {
      rubricDoc = (await api.getCriteriaDoc()).markdown;
    } catch (error) {
      toast(error.detail || error.message, { error: true });
      return;
    }
  }
  openModal({
    title: "How scoring works",
    body: `${UI_BANDS_NOTE}<div class="md-doc">${mdToHtml(rubricDoc)}</div>`,
    footer: `<button type="button" class="btn" data-action="modal-close">Close</button>`,
  });
}

async function triggerRefresh() {
  try {
    const r = await api.triggerRefresh();
    toast(r.running ? "Refresh already running…" : "Refreshing job boards…");
    pollRefresh();
  } catch (error) {
    toast(error.detail || error.message, { error: true });
  }
}

let pollTimer = null;

function pollRefresh() {
  clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    // Self-terminate if Jobs is no longer mounted: app.js renders every view
    // into the same #view element, so this leaked poll's completion reload()
    // would clobber whatever view is showing (same guard as the today.js/
    // settings.js/companies.js pollers; jobs-layout is the mount marker
    // because .filters/.layout/.list-pane are shared across views).
    if (!root || !root.querySelector(".jobs-layout")) {
      clearInterval(pollTimer);
      return;
    }
    try {
      const s = await api.refreshStatus();
      if (!s.running) {
        clearInterval(pollTimer);
        toast("Job boards refreshed");
        if (s.refresh_error) buzz("refresh:" + s.refresh_error.at);
        else chime("refresh:" + (s.refresh_report?.at || "done"));
        state.detailCache.clear();
        await reload();
      }
    } catch {
      clearInterval(pollTimer);
    }
  }, 5000);
}

bindOutsideClose(() => root);

function onClick(event) {
  const target = event.target.closest("[data-action]");
  if (!target || !root.contains(target)) return;
  switch (target.dataset.action) {
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
      clearFilterGroup(key, root.querySelector(`.filter-dd-panel[data-dd="${key}"]`));
      afterFilterChange(key); // panel stays open
      break;
    }
    case "open-filter-sheet":
      openFilterSheet();
      break;
    case "clear-company": {
      // Clear the scope directly rather than relying on a hashchange: a prior
      // dismiss/close may have already stripped the ?company param, so re-setting
      // the same hash wouldn't fire hashchange. Drop the query explicitly (keep
      // the detail path if a job is open) and repaint the unscoped list.
      state.filters.company = null;
      const bare = state.selectedId ? `#/jobs/${state.selectedId}` : "#/jobs";
      history.replaceState(history.state, "", bare);
      paint();
      break;
    }
    case "toggle-rollup":
      // UI-only: a full paint() rebuilds the strip from state.rollupExpanded and
      // preserves list/detail scroll. The toggle is infrequent, so the detail
      // re-render (cached, no network) is a non-issue.
      state.rollupExpanded = !state.rollupExpanded;
      paint();
      break;
    case "filter-by-flag":
      // paint() (not repaintList) so the new "Flag: …" scope chip in .filters
      // appears alongside the re-filtered list.
      state.filters.flag = target.dataset.flag;
      paint();
      break;
    case "clear-flag":
      state.filters.flag = null;
      paint();
      break;
    case "reveal-hidden":
      // #58: flip the same reveal the Fit dropdown's "hide 0-fit jobs" switch
      // owns. Full paint() so that switch (in .filters) reflects the new state
      // and the notice (now counting 0) clears.
      state.filters.hideZeroFit = false;
      paint();
      break;
    case "select":
      state.selectedId = Number(target.dataset.id);
      state.mobileDetail = true;
      paint({ detailToTop: true });
      loadDetail(state.selectedId);
      setDetailHash("jobs", state.selectedId);
      break;
    case "close-detail":
      // Our own history entry → back() pops it (popstate → hashchange → render);
      // cold deep-link entry → rewrite the hash in place and close locally.
      if (history.state?.hqDetail) {
        history.back();
      } else {
        state.mobileDetail = false;
        setDetailHash("jobs", null);
        paint();
      }
      break;
    case "set-status":
      setStatus(target.dataset.status);
      break;
    case "elevate":
      elevate(target.dataset.elevated === "1");
      break;
    case "edit-details": {
      const job = selected();
      if (job) openEditDetailsModal(job);
      break;
    }
    case "start-application": {
      const job = selected();
      if (job) startApplication(job);
      break;
    }
    case "dismiss-dialog":
      openDismissDialog();
      break;
    case "log-activity": {
      const job = selected();
      if (!job) break;
      openActivityModal({
        entity_type: "job",
        entity_id: job.id,
        entity_label: `${job.title} @ ${job.company_name}`,
        onSaved: () => {
          state.activityCache.delete(job.id);
          loadDetail(job.id);
        },
      });
      break;
    }
    case "compose": {
      const job = selected();
      if (!job) break;
      openComposeModal({
        entity_type: "job",
        entity_id: job.id,
        entity_label: `${job.title} @ ${job.company_name}`,
        onLogged: () => {
          state.activityCache.delete(job.id);
          loadDetail(job.id);
        },
      });
      break;
    }
    case "propose-rule": {
      const job = selected();
      if (job) proposeRule(job);
      break;
    }
    case "add-reminder": {
      const job = selected();
      if (!job) break;
      openReminderModal({
        prefill: {
          title: `Re: ${job.title} @ ${job.company_name}`,
          entity_type: "job",
          entity_id: job.id,
          entity_label: `${job.title} @ ${job.company_name}`,
        },
      });
      break;
    }
    case "refresh":
      triggerRefresh();
      break;
    case "fit-help":
      closeDropdowns(root);
      openRubricModal();
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
  }
}

/* Filter-change core, shared by the root-delegated handlers (popup pills)
   and the mobile filter sheet — the sheet is a modal in document.body, so
   root's delegated listeners never see its events. */
function applyFilterInput(input) {
  const key = input.dataset.dd;
  if (!key) return null;
  if (input.dataset.switch) {
    state.filters[input.dataset.switch] = input.checked;
  } else if (input.type === "radio") {
    state.filters[key] = input.value;
  } else if (input.type === "checkbox") {
    const set = state.filters[key];
    if (input.checked) set.add(input.value);
    else set.delete(input.value);
  } else {
    return null;
  }
  return key;
}

function clearFilterGroup(key, container) {
  state.filters[key].clear();
  container.querySelectorAll("input[type=checkbox]").forEach((box) => (box.checked = false));
}

/* fromSheet: mirror the change back onto the hidden popup panel's inputs so
   they don't lie when the viewport widens past the collapse breakpoint. */
function afterFilterChange(key, { fromSheet = false } = {}) {
  updateToggle(root, ddByKey(key), state.filters);
  if (fromSheet) syncPanelInputs(key);
  updateSummaryPill(root, ALL_DD, state.filters);
  repaintList();
}

function syncPanelInputs(key) {
  const panel = root.querySelector(`.filter-dd-panel[data-dd="${key}"]`);
  if (!panel) return;
  panel.querySelectorAll("input").forEach((input) => {
    if (input.dataset.switch) input.checked = state.filters[input.dataset.switch];
    else if (input.type === "radio") input.checked = input.value === state.filters[key];
    else if (input.type === "checkbox") input.checked = state.filters[key].has(input.value);
  });
}

function sheetSectionHtml(dd, filters) {
  return `
    <div class="sheet-section" data-dd="${dd.key}">
      <div class="sheet-section-title">${esc(dd.label)}</div>
      ${optionsHtml(dd, filters)}
    </div>`;
}

/* ≤640px: the seven pills collapse to one "Filters · N" pill; this sheet is
   its panel — every group's options at once, list repainting live behind the
   scrim. Done/Escape/overlay-click just close: state is applied on change,
   so there is nothing to confirm or lose. The fit footer swaps modals
   (openModal is single-active) — accepted. */
function openFilterSheet() {
  closeDropdowns(root);
  const overlay = openModal({
    title: "Filters",
    body: ALL_DD.map((dd) => sheetSectionHtml(dd, state.filters)).join(""),
    footer: `<button type="button" class="btn btn-accent" data-action="modal-close">Done</button>`,
  });
  overlay.addEventListener("change", (event) => {
    const key = applyFilterInput(event.target);
    if (key) afterFilterChange(key, { fromSheet: true });
  });
  overlay.addEventListener("click", (event) => {
    const target = event.target.closest("[data-action]");
    if (!target) return;
    if (target.dataset.action === "dd-clear") {
      clearFilterGroup(target.dataset.dd, target.closest(".sheet-section"));
      afterFilterChange(target.dataset.dd, { fromSheet: true });
    } else if (target.dataset.action === "fit-help") {
      openRubricModal();
    }
  });
}

function onChange(event) {
  const input = event.target;
  const key = applyFilterInput(input);
  if (!key) return;
  afterFilterChange(key);
  // a radio choice is terminal — close the panel; switches/checkboxes keep it open
  if (input.type === "radio" && !input.dataset.switch) closeDropdowns(root);
}

function onInput(event) {
  if (event.target.dataset.action === "search") {
    state.filters.q = event.target.value;
    root.querySelector(".search-clear")?.classList.toggle("hide", !state.filters.q);
    repaintList();
  }
}

export async function render(container, preselectId = null, params = {}) {
  root = container;
  renderLoading(container);
  container.onclick = onClick;
  container.onchange = onChange;
  container.oninput = onInput;
  setFocusOut(container, null);
  setRowKeys(container, onClick);
  // Hash query param is the source of truth for the company scope: set it on
  // every mount (default null) so back/forward and the clear-chip stay in sync.
  state.filters.company = params.company ? Number(params.company) : null;
  try {
    await load();
  } catch (error) {
    renderLoadError(container, error, () => render(container, preselectId, params));
    setStats([]);
    return;
  }
  // Scoped to a company (#/jobs?company=<id>): don't leave a stale selection from
  // another company showing in the detail pane. Key off what's actually visible
  // (filtered() respects the dismissed/fit filters) — drop a selection that isn't
  // shown, and auto-select when exactly one job is visible so "View all 1 job →"
  // lands right on it instead of an unrelated posting.
  if (state.filters.company && !preselectId) {
    const visible = filtered();
    if (!visible.some((j) => j.id === state.selectedId)) {
      state.selectedId = visible.length === 1 ? visible[0].id : null;
    }
  }
  // Desktop first paint lands on the product, not the watermark (P4). With no
  // selection the detail pane was a 56px "hq" mark over the majority of the
  // pixels a reviewer looks at hardest — at 1920 that is 1440 of 1920px wide.
  // filtered() is already fully sorted (elevated pinned, then the chosen sort,
  // hard-fails sunk), so [0] is the top-ranked visible row by definition.
  //
  // Guards, in order: never override a route preselection; never yank a
  // selection the user already made (state is module-level and survives
  // remounts); and never do it on the stacked layout, where selecting would
  // push the phone straight into the detail instead of the list. The media
  // query mirrors .layout's own breakpoint in app.css — change them together.
  if (!preselectId && !state.selectedId && !window.matchMedia("(max-width: 900px)").matches) {
    const visible = filtered();
    if (visible.length) state.selectedId = visible[0].id;
  }
  if (preselectId && state.jobs.some((j) => j.id === preselectId)) {
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
  if (state.selectedId) loadDetail(state.selectedId);
}
