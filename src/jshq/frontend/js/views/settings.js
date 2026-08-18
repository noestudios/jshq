/* Settings (Phase 7h foundation, 7i structural rework): in-app editing of the
   lists and fit criteria that previously needed hand-edits to the settings
   table / DATA_DIR/fit_criteria.md.

   7i grouped the flat page into three in-view sub-tabs (Sourcing / Scoring /
   System) so every future surface has a home and the staged-save model is bounded.

   Two save models live on one page:
   - Title + dismiss-reason lists (Sourcing) are settings-table rows: each chip
     add/remove PUTs /api/settings/{key} immediately.
   - Fit criteria (Scoring: Tier 1 params + Tier 2 ranked list) is file-first:
     edits are held locally and written together via PUT /api/scoring/criteria on
     Save; existing jobs need a Rescore to pick up the change. A dirty flag drives
     a scoped sticky Save bar and a leave-the-view warning (canLeave) so unsaved
     criteria edits are never silently dropped.

   Criteria text inputs are read from the DOM into state on every click/Enter
   (syncCriteriaFromDom) so a structural repaint never drops an in-progress edit;
   paint() renders from state and never re-reads the DOM. */

import { api } from "../api.js";
import {
  confirmModal,
  esc,
  fmtAgo,
  fmtStamp,
  isMac,
  renderLoadError,
  renderLoading,
  setStats,
  toast,
} from "../lib/ui.js";
import { buzz, chime, preview, setSoundEnabled, soundEnabled } from "../lib/notify.js";
import { getThemePref, setThemePref } from "../lib/theme.js";
import { helpHintHtml } from "../lib/helpHint.js";
import { refresh as refreshOnboardingTracker } from "../lib/onboardingTracker.js";
// Aliased: `criteriaError` below is this view's own 422-validation state, a
// different thing from the doc-level parse failure the vocab endpoint reports.
import { criteriaError as vocabDocError, flagValues, levelBands } from "../lib/vocab.js";

let root = null;
let activeTab = "sourcing"; // view-local UI state (not server data); survives paint()
let criteriaBaseline = null; // last server-synced criteria payload (object — patched per-field)
let criteriaError = null; // { field, message } | null — persistent, field-anchored
let rulesError = null; // { message } | null — persistent inclusion-rules error (not a toast)
let pendingFocus = null; // { sel, start?, end? } | null — preferred focus target after next paint
let justAddedChip = null; // "kind|key|value" of the chip to fade in on the next paint (one-shot)
// Add-rule composer draft (survives paint; synced DOM->state like criteria text).
let composer = { verb: "include", target: "title", term: "" };
// Which "Advanced — compiled from rules" disclosures are open (view-local, like activeTab).
const expandedAdvanced = new Set();

// The three raw arrays a rule compiles into (decision C). Each lives in
// state.compiled as [{value, source: "rule"|"manual"}].
const COMPILED_ARRAYS = ["title_keywords", "title_exclude_keywords", "location_allowlist"];

const TABS = [
  { id: "sourcing", label: "Sourcing" },
  { id: "scoring", label: "Scoring" },
  { id: "system", label: "System" },
];

/* Tier 1 keys, used to map a 422 detail string back to the field it names. */
const CRIT_FIELDS = [
  "comp_floor",
  "comp_target",
  "location_allowlist",
  "location_radius",
  "company_location_overrides",
  "remote_regions",
  "excluded_sectors",
  "target_title_bands",
  "flag_title_bands",
];
const CRIT_LABELS = {
  comp_floor: "Comp floor",
  comp_target: "Comp target",
  location_allowlist: "Location allowlist",
  location_radius: "Location radius",
  company_location_overrides: "Per-company location overrides",
  remote_regions: "Remote regions",
  excluded_sectors: "Industries to avoid",
  target_title_bands: "Target title bands",
  flag_title_bands: "Title-seniority flags",
};

/* Singular item type per list, for the "Please add a …" empty-add prompt. */
const ITEM_NOUNS = {
  title_keywords: "title keyword",
  title_exclude_keywords: "exclusion keyword",
  dismiss_reasons: "dismissal reason",
  contact_sources: "contact source",
  location_allowlist: "location",
  remote_regions: "remote region",
  excluded_sectors: "industry",
  target_title_bands: "title band",
};

const state = {
  settings: { dismiss_reasons: [], contact_sources: [] }, // title arrays are rule-managed now (state.compiled)
  suggestions: [], // typed [{type:"title_exclude", keyword, count, examples}] — Sourcing
  scoringProposals: [], // typed [{type:"scoring_rule", id, text, rationale, source, job_title}] — Scoring queue
  scoringRules: [], // accepted learned scoring rules [{id, text, source, job_title}] — Scoring active list
  companies: [], // known company names, for the per-company override dropdown
  criteria: null, // {tier1_params, tier2_criteria, _overrides:[{key,val}], _bands:[{key,val}]}
  rules: [], // [{id, verb, target, terms:[...]}] — source of truth (decision C)
  compiled: { title_keywords: [], title_exclude_keywords: [], location_allowlist: [] }, // [{value, source}]
  status: null, // {last_refresh, last_rescore, running}
  rescoring: false,
  notifyPopups: true, // settings.notify_popups — absent key = ON (only stored false disables)
  apiKeyDeclined: false, // settings.api_key_declined — explicit "I don't want a key"
  apiKey: null, // {configured, masked, source, editable} from GET /api/settings/api-key
  apiKeyTest: null, // {ok, error} | "pending" | null — last Test result (view-local)
  persona: null, // {display_name, domain_label} from GET /api/scoring/persona
  voiceGuide: "", // the editable voice-guide markdown (working copy, synced from the textarea)
  synthesis: null, // {proposal, available} from GET /api/scoring/synthesis
  synthesisBusy: false, // Draft-with-AI in flight
  synthesisReply: "", // paste-back working copy (synced from the textarea, voice-guide style)
  synthesisReplyError: null, // 422 detail from the last Check reply
  synthesisPrompt: null, // clipboard-blocked fallback: the prompt shown for manual copy
  synthesisApplyTier2: false, // the "also update ranked list" toggle
};

/* ---------- data ---------- */

async function load() {
  const [reasons, sources, suggestions, criteria, status, companies, rulesData, scoringRules, popups, apiKey, persona, voiceGuide, synthesis, declined] =
    await Promise.all([
      api.getSetting("dismiss_reasons"),
      api.getSetting("contact_sources"),
      api.getSuggestions(),
      api.getCriteria(),
      api.refreshStatus(),
      api.listCompanies(),
      api.getInclusionRules(),
      api.getScoringRules(),
      api.getSetting("notify_popups"),
      api.getApiKeyStatus(),
      api.getPersona(),
      api.getVoiceGuide(),
      api.getSynthesis().catch(() => null),
      api.getSetting("api_key_declined"),
    ]);
  state.synthesis = synthesis;
  state.apiKey = apiKey;
  state.apiKeyDeclined = declined.value === true; // absent key returns [] — treat as not declined
  state.persona = persona;
  state.voiceGuide = voiceGuide.markdown || "";
  state.notifyPopups = popups.value !== false; // absent key returns [] — still ON
  state.settings.dismiss_reasons = reasons.value || [];
  state.settings.contact_sources = sources.value || [];
  applySuggestions(suggestions);
  state.scoringRules = scoringRules.rules || [];
  state.status = status;
  state.companies = (companies || [])
    .map((c) => c.name)
    .filter(Boolean)
    .sort((a, b) => a.localeCompare(b));
  setCriteria(criteria); // arms criteriaBaseline (incl. location_allowlist)
  applyRulesResult(rulesData); // overlays the rule-owned location_allowlist (same value)
}

/* Split the /api/suggestions payload into the two typed surfaces and stamp each
   with its `type` so suggestionCard()/onSuggestion() can dispatch (shortlist 7).
   title_exclude stays in Sourcing; scoring_rule proposals go to Scoring. */
function applySuggestions(suggestions) {
  state.suggestions = (suggestions.title_exclude || []).map((s) => ({
    ...s,
    type: "title_exclude",
  }));
  state.scoringProposals = (suggestions.scoring_rule || []).map((s) => ({
    ...s,
    type: "scoring_rule",
  }));
}

function setCriteria(criteria) {
  const p = criteria.tier1_params;
  state.criteria = {
    tier1_params: p,
    // Tier 2 is {text, weight, craft, bonus_only}[] (weights Phase 8, markers
    // Phase 2). Normalize defensively so a legacy string payload or a missing
    // weight still lands as a 1.0-weighted item. The markers are not editable
    // here; they are kept on the item so a save round-trips them.
    tier2_criteria: (criteria.tier2_criteria || []).map((t) =>
      typeof t === "string"
        ? { text: t, weight: 1, craft: false, bonus_only: false }
        : {
            text: t.text || "",
            weight: readWeight(t.weight),
            craft: !!t.craft,
            bonus_only: !!t.bonus_only,
          }
    ),
    _overrides: Object.entries(p.company_location_overrides || {}).map(([key, v]) => ({
      key,
      val: (v || []).join(", "),
    })),
    _bands: Object.entries(p.flag_title_bands || {}).map(([key, val]) => ({ key, val })),
  };
  // Re-arm the dirty baseline: covers initial load and the post-save refresh.
  // An object (not a JSON string) so a rule-driven location_allowlist change can
  // be patched into it in place without clobbering other unsaved criteria edits.
  criteriaBaseline = JSON.parse(JSON.stringify(criteriaPayload()));
}

/* Apply an inclusion-rules GET/PUT result: rules + compiled arrays. Keep the
   criteria copy of location_allowlist (which the criteria payload/dirty model
   still carries) in sync with the rule-owned value, and patch ONLY that field of
   the dirty baseline so a rules save never falsely trips — or falsely clears —
   the Scoring "Unsaved changes" flag. */
function applyRulesResult(res) {
  state.rules = res.rules || [];
  state.compiled = res.compiled || {
    title_keywords: [],
    title_exclude_keywords: [],
    location_allowlist: [],
  };
  const newLoc = (state.compiled.location_allowlist || []).map((e) => e.value);
  if (state.criteria) {
    state.criteria.tier1_params.location_allowlist = newLoc;
    if (criteriaBaseline && criteriaBaseline.tier1_params) {
      criteriaBaseline.tier1_params.location_allowlist = newLoc.slice();
    }
  }
}

/* The exact body PUT /api/scoring/criteria expects — the single source of truth
   for both saving and dirty detection (so a no-op edit reads as not-dirty). */
function criteriaPayload() {
  const p = { ...state.criteria.tier1_params };
  p.company_location_overrides = rowsToMap(state.criteria._overrides, true);
  p.flag_title_bands = rowsToMap(state.criteria._bands, false);
  const tier2 = state.criteria.tier2_criteria
    .map((t) => ({
      text: (t.text || "").trim(),
      weight: readWeight(t.weight),
      craft: !!t.craft,
      bonus_only: !!t.bonus_only,
    }))
    .filter((t) => t.text);
  return { tier1_params: p, tier2_criteria: tier2 };
}

/* Dirty from current in-memory state (no DOM read — safe to call during paint).
   onClick syncs the DOM into state before every mutating paint, so state is
   current at paint time. */
function criteriaDirtyFromState() {
  return !!state.criteria && JSON.stringify(criteriaPayload()) !== JSON.stringify(criteriaBaseline);
}

/* ---------- markup helpers ---------- */

/* Chip editor. `values` is either a string[] (all manual/removable — every
   legacy call site) or a {value, source}[] for the compiled "Advanced" view
   (decision C): rule chips render read-only with a "rule" badge — edit the rule,
   not the chip — while manual chips stay removable and the add-input adds a
   manual one-off. */
function tagsHtml(kind, key, values, { addInput = true, options } = {}) {
  const items = (values || []).map((v) =>
    typeof v === "string" ? { value: v, source: "manual" } : v
  );
  // Closed-vocabulary mode: `options` ([{value, label}]) swaps the free-text
  // add for a select of the not-yet-chosen tokens. The stored values stay raw
  // tokens; only the display uses the label. Built for the title-band fields,
  // whose vocabulary is the doc's emittable bands — free text there produced
  // names no level_bands entry could emit, which the loader (rightly) rejects.
  const label = (v) => options?.find((o) => o.value === v)?.label || v;
  const remaining = options?.filter((o) => !items.some((it) => it.value === o.value));
  return `
    <div class="settings-tagfield">
      ${
        addInput
          ? `
      <div class="settings-add">
        ${
          options
            ? `<select class="settings-add-input" data-add data-kind="${kind}" data-key="${esc(key)}" aria-label="Add to ${esc(key)}">
          <option value="" selected disabled hidden>${remaining.length ? "add…" : "all added"}</option>
          ${remaining.map((o) => `<option value="${esc(o.value)}">${esc(o.label)}</option>`).join("")}
        </select>`
            : `<input type="text" class="settings-add-input" data-add data-kind="${kind}" data-key="${esc(key)}" placeholder="add…" aria-label="Add to ${esc(key)}" />`
        }
        <button type="button" class="btn btn-ghost" data-action="tag-add" data-kind="${kind}" data-key="${esc(key)}">Add</button>
      </div>`
          : ""
      }
      <div class="settings-tags">
        ${items
          .map((it) => {
            const v = it.value;
            const isNew = justAddedChip === `${kind}|${key}|${v}`;
            return `
        <span class="settings-tag${isNew ? " settings-tag--enter" : ""}" data-origin="${it.source}">${esc(label(v))}${
              it.source === "rule"
                ? `
          <span class="settings-tag-prov" title="Compiled from a rule above">rule</span>`
                : `
          <button type="button" class="settings-tag-x" data-action="tag-remove" data-kind="${kind}" data-key="${esc(key)}" data-value="${esc(v)}" aria-label="Remove ${esc(v)}">×</button>`
            }
        </span>`;
          })
          .join("")}
      </div>
    </div>`;
}

function mapEditorHtml(map, rows, { keyLabel, valLabel, valPlaceholder, valOptions, keyOptions: rawKeyOptions }) {
  // keyOptions entries are strings (value doubles as label — company names) or
  // {value, label} pairs (title bands: the stored token and its display label).
  const keyOptions = rawKeyOptions?.map((o) => (typeof o === "string" ? { value: o, label: o } : o));
  const valInput = (r) => {
    if (!valOptions) {
      return `<input type="text" data-mapval value="${esc(r.val)}" placeholder="${esc(valPlaceholder)}" aria-label="${esc(valLabel)}" />`;
    }
    // include the current value even if it's not in the enum, so a save never drops it
    const opts = [...new Set([...valOptions, r.val].filter(Boolean))];
    return `<select data-mapval aria-label="${esc(valLabel)}">${opts
      .map((o) => `<option value="${esc(o)}"${o === r.val ? " selected" : ""}>${esc(o)}</option>`)
      .join("")}</select>`;
  };
  const keyInput = (r) => {
    if (!keyOptions) {
      return `<input type="text" data-mapkey value="${esc(r.key)}" placeholder="${esc(keyLabel)}" aria-label="${esc(keyLabel)}" />`;
    }
    const known = keyOptions.some((o) => o.value === r.key);
    const placeholder = r.key
      ? ""
      : `<option value="" selected disabled hidden>Select ${esc(keyLabel.toLowerCase())}…</option>`;
    // keep an unknown current value (a removed company) selectable so a save never drops it
    const extra = r.key && !known ? `<option value="${esc(r.key)}" selected>${esc(r.key)}</option>` : "";
    const opts = keyOptions
      .map((o) => `<option value="${esc(o.value)}"${o.value === r.key ? " selected" : ""}>${esc(o.label)}</option>`)
      .join("");
    return `<select data-mapkey aria-label="${esc(keyLabel)}">${placeholder}${extra}${opts}</select>`;
  };
  // A row whose company is no longer in the known list is hidden, not dropped:
  // it stays in the DOM (so sync/save preserve it) and reappears if re-added.
  const hidden = (r) => keyOptions && r.key && !keyOptions.some((o) => o.value === r.key);
  return `
    <div class="map-editor" data-map-container="${map}">
      ${rows
        .map(
          (r, i) => `
        <div class="map-row${hidden(r) ? " map-row--hidden" : ""}" data-map="${map}" data-index="${i}">
          ${keyInput(r)}
          ${valInput(r)}
          <button type="button" class="btn btn-ghost btn-danger" data-action="map-remove" data-map="${map}" data-index="${i}">Remove</button>
        </div>`
        )
        .join("")}
      <button type="button" class="btn btn-ghost" data-action="map-add" data-map="${map}">+ Add ${esc(keyLabel.toLowerCase())}</button>
    </div>`;
}

/* One accept/ignore card. Typed (shortlist 7): title_exclude keeps the
   ingestion-filter voice; scoring_rule is a description-based learned rule that
   acts at the AI scoring layer. Both reuse the .suggestion-card chrome. */
function suggestionCard(s) {
  return s.type === "scoring_rule" ? scoringProposalCard(s) : titleExcludeCard(s);
}

function titleExcludeCard(s) {
  return `
    <div class="suggestion-card">
      <div class="suggestion-text">
        Dismissed ${esc(s.count)}× — exclude <strong>"${esc(s.keyword)}"</strong> from ingestion?
        <div class="suggestion-examples">${(s.examples || []).map(esc).join("<br>")}</div>
      </div>
      <div class="suggestion-actions">
        <button class="btn" data-action="suggestion" data-type="title_exclude" data-verb="accept" data-key="${esc(s.keyword)}">Accept</button>
        <button class="btn btn-ghost" data-action="suggestion" data-type="title_exclude" data-verb="ignore" data-key="${esc(s.keyword)}">Ignore</button>
      </div>
    </div>`;
}

function sourceLabel(source) {
  return source === "title" ? "title-based" : "description-based";
}

function scoringProposalCard(s) {
  return `
    <div class="suggestion-card">
      <div class="suggestion-text">
        <span class="suggestion-source">${esc(sourceLabel(s.source))}</span>
        <strong>${esc(s.text)}</strong>
        ${s.rationale ? `<div class="suggestion-examples">${esc(s.rationale)}</div>` : ""}
        ${s.job_title ? `<div class="suggestion-origin">from “${esc(s.job_title)}”${s.company ? ` @ ${esc(s.company)}` : ""}</div>` : ""}
      </div>
      <div class="suggestion-actions">
        <button class="btn" data-action="suggestion" data-type="scoring_rule" data-verb="accept" data-key="${esc(s.id)}">Accept</button>
        <button class="btn btn-ghost" data-action="suggestion" data-type="scoring_rule" data-verb="ignore" data-key="${esc(s.id)}">Ignore</button>
      </div>
    </div>`;
}

/* The error span anchored under the field a 422 named (if any). */
function fieldError(key) {
  return criteriaError && criteriaError.field === key
    ? `<span class="field-error" role="alert">${esc(criteriaError.message)}</span>`
    : "";
}

/* ---------- inclusion rules (decision C) ---------- */

function rulePhrase(rule) {
  const verb = rule.verb === "include" ? "Always include" : "Don't include";
  const where = rule.target === "location" ? "in the location" : "in the job title";
  return `${verb} <span class="rule-card__where">${esc(where)}</span>`;
}

function ruleCardHtml(rule) {
  const id = esc(rule.id);
  return `
    <div class="rule-card" data-rule-id="${id}">
      <div class="rule-card__head">
        <span class="rule-card__phrase">${rulePhrase(rule)}</span>
        <button type="button" class="btn btn-ghost btn-danger" data-action="rule-remove" data-rule-id="${id}" aria-label="Remove rule">Remove</button>
      </div>
      <div class="settings-tags">
        ${(rule.terms || [])
          .map(
            (t) => `
        <span class="settings-tag" data-origin="rule">${esc(t)}
          <button type="button" class="settings-tag-x" data-action="rule-term-remove" data-rule-id="${id}" data-term="${esc(t)}" aria-label="Remove ${esc(t)}">×</button>
        </span>`
          )
          .join("")}
      </div>
      <div class="settings-add rule-term-add">
        <input type="text" class="settings-add-input" data-rule-term-input data-rule-id="${id}" placeholder="add term…" aria-label="Add a term to this rule" />
        <button type="button" class="btn btn-ghost" data-action="rule-term-add" data-rule-id="${id}">Add</button>
      </div>
    </div>`;
}

function ruleComposerHtml() {
  const locDisabled = composer.verb === "exclude"; // (location, exclude) is invalid
  return `
    <div class="rule-composer">
      <select class="rule-select" data-rule-new-verb aria-label="Rule verb">
        <option value="include"${composer.verb === "include" ? " selected" : ""}>Always include</option>
        <option value="exclude"${composer.verb === "exclude" ? " selected" : ""}>Don't include</option>
      </select>
      <select class="rule-select" data-rule-new-target aria-label="What to match">
        <option value="title"${composer.target === "title" ? " selected" : ""}>in the job title</option>
        <option value="location"${composer.target === "location" ? " selected" : ""}${locDisabled ? " disabled" : ""}>in the location</option>
      </select>
      <input type="text" class="settings-add-input rule-term-newinput" data-rule-new-term value="${esc(composer.term)}" placeholder="terms, comma-separated…" aria-label="Rule terms" />
      <button type="button" class="btn" data-action="rule-add">Add rule</button>
    </div>`;
}

/* The "Advanced — compiled from rules" disclosure. Two scopes: the title arrays
   live in Sourcing under the rules editor; the location allowlist in Scoring's
   Location subgroup. Reuses the companies.js collapse idiom (render-or-omit). */
const ADVANCED = {
  title: [
    { key: "title_keywords", label: "Include — titles containing" },
    { key: "title_exclude_keywords", label: "Exclude — drop titles containing" },
  ],
  location: [{ key: "location_allowlist", label: "Town allowlist (towns that pass)" }],
};

function advancedHtml(scope) {
  const open = expandedAdvanced.has(scope);
  return `
    <div class="advanced-details">
      <button type="button" class="collapse-head" data-action="toggle-advanced" data-scope="${scope}" aria-expanded="${open}">
        <span class="section-title">Advanced — compiled from rules</span>
        <svg class="collapse-chevron${open ? " open" : ""}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="9 18 15 12 9 6" /></svg>
      </button>
      ${
        open
          ? `<div class="collapse-body">
        <p class="settings-help">Compiled from the rules above. <strong>Rule</strong> chips are read-only — edit the rule to change them; you can still add one-off manual keywords here.</p>
        ${ADVANCED[scope]
          .map(
            (f) => `
        <div class="settings-field">
          <span class="field-label">${esc(f.label)}</span>
          ${tagsHtml("compiled", f.key, state.compiled[f.key])}
        </div>`
          )
          .join("")}
      </div>`
          : ""
      }
    </div>`;
}

function rulesSectionHtml() {
  return `
    <div class="section">
      <div class="section-head"><h2 class="section-title">Inclusion rules${helpHintHtml("inclusion-rules")}</h2></div>
      <p class="settings-help">Plain-English rules decide which jobs get pulled in. <strong>Always include</strong> / <strong>Don't include</strong> match the job <strong>title</strong> at ingestion (next refresh); a location rule adds towns to the allowlist used at scoring. They compile to the keyword lists in Advanced.</p>
      ${rulesError ? `<div class="rules-error" role="alert">${esc(rulesError.message)}</div>` : ""}
      <div class="rules-list">
        ${
          state.rules.length
            ? state.rules.map(ruleCardHtml).join("")
            : `<p class="settings-help rules-empty">No rules yet — add one below. Until then your current keywords show as manual entries under Advanced.</p>`
        }
      </div>
      ${ruleComposerHtml()}
      ${advancedHtml("title")}
    </div>`;
}

/* ---------- template ---------- */

function tabsHtml() {
  return `
    <div class="settings-tabbar">
      <nav class="settings-tabs" aria-label="Settings sections">
        ${TABS.map(
          (t) => `
          <button type="button" class="settings-tab${activeTab === t.id ? " active" : ""}" data-action="settings-tab" data-tab="${t.id}"${activeTab === t.id ? ` aria-current="page"` : ""}>${t.label}</button>`
        ).join("")}
      </nav>
    </div>`;
}

function template() {
  return `
    <div class="settings-view">
      <div class="settings-inner">
        <h1 class="settings-h1">Settings</h1>
        <p class="settings-help settings-intro">Title rules act at the next job-board refresh (new jobs only). Fit criteria act at scoring — new jobs automatically, existing jobs after a <strong>Rescore</strong>. Nothing here rewrites jobs already in your list except a rescore.</p>
        ${tabsHtml()}
        <div class="settings-tabpanel">${tabContent()}</div>
      </div>
    </div>`;
}

function tabContent() {
  return activeTab === "sourcing"
    ? sourcingTab()
    : activeTab === "scoring"
      ? scoringTab()
      : systemTab();
}

/* Tab switch is a partial update: toggle the active class on the existing tab
   buttons in place (so the 300ms surface fade fires — a full innerHTML rebuild
   makes fresh nodes and the transition never runs) and swap only the panel body.
   syncCriteriaFromDom() ran at the top of onClick, so leaving the Scoring tab
   keeps its staged edits in state. */
function switchTab(tab) {
  if (!tab || tab === activeTab) return;
  activeTab = tab;
  root.querySelectorAll(".settings-tab").forEach((b) => {
    const on = b.dataset.tab === tab;
    b.classList.toggle("active", on);
    // A11Y-08: this is a labelled <nav>, not an ARIA tablist (the old partial
    // pattern claimed tab roles but had no controls/tabpanel wiring, roving
    // tabindex or arrow-key nav). aria-current marks the active section.
    if (on) b.setAttribute("aria-current", "page");
    else b.removeAttribute("aria-current");
  });
  const panel = root.querySelector(".settings-tabpanel");
  if (panel) panel.innerHTML = tabContent();
  // Keep the scroll position on a tab switch (QA): don't jump to top, and focus
  // the active tab for keyboard nav without the browser scrolling it into view.
  const activeBtn = root.querySelector(".settings-tab.active");
  if (activeBtn) activeBtn.focus({ preventScroll: true });
  updateDirtyIndicator();
}

function sourcingTab() {
  return `
    ${rulesSectionHtml()}

    ${
      state.suggestions.length
        ? `<div class="section">
             <div class="section-head"><h2 class="section-title">Suggested exclusions</h2></div>
             <p class="settings-help">From your dismissals — accepting adds a manual exclusion keyword (visible under Advanced above).</p>
             ${state.suggestions.map(suggestionCard).join("")}
           </div>`
        : ""
    }

    <div class="section">
      <div class="section-head"><h2 class="section-title">Dismissal reasons</h2></div>
      <p class="settings-help">The reason dropdown shown when you dismiss a job.</p>
      ${tagsHtml("set", "dismiss_reasons", state.settings.dismiss_reasons)}
    </div>

    <div class="section">
      <div class="section-head"><h2 class="section-title">Contact sources</h2></div>
      <p class="settings-help">The how-you-met dropdown on contacts.</p>
      ${tagsHtml("set", "contact_sources", state.settings.contact_sources)}
    </div>`;
}

/* One location block. Two peer "modes" feed the same Tier 1 pass decision: a
   center+radius (7i) and the town allowlist (the editable escape hatch). The gate
   passes a hybrid/onsite job when its town is within the radius OR on the
   allowlist — radius augments, never replaces, the list. */
function locationGroupHtml(p) {
  return `
    <div class="settings-subgroup" data-subgroup="location">
      <span class="subgroup-title">Location</span>
      ${locationModeRadius(p)}
      ${locationModeAllowlist(p)}
    </div>`;
}

/* Center + commute radius (Phase 7i). The center resolves to coordinates through
   the bundled offline place table (api.geocode); the threshold is drive-time
   MINUTES — a measured per-town time where known, an offline estimate otherwise.
   A blank/cleared center turns it off (allowlist only). Saves with the rest of
   Tier 1 via the staged "Save fit criteria" bar — a criteria param, not an
   instant rule write. */
function locationModeRadius(p) {
  const r = p.location_radius;
  const on = !!(r && r.center);
  const label = on ? r.center.label || "" : "";
  const minutes = on && r.radius_minutes != null ? r.radius_minutes : 30;
  const resolved = on
    ? `<span class="radius-resolved" data-radius-resolved>✓ ${esc(r.center.label || "Center set")} · ${Number(r.center.lat).toFixed(3)}, ${Number(r.center.lng).toFixed(3)}</span>`
    : `<span class="radius-resolved radius-off" data-radius-resolved>Off — using the town allowlist only. Set a center to also include nearby towns.</span>`;
  return `
    <div class="settings-field location-radius">
      <span class="field-label">Commute radius${helpHintHtml("commute-radius")}</span>
      <p class="settings-help">A job whose town is within this many minutes' drive of the center passes Tier 1 even if it isn't an allowlisted town. Drive time is measured where known, estimated otherwise. Clear the center to use the allowlist only.</p>
      <div class="radius-row">
        <div class="radius-center">
          <input type="text" data-radius-center value="${esc(label)}" placeholder="Madison, WI" aria-label="Radius center (town, state)" />
          <button type="button" class="btn btn-ghost" data-action="radius-resolve">Set center</button>
          ${on ? `<button type="button" class="btn btn-ghost" data-action="radius-clear">Clear</button>` : ""}
        </div>
        <label class="radius-minutes">Within
          <input type="number" min="1" step="1" inputmode="numeric" data-radius-minutes value="${esc(String(minutes))}" aria-label="Commute radius in minutes" /> min
        </label>
      </div>
      ${resolved}
      ${fieldError("location_radius")}
    </div>`;
}

function locationModeAllowlist(p) {
  return `
    <p class="settings-help location-pointer">Town allowlist is authored in <strong>Inclusion rules</strong> (Sourcing tab) — add a "${"Always include … in the location"}" rule. The compiled towns are below.</p>
    ${advancedHtml("location")}
    <div class="settings-field">
      <span class="field-label">Remote roles I'll accept (allowed regions)</span>
      ${tagsHtml("crit", "remote_regions", p.remote_regions)}
      ${fieldError("remote_regions")}
    </div>
    <div class="settings-field">
      <span class="field-label">Per-company location overrides</span>
      ${mapEditorHtml("overrides", state.criteria._overrides, {
        keyLabel: "Company",
        valLabel: "Cities",
        valPlaceholder: "copenhagen, london",
        keyOptions: state.companies,
      })}
      ${fieldError("company_location_overrides")}
    </div>`;
}

/* "Description-based rules" surface (Phase 7i-semantic): the pending review
   queue (typed scoring_rule proposals) + the active learned-rules list. These
   act at the AI scoring layer, not as title keywords — so they live under
   Scoring, not nested under Job titles. Renders only when there's something to
   show, like Sourcing's "Suggested exclusions". */
function proposedRulesHtml() {
  if (!state.scoringProposals.length && !state.scoringRules.length) return "";
  return `
    <div class="section">
      <div class="section-head"><h2 class="section-title">Description-based rules${helpHintHtml("learned-rules")}</h2><span class="section-count">learned</span></div>
      <p class="settings-help">Rules proposed from job descriptions (via “Propose exclusion rule” on a job). They down-rank matching roles during AI scoring — not a title filter. New scores use them automatically; existing jobs need a rescore (System tab).</p>
      ${
        state.scoringProposals.length
          ? `<div class="subgroup-title">To review</div>
             ${state.scoringProposals.map(suggestionCard).join("")}`
          : ""
      }
      ${
        state.scoringRules.length
          ? `<div class="subgroup-title">Active rules</div>
             ${state.scoringRules.map(scoringRuleRow).join("")}`
          : ""
      }
    </div>`;
}

function scoringRuleRow(r) {
  return `
    <div class="rule-card">
      <div class="rule-card__head">
        <span class="rule-card__phrase">${esc(r.text)}</span>
        <button type="button" class="btn btn-ghost btn-danger" data-action="scoring-rule-remove" data-id="${esc(r.id)}" aria-label="Remove rule">Remove</button>
      </div>
      ${r.job_title ? `<p class="settings-help rule-origin">from “${esc(r.job_title)}”</p>` : ""}
    </div>`;
}

function scoringTab() {
  const p = state.criteria.tier1_params;
  const dirty = criteriaDirtyFromState();
  return `
    ${criteriaError ? `<div class="criteria-error" role="alert">${esc(criteriaError.message)}</div>` : ""}
    ${
      vocabDocError()
        ? `<div class="criteria-error" role="alert">Your <code>fit_criteria.md</code> couldn't be read, so scoring is failing and the lists below are showing built-in defaults: ${esc(vocabDocError())}</div>`
        : ""
    }

    <div class="section">
      <div class="section-head"><h2 class="section-title">Hard rules — auto-rejected if they fail${helpHintHtml("tier1-tier2")}</h2></div>
      <p class="settings-help">Deterministic gates run before AI scoring. Edits save to <code>fit_criteria.md</code> together with the ranked criteria below, and reach existing jobs only after a rescore.</p>

      <div class="settings-subgroup" data-subgroup="comp">
        <span class="subgroup-title">Compensation${helpHintHtml("comp-floor-target")}</span>
        <p class="settings-help"><strong>Floor</strong> = hard cutoff (jobs paying below it are auto-rejected). <strong>Target</strong> = goal (jobs between floor and target pass but get a "below target" flag). Jobs with no listed pay are never rejected on comp.</p>
        <div class="control-row">
          <div class="field">
            <span class="field-label">Comp floor ($)</span>
            <input type="number" min="0" step="1000" inputmode="numeric" data-param="comp_floor" aria-label="Comp floor ($)" value="${esc(p.comp_floor)}" />
            ${fieldError("comp_floor")}
          </div>
          <div class="field">
            <span class="field-label">Comp target ($)</span>
            <input type="number" min="0" step="1000" inputmode="numeric" data-param="comp_target" aria-label="Comp target ($)" value="${esc(p.comp_target)}" />
            ${fieldError("comp_target")}
          </div>
        </div>
      </div>

      ${locationGroupHtml(p)}

      <div class="settings-subgroup" data-subgroup="sector">
        <span class="subgroup-title">Sector rejection</span>
        <div class="settings-field">
          <span class="field-label">Industries to avoid (rejects the whole company)</span>
          ${tagsHtml("crit", "excluded_sectors", p.excluded_sectors)}
          ${fieldError("excluded_sectors")}
        </div>
      </div>

      <div class="settings-subgroup" data-subgroup="title">
        <span class="subgroup-title">Title band${helpHintHtml("title-bands")}</span>
        <div class="settings-field">
          <span class="field-label">Target title bands (pass)</span>
          ${tagsHtml("crit", "target_title_bands", p.target_title_bands, { options: levelBands() })}
          ${fieldError("target_title_bands")}
        </div>
        <div class="settings-field">
          <span class="field-label">Title-seniority flags (flagged, not rejected)</span>
          ${mapEditorHtml("bands", state.criteria._bands, {
            keyLabel: "Band",
            valLabel: "Flag",
            valOptions: flagValues(),
            keyOptions: levelBands(),
          })}
          ${fieldError("flag_title_bands")}
        </div>
      </div>
    </div>

    ${proposedRulesHtml()}

    <div class="section">
      <div class="section-head"><h2 class="section-title">What I'm looking for — ranked${helpHintHtml("tier2-weight")}</h2></div>
      <p class="settings-help">In stack-rank order. Each item carries an importance weight (× multiplier, 1 = normal) the AI score reads as emphasis on top of its rank. Markdown (<code>**bold**</code>) is allowed.</p>
      <div class="tier2-list">
        ${state.criteria.tier2_criteria
          .map(
            (item, i) => `
          <div class="tier2-row" data-index="${i}">
            <span class="tier2-num">${i + 1}</span>
            <input type="text" data-t2 value="${esc(item.text)}" aria-label="Criterion ${i + 1}" />
            ${
              item.craft
                ? `<span class="tier2-tag" title="The craft-versus-convert axis. The scorer may never leave this one unevidenced, and the tension label on every job is derived from it. Removing this row removes the axis.">craft axis</span>`
                : ""
            }${
              item.bonus_only
                ? `<span class="tier2-tag" title="Bonus only: this criterion never scores negative.">bonus only</span>`
                : ""
            }
            <label class="tier2-weight" title="Importance weight (1 = normal, 2 = counts ~twice)">
              <span class="tier2-weight-mul">×</span>
              <input type="number" data-t2-weight min="0.25" max="4" step="0.25" value="${item.weight}" aria-label="Weight for criterion ${i + 1}" />
            </label>
            <span class="tier2-controls">
              <button type="button" class="btn btn-ghost" data-action="t2-up" data-index="${i}" ${i === 0 ? "disabled" : ""} aria-label="Move up">↑</button>
              <button type="button" class="btn btn-ghost" data-action="t2-down" data-index="${i}" ${i === state.criteria.tier2_criteria.length - 1 ? "disabled" : ""} aria-label="Move down">↓</button>
              <button type="button" class="btn btn-ghost btn-danger" data-action="t2-remove" data-index="${i}" aria-label="Remove">×</button>
            </span>
          </div>`
          )
          .join("")}
      </div>
      <button type="button" class="btn btn-ghost" data-action="t2-add">+ Add criterion</button>
    </div>

    ${synthesisSection()}

    <div class="settings-savebar scoring-savebar${dirty ? " is-dirty" : ""}">
      <button type="button" class="btn btn-accent" data-action="save-criteria"${dirty ? "" : " disabled"}>Save fit criteria</button>
      <span class="dirty-flag"${dirty ? "" : " hidden"}>Unsaved changes</span>
      <span class="scoring-savebar-sep" aria-hidden="true"></span>
      ${rescoreControlsHtml({ requireSaved: true })}
    </div>`;
}

/* Roadmap synthesis: turn the wizard's raw wish-list + matrix words into the
   reflection prose the scorer reads. Keyed = one Draft-with-AI call; keyless =
   copy the same prompt into any chat AI and paste the JSON reply back. Both
   park the same draft, previewed here — fit_criteria.md is never written
   without an explicit Apply. */
function synthesisSection() {
  const s = state.synthesis;
  if (!s) return "";
  const keyed = !!state.apiKey?.configured;
  let body;
  if (!s.available && !s.proposal) {
    body = `<p class="settings-help">Nothing captured yet — rank a wish list or fill the
      fulfillment matrix in the <a href="#/welcome">setup walkthrough</a> first.</p>`;
  } else {
    const pasteCard = `
      <div class="synthesis-paste">
        <button type="button" class="btn btn-ghost" data-action="synthesis-copy-prompt">Copy prompt for any chat AI</button>
        ${state.synthesisPrompt ? `<textarea class="settings-voice-guide" data-synthesis-prompt rows="6" readonly aria-label="Synthesis prompt">${esc(state.synthesisPrompt)}</textarea>` : ""}
        <textarea class="settings-voice-guide" data-synthesis-reply rows="4" spellcheck="false" placeholder="Paste the model's JSON reply here" aria-label="Pasted synthesis reply">${esc(state.synthesisReply)}</textarea>
        ${state.synthesisReplyError ? `<p class="criteria-error" role="alert">${esc(state.synthesisReplyError)}</p>` : ""}
        <button type="button" class="btn" data-action="synthesis-check">Check reply</button>
      </div>`;
    body = `
      ${
        keyed
          ? `<button type="button" class="btn btn-accent" data-action="synthesis-draft"${state.synthesisBusy ? " disabled" : ""}>${state.synthesisBusy ? "Drafting…" : "Draft with AI"}</button>
             <details class="synthesis-keyless"><summary>No key handy? Use any chat AI</summary>${pasteCard}</details>`
          : pasteCard
      }
      ${s.proposal ? synthesisPreviewHtml(s.proposal) : ""}`;
  }
  return `
    <div class="section">
      <div class="section-head"><h2 class="section-title">Synthesize from your own words</h2></div>
      <p class="settings-help">The setup walkthrough kept your ranked wish list and
        fulfillment-matrix answers verbatim. This turns them into the reflection
        sections the scorer reads — quadrant signal verbs and the central tension —
        reviewed here before anything lands in <code>fit_criteria.md</code>.</p>
      ${body}
    </div>`;
}

const SYNTHESIS_QUADRANT_LABELS = {
  energizing_strength: "Keep doing this",
  energizing_growth: "Grow into this",
  draining_strength: "The trap",
  draining_growth: "Leave these",
};

function synthesisPreviewHtml(proposal) {
  const d = proposal.data;
  const current = state.criteria?.tier2_criteria || [];
  const quadrant = (k) => {
    const cell = d.quadrants[k];
    return `
      <div class="synthesis-quadrant">
        <span class="field-label">${SYNTHESIS_QUADRANT_LABELS[k]}</span>
        <ul>${cell.activities.map((a) => `<li>${esc(a)}</li>`).join("")}</ul>
        ${cell.signal_verbs.length ? `<p class="settings-help">Signal verbs: ${esc(cell.signal_verbs.join(", "))}</p>` : ""}
      </div>`;
  };
  const rubric = d.central_tension.rubric;
  const refinements = d.tier2_refinements || [];
  return `
    <div class="synthesis-preview">
      <p class="settings-help">Drafted ${proposal.source === "paste" ? "from your pasted reply" : `by ${esc(proposal.model || "AI")}`} —
        nothing changes until you apply. Re-applying later replaces only the generated section.</p>
      <div class="synthesis-quadrants">
        ${["energizing_strength", "energizing_growth", "draining_strength", "draining_growth"].map(quadrant).join("")}
      </div>
      <blockquote class="synthesis-tension">${esc(d.central_tension.one_liner)}</blockquote>
      <ul class="synthesis-rubric">
        ${[2, 1, 0, -1, -2].map((v) => `<li><strong>${v > 0 ? `+${v}` : v}</strong> — ${esc(rubric[String(v)])}</li>`).join("")}
      </ul>
      ${d.away_toward.away.length ? `<p class="settings-help"><strong>Away from:</strong> ${esc(d.away_toward.away.join("; "))}</p>` : ""}
      ${d.away_toward.toward.length ? `<p class="settings-help"><strong>Toward:</strong> ${esc(d.away_toward.toward.join("; "))}</p>` : ""}
      ${
        refinements.length
          ? `<label class="form-check"><input type="checkbox" data-action="synthesis-toggle-tier2"${state.synthesisApplyTier2 ? " checked" : ""}/> Also update ${refinements.length} ranked item${refinements.length === 1 ? "" : "s"} (old → new below)</label>
             <ul class="synthesis-refinements">
               ${refinements.map((r) => `<li><span class="settings-help">${r.index}. ${esc(current[r.index - 1]?.text || "?")} →</span> ${esc(r.text)} <span class="settings-help">(×${r.weight}${r.craft ? ", craft axis" : ""}${r.bonus_only ? ", bonus only" : ""})</span></li>`).join("")}
             </ul>`
          : ""
      }
      <div class="control-row">
        <button type="button" class="btn btn-accent" data-action="synthesis-apply">Apply to fit_criteria.md</button>
        <button type="button" class="btn btn-ghost" data-action="synthesis-discard">Discard draft</button>
      </div>
    </div>`;
}

async function synthesisDraft() {
  if (state.synthesisBusy) return;
  state.synthesisBusy = true;
  paint();
  try {
    state.synthesis = await api.proposeSynthesis();
    toast("Draft ready — review it below");
  } catch (error) {
    toast(error.detail || error.message, { error: true });
  }
  state.synthesisBusy = false;
  paint();
}

async function synthesisCopyPrompt() {
  let prompt;
  try {
    prompt = (await api.getSynthesisPrompt()).prompt;
  } catch (error) {
    toast(error.detail || error.message, { error: true });
    return;
  }
  try {
    await navigator.clipboard.writeText(prompt);
    toast("Prompt copied — paste it into any chat AI, then bring the JSON reply back");
  } catch {
    // Clipboard API is blocked on non-HTTPS LAN origins; show the prompt for a
    // manual copy instead (the composeModal fallback pattern).
    state.synthesisPrompt = prompt;
    paint();
    const el = root.querySelector("[data-synthesis-prompt]");
    if (el) {
      el.focus();
      el.select();
    }
    toast(`Clipboard blocked, press ${isMac ? "⌘C" : "Ctrl+C"} to copy the prompt`);
  }
}

async function synthesisCheck() {
  const reply = state.synthesisReply.trim();
  if (!reply) return;
  try {
    state.synthesis = await api.submitSynthesisReply(reply);
    state.synthesisReplyError = null;
    state.synthesisReply = "";
    toast("Reply checks out — review the draft below");
  } catch (error) {
    state.synthesisReplyError = error.detail || error.message;
  }
  paint();
}

async function synthesisApply() {
  try {
    const updated = await api.applySynthesis({ apply_tier2: state.synthesisApplyTier2 });
    setCriteria(updated); // same {tier1_params, tier2_criteria} shape as save-criteria
    state.synthesis = { ...state.synthesis, proposal: null };
    state.synthesisApplyTier2 = false;
    toast("Applied to fit_criteria.md — existing jobs need a rescore");
  } catch (error) {
    toast(error.detail || error.message, { error: true });
  }
  paint();
}

async function synthesisDiscard() {
  // The draft may have come from a paid "Draft with AI" call; regenerating one
  // costs another. Confirm before dropping it (the raw wish-list/matrix words
  // survive, so it's re-creatable — but a re-draft is a re-spend). Matches the
  // compose modal's discard confirm.
  const ok = await confirmModal({
    title: "Discard this draft?",
    message: "The draft goes away. Drafting a new one with AI is a fresh paid call.",
    confirmLabel: "Discard",
  });
  if (!ok) return;
  try {
    state.synthesis = await api.discardSynthesis();
    state.synthesisApplyTier2 = false;
  } catch (error) {
    toast(error.detail || error.message, { error: true });
  }
  paint();
}

/* The Anthropic API key section (Phase 3). AI features degrade gracefully with
   no key; this is where a user turns them on without hand-editing a .env. The
   key is never rendered — only the server's masked status is shown. */
function apiKeySection() {
  const k = state.apiKey || { configured: false, source: null, editable: true };
  const shadowed = k.source === "environment"; // an env var / cwd .env wins over our .env
  const declined = state.apiKeyDeclined === true; // explicit "I don't want a key"
  const inputsOff = shadowed || declined; // declining disables the field + Save
  // The decline option only makes sense with no key; a configured key hides it.
  const declineRow = k.configured
    ? ""
    : `
      <label class="form-check">
        <input type="checkbox" data-action="toggle-decline-key" ${declined ? "checked" : ""} />
        I don't want to use an API key
      </label>
      ${
        declined
          ? `<p class="field-error">AI features stay off without a key: postings aren't scored or explained, and there are no AI-drafted messages, cover-letter tailoring, or job-URL prefill. Everything else — pulling, filtering, tracking — works as normal. Uncheck this to add a key.</p>`
          : ""
      }`;
  const test = state.apiKeyTest;
  let testLine = "";
  if (test === "pending") {
    testLine = `<p class="settings-help">Testing…</p>`;
  } else if (test && test.ok) {
    testLine = `<p class="settings-help">The key works.</p>`;
  } else if (test && test.error) {
    testLine = `<p class="field-error">${esc(test.error)}</p>`;
  }
  const status = k.configured
    ? `<strong>Configured</strong> <code>${esc(k.masked || "")}</code>`
    : `<strong>Not configured</strong> — AI features (scoring, compose, tailoring) are off`;
  return `
    <div class="section">
      <div class="section-head"><h2 class="section-title">Anthropic API key</h2></div>
      <p class="settings-help">Bring your own key to turn on the AI features. It is stored in <code>.env</code> in your data directory on this machine and sent only to api.anthropic.com — never anywhere else.</p>
      <p class="settings-help">Status: ${status}.</p>
      ${
        shadowed
          ? `<p class="field-error">This key comes from your environment (an exported variable or a project <code>.env</code>), which takes precedence. Saving here won't override it — unset it in your shell to manage the key from this screen.</p>`
          : ""
      }
      <div class="settings-savebar">
        <input type="password" class="settings-add-input" data-api-key-input placeholder="sk-ant-…" autocomplete="off" aria-label="Anthropic API key" ${inputsOff ? "disabled" : ""} />
        <button type="button" class="btn btn-accent" data-action="save-api-key" ${inputsOff ? "disabled" : ""}>Save</button>
        <button type="button" class="btn btn-ghost" data-action="test-api-key" ${k.configured && !declined ? "" : "disabled"}>Test</button>
        ${k.configured && !shadowed ? `<button type="button" class="btn btn-ghost btn-danger" data-action="remove-api-key">Remove</button>` : ""}
      </div>
      ${declineRow}
      ${testLine}
      <p class="settings-help"><a href="https://platform.claude.com/settings/keys" target="_blank" rel="noopener">Get an API key →</a></p>
    </div>`;
}

/* Persona (Phase 3): who the AI prompts are written for. display_name is the one
   part of the criteria doc that names a person; blank means "the candidate". */
function personaSection() {
  const p = state.persona || { display_name: null, domain_label: "" };
  const name = p.display_name || "";
  const label = p.domain_label || "";
  return `
    <div class="section">
      <div class="section-head"><h2 class="section-title">Persona</h2></div>
      <p class="settings-help">Who the AI writes as. Your name is sent to the model on every scoring, compose, and tailoring call. Leave it blank to stay anonymous — prompts then say "the candidate".</p>
      <div class="control-row">
        <div class="field">
          <label class="field-label" for="persona-name">Display name</label>
          <input type="text" id="persona-name" data-persona-name value="${esc(name)}" placeholder="e.g. Sam Lee (or leave blank)" aria-label="Display name" />
        </div>
        <div class="field">
          <label class="field-label" for="persona-label">Role you're searching for</label>
          <input type="text" id="persona-label" data-persona-label value="${esc(label)}" placeholder="e.g. engineering-management" aria-label="Role you are searching for" />
        </div>
      </div>
      <div class="settings-savebar">
        <button type="button" class="btn btn-accent" data-action="save-persona">Save persona</button>
      </div>
    </div>`;
}

/* Voice guide (Phase 3): the prose the compose/tailor/refine prompts carry
   verbatim. Now editable in place — the live copy lives in the data directory. */
function voiceGuideSection() {
  return `
    <div class="section">
      <div class="section-head"><h2 class="section-title">Voice guide</h2></div>
      <p class="settings-help">Tone, vocabulary, and phrasing rules the AI follows when it drafts and tailors on your behalf. Edited here, saved to your data directory. Leave it empty to fall back to the plain base framing.</p>
      <textarea class="settings-voice-guide" data-voice-guide rows="14" spellcheck="false" aria-label="Voice guide">${esc(state.voiceGuide)}</textarea>
      <div class="settings-savebar">
        <button type="button" class="btn btn-accent" data-action="save-voice-guide">Save voice guide</button>
      </div>
    </div>`;
}

// Scoring-tab rescore hints, shared by the initial render (rescoreControlsHtml)
// and the in-place dirty update (updateDirtyIndicator) so the two never drift.
const RESCORE_HINT_DIRTY = "Save first, then rescore to apply your changes to existing jobs.";
const RESCORE_HINT_SAVED = "Applies your saved criteria to the jobs already on your board.";

/* The Rescore button + its status/progress line. Shared by the System tab's
   Rescore section and the Scoring tab's sticky save bar, so the "rescore to
   apply to existing jobs" prompt sits next to the action instead of a tab away.
   On the Scoring tab (requireSaved) it stays disabled while criteria edits are
   unsaved — rescore applies what is on disk, so Save comes first. Live edits
   keep it in sync via updateDirtyIndicator (this only paints the initial state). */
function rescoreControlsHtml({ requireSaved = false } = {}) {
  const s = state.status || {};
  const prog = state.rescoring && s.scoring_progress ? s.scoring_progress : null;
  const blockedByDirty = requireSaved && criteriaDirtyFromState();
  const disabled = state.rescoring || s.running || blockedByDirty;
  // Accent when this is the live call to action: always on the System tab, and
  // on the Scoring tab once criteria are saved (Save stays the accent until then).
  const cls = !requireSaved || !blockedByDirty ? "btn btn-accent" : "btn";
  const tail = prog
    ? `<span class="settings-help" data-rescore-progress>${prog.total ? `Rescoring ${prog.done}/${prog.total}` : "Rescoring…"}${prog.errors ? ` · ${prog.errors} errors` : ""}</span>`
    : requireSaved
      ? `<span class="settings-help">${blockedByDirty ? RESCORE_HINT_DIRTY : RESCORE_HINT_SAVED}</span>`
      : `<span class="settings-help">Last rescore: <span title="${esc(fmtStamp(s.last_rescore))}">${esc(fmtAgo(s.last_rescore))}</span> · last refresh: <span title="${esc(fmtStamp(s.last_refresh))}">${esc(fmtAgo(s.last_refresh))}</span></span>`;
  return `
    <button type="button" class="${cls}" data-action="rescore"${disabled ? " disabled" : ""}>${state.rescoring ? "Rescoring…" : "Rescore now"}</button>
    ${tail}`;
}

function systemTab() {
  const s = state.status || {};
  const rep = s.scoring_report;
  const byModel = s.usage?.by_model || {};
  const models = Object.entries(byModel);
  const spendTotal = models.reduce((sum, [, m]) => sum + (m.cost || 0), 0);
  const spendCalls = models.reduce((sum, [, m]) => sum + (m.calls || 0), 0);
  const MODEL_SPEND_LABELS = {
    "claude-haiku-4-5": "Haiku (scoring)",
    "claude-sonnet-4-6": "Sonnet (compose/tailor)",
    "claude-sonnet-5": "Sonnet (compose/tailor)",
  };
  return `
    ${apiKeySection()}
    ${personaSection()}
    ${voiceGuideSection()}
    <div class="section">
      <div class="section-head"><h2 class="section-title">Rescore</h2></div>
      <p class="settings-help">Re-score every active job against the current criteria. Runs in the background.</p>
      <div class="settings-savebar">
        ${rescoreControlsHtml()}
      </div>
      ${
        rep && rep.skipped
          ? `<p class="field-error">Last run skipped — ${esc(rep.skipped)}</p>`
          : rep
            ? `<p class="settings-help">Last result: ${rep.scored} scored, ${rep.tier1_failed} gated${rep.rate_limited ? `, ${rep.rate_limited} rate-limited` : ""}${rep.errors ? `, ${rep.errors} errors` : ""}${typeof rep.cost === "number" ? ` · $${rep.cost.toFixed(2)}` : ""}</p>`
            : ""
      }
      ${
        models.length
          ? `<p class="settings-help">Model spend: <strong>$${spendTotal.toFixed(2)}</strong> over ${spendCalls} call${spendCalls === 1 ? "" : "s"} — ${models
              .map(([id, m]) => `${esc(MODEL_SPEND_LABELS[id] || id)} $${(m.cost || 0).toFixed(2)}`)
              .join(", ")}.</p>`
          : ""
      }
      <p class="settings-help"><a href="https://platform.claude.com/usage" target="_blank" rel="noopener">Anthropic Console →</a></p>
    </div>
    <div class="section">
      <div class="section-head"><h2 class="section-title">Appearance</h2></div>
      <p class="settings-help">Theme for this browser (per device). System follows the OS setting.</p>
      <div class="theme-seg" role="group" aria-label="Theme">
        ${["system", "light", "dark"]
          .map(
            (v) =>
              `<button type="button" data-action="set-theme" data-value="${v}" aria-pressed="${getThemePref() === v}">${v[0].toUpperCase() + v.slice(1)}</button>`
          )
          .join("")}
      </div>
    </div>
    <div class="section">
      <div class="section-head"><h2 class="section-title">Notifications</h2></div>
      <p class="settings-help">Alerts when background work finishes — board refresh, rescore, tailoring.</p>
      <label class="form-check"><input type="checkbox" data-action="toggle-notify-sound"${soundEnabled() ? " checked" : ""}/> Play a sound in this browser (per device)</label>
      ${isMac ? `<label class="form-check"><input type="checkbox" data-action="toggle-notify-popups"${state.notifyPopups ? " checked" : ""}/> Show desktop notifications for refresh &amp; rescore (works with the browser closed)</label>` : ""}
    </div>`;
}

/* ---------- paint ---------- */

function paint() {
  const scroller = root.querySelector(".settings-view");
  const top = scroller ? scroller.scrollTop : 0;

  // Capture focus identity before the innerHTML swap. An explicit pendingFocus
  // (set by an action that creates/moves a control) wins; otherwise preserve
  // wherever the caret was so a structural repaint never steals focus.
  let focus = pendingFocus;
  let selStart = null;
  let selEnd = null;
  if (!focus) {
    const sel = focusSelector(document.activeElement);
    if (sel) {
      focus = { sel };
      try {
        selStart = document.activeElement.selectionStart;
        selEnd = document.activeElement.selectionEnd;
      } catch {
        /* number inputs throw on selectionStart — leave the caret unset */
      }
    }
  }

  root.innerHTML = template();
  justAddedChip = null; // one-shot: only the just-added chip animates in
  const next = root.querySelector(".settings-view");
  if (next) next.scrollTop = top;

  if (focus) {
    const el = root.querySelector(focus.sel);
    if (el) {
      el.focus();
      const s = focus.start != null ? focus.start : selStart;
      const e = focus.end != null ? focus.end : selEnd;
      if (s != null) {
        try {
          el.setSelectionRange(s, e);
        } catch {
          /* not a text input */
        }
      }
    }
  }
  pendingFocus = null;

  updateDirtyIndicator();
  setStats([
    { value: fmtAgo(state.status?.last_refresh), label: "Last refresh", title: fmtStamp(state.status?.last_refresh) },
    { value: fmtAgo(state.status?.last_rescore), label: "Last rescore", title: fmtStamp(state.status?.last_rescore) },
  ]);
}

/* Build a re-findable selector from an element's stable data-* attributes.
   Full repaints destroy node refs, so focus is restored by identity, not ref. */
function focusSelector(el) {
  if (!el || !root || !root.contains(el)) return null;
  if (el.hasAttribute("data-add")) {
    return `.settings-add-input[data-kind="${el.dataset.kind}"][data-key="${el.dataset.key}"]`;
  }
  if (el.hasAttribute("data-rule-new-verb")) return "[data-rule-new-verb]";
  if (el.hasAttribute("data-rule-new-target")) return "[data-rule-new-target]";
  if (el.hasAttribute("data-rule-new-term")) return "[data-rule-new-term]";
  if (el.hasAttribute("data-rule-term-input")) {
    const card = el.closest(".rule-card");
    return card ? `.rule-card[data-rule-id="${card.dataset.ruleId}"] [data-rule-term-input]` : null;
  }
  if (el.dataset.action === "set-theme") {
    return `[data-action="set-theme"][data-value="${el.dataset.value}"]`;
  }
  if (el.hasAttribute("data-radius-center")) return "[data-radius-center]";
  if (el.hasAttribute("data-radius-minutes")) return "[data-radius-minutes]";
  if (el.dataset.param) return `[data-param="${el.dataset.param}"]`;
  if (el.hasAttribute("data-t2")) {
    const row = el.closest(".tier2-row");
    return row ? `.tier2-row[data-index="${row.dataset.index}"] [data-t2]` : null;
  }
  if (el.hasAttribute("data-t2-weight")) {
    const row = el.closest(".tier2-row");
    return row ? `.tier2-row[data-index="${row.dataset.index}"] [data-t2-weight]` : null;
  }
  if (el.hasAttribute("data-mapkey") || el.hasAttribute("data-mapval")) {
    const row = el.closest(".map-row");
    if (!row) return null;
    const which = el.hasAttribute("data-mapkey") ? "[data-mapkey]" : "[data-mapval]";
    return `.map-row[data-map="${row.dataset.map}"][data-index="${row.dataset.index}"] ${which}`;
  }
  return null;
}

/* Reflect the criteria dirty state on the sticky Save bar in place (no repaint —
   called on every keystroke in a criteria field as well as after each paint). */
function updateDirtyIndicator() {
  if (!root || !state.criteria) return;
  const bar = root.querySelector(".scoring-savebar");
  if (!bar) return; // only the Scoring tab has the bar
  syncCriteriaFromDom(); // safe here: the freshly-painted DOM equals state
  const dirty = criteriaDirtyFromState();
  bar.classList.toggle("is-dirty", dirty);
  const btn = bar.querySelector('[data-action="save-criteria"]');
  if (btn) btn.disabled = !dirty;
  const flag = bar.querySelector(".dirty-flag");
  if (flag) flag.hidden = !dirty;
  // The co-located Rescore control reflects the same dirty state: rescore applies
  // what is on disk, so unsaved edits must disable it and swap in the "save first"
  // hint. Skipped mid-rescore — the button label and progress line own it then.
  const rescoreBtn = bar.querySelector('[data-action="rescore"]');
  if (rescoreBtn && !state.rescoring) {
    rescoreBtn.disabled = dirty || !!(state.status && state.status.running);
    rescoreBtn.classList.toggle("btn-accent", !dirty);
    const hint = bar.querySelector(".settings-help:not([data-rescore-progress])");
    if (hint) hint.textContent = dirty ? RESCORE_HINT_DIRTY : RESCORE_HINT_SAVED;
  }
}

/* ---------- criteria DOM <-> state ---------- */

function parseIntOrRaw(v) {
  const s = String(v).trim();
  if (s === "") return ""; // let the server reject empties with a clear message
  const n = Number(s);
  return Number.isInteger(n) ? n : s;
}

/* Tier 2 importance multiplier (Phase 8): clamp to the 0.25–4.0 band the server
   (Tier2Item) enforces so the payload is always valid; empty/invalid ⇒ 1.0
   (normal), which renders with no `[w:]` suffix and reads as not-dirty. */
function readWeight(v) {
  const n = Number(String(v).trim());
  if (!Number.isFinite(n) || n <= 0) return 1;
  return Math.min(4, Math.max(0.25, n));
}

function syncCriteriaFromDom() {
  if (!state.criteria || !root) return;
  const p = state.criteria.tier1_params;
  const cf = root.querySelector('[data-param="comp_floor"]');
  const ct = root.querySelector('[data-param="comp_target"]');
  if (cf) p.comp_floor = parseIntOrRaw(cf.value);
  if (ct) p.comp_target = parseIntOrRaw(ct.value);
  state.criteria._overrides = readMapRows("overrides");
  state.criteria._bands = readMapRows("bands");
  if (root.querySelector("[data-t2]")) {
    state.criteria.tier2_criteria = [...root.querySelectorAll(".tier2-row")].map((row) => {
      const text = row.querySelector("[data-t2]");
      const weight = row.querySelector("[data-t2-weight]");
      /* craft/bonus_only have no input to read: carry them from the item this
         row was painted from, or saving would silently drop the markers and
         move the craft axis. Reorder splices the state array, so they travel
         with their criterion. */
      const prev = state.criteria.tier2_criteria[Number(row.dataset.index)] || {};
      return {
        text: text ? text.value : "",
        weight: readWeight(weight ? weight.value : ""),
        craft: !!prev.craft,
        bonus_only: !!prev.bonus_only,
      };
    });
  }
}

function readMapRows(map) {
  if (!root.querySelector(`[data-map-container="${map}"]`)) {
    return map === "overrides" ? state.criteria._overrides : state.criteria._bands;
  }
  return [...root.querySelectorAll(`.map-row[data-map="${map}"]`)].map((r) => ({
    key: r.querySelector("[data-mapkey]").value,
    val: r.querySelector("[data-mapval]").value,
  }));
}

function rowsToMap(rows, asList) {
  const out = {};
  for (const r of rows) {
    const key = r.key.trim();
    if (!key) continue;
    out[key] = asList
      ? r.val.split(",").map((s) => s.trim()).filter(Boolean)
      : r.val.trim();
  }
  return out;
}

/* ---------- actions ---------- */

function tagArray(kind, key) {
  return kind === "set" ? state.settings[key] : state.criteria.tier1_params[key];
}

async function saveSetting(key) {
  try {
    await api.putSetting(key, state.settings[key]);
  } catch (error) {
    toast(error.detail || error.message, { error: true });
    await reloadSettings();
  }
}

function itemNoun(key) {
  return ITEM_NOUNS[key] || "value";
}

function indefinite(noun) {
  return /^[aeiou]/i.test(noun) ? "an" : "a";
}

function addTag(kind, key, value) {
  const v = String(value).trim();
  if (!v) {
    const n = itemNoun(key);
    toast(`Please add ${indefinite(n)} ${n}`, { error: true });
    return;
  }
  const arr = tagArray(kind, key);
  if (arr.includes(v)) return;
  arr.push(v);
  justAddedChip = `${kind}|${key}|${v}`; // fade the new chip in on this paint (one-shot)
  pendingFocus = { sel: `.settings-add-input[data-kind="${kind}"][data-key="${key}"]` };
  paint();
  if (kind === "set") {
    saveSetting(key);
    toast("Saved");
  }
}

function removeTag(kind, key, value) {
  const arr = tagArray(kind, key);
  const i = arr.indexOf(value);
  if (i === -1) return;
  arr.splice(i, 1);
  pendingFocus = { sel: `.settings-add-input[data-kind="${kind}"][data-key="${key}"]` };
  paint();
  if (kind === "set") saveSetting(key);
}

/* Fade the chip out before the structural repaint removes it (300ms matches the
   app's pill/surface fades). Falls back to an immediate remove if the node is gone. */
function removeTagAnimated(kind, key, value, chip) {
  if (!chip) {
    removeTag(kind, key, value);
    return;
  }
  chip.classList.add("settings-tag--exit");
  setTimeout(() => removeTag(kind, key, value), 300);
}

/* Dispatch an accept/ignore by suggestion type (shortlist 7). */
function onSuggestion(type, key, verb) {
  if (type === "scoring_rule") return onScoringProposal(key, verb);
  return onTitleExclude(key, verb);
}

async function onTitleExclude(keyword, verb) {
  try {
    await api.actOnSuggestion(keyword, verb);
  } catch (error) {
    toast(error.detail || error.message, { error: true });
    return;
  }
  state.suggestions = state.suggestions.filter((s) => s.keyword !== keyword);
  // Accept writes title_exclude_keywords directly; it surfaces as a manual chip
  // in the compiled view, so re-pull the rules to reflect it.
  if (verb === "accept") await reloadRules();
  else paint();
  toast(verb === "accept" ? "Will exclude at the next refresh" : "Suggestion ignored");
}

/* A description-based learned rule. Accept promotes it to the active scoring
   rules (injected into the AI scorer); ignore drops it. The endpoint returns
   both updated lists, so no extra round-trip. NOT reloadRules — these are not
   inclusion rules and never touch the title arrays. */
async function onScoringProposal(id, verb) {
  let res;
  try {
    res = await api.actOnScoringProposal(id, verb);
  } catch (error) {
    toast(error.detail || error.message, { error: true });
    return;
  }
  state.scoringProposals = (res.proposals || []).map((s) => ({ ...s, type: "scoring_rule" }));
  state.scoringRules = res.rules || [];
  paint();
  toast(verb === "accept" ? "Rule added — rescore to apply" : "Proposal ignored");
}

async function removeScoringRule(id) {
  try {
    const res = await api.removeScoringRule(id);
    state.scoringRules = res.rules || [];
  } catch (error) {
    toast(error.detail || error.message, { error: true });
    return;
  }
  paint();
  toast("Rule removed — rescore to apply");
}

/* ---------- inclusion-rules actions (decision C) ---------- */

const newRuleId = () =>
  globalThis.crypto && crypto.randomUUID ? crypto.randomUUID() : `r-${performance.now()}`;

function manualPayload() {
  const out = {};
  for (const arr of COMPILED_ARRAYS) {
    out[arr] = (state.compiled[arr] || [])
      .filter((e) => e.source === "manual")
      .map((e) => e.value);
  }
  return out;
}

function parseRulesError(detail) {
  const text = String(detail || "");
  if (/location rule cannot be 'exclude'|location.*exclude/i.test(text)) {
    return "A location rule can't be an exclusion — use “Always include”.";
  }
  return text || "Could not save inclusion rules.";
}

/* Persist rules + manual extras (one atomic backend call). Like Sourcing's chip
   auto-save — no staged dirty model. Errors surface inline (persistent), and the
   optimistic edit is rolled back from the server. */
async function saveRules() {
  rulesError = null;
  try {
    const res = await api.putInclusionRules({ rules: state.rules, manual: manualPayload() });
    applyRulesResult(res);
    paint();
  } catch (error) {
    rulesError = { message: error.status === 422 ? parseRulesError(error.detail) : error.detail || error.message };
    await reloadRules(); // discard the optimistic change; paints with rulesError shown
  }
}

async function reloadRules() {
  try {
    applyRulesResult(await api.getInclusionRules());
  } catch {
    /* keep current state; the caller already set rulesError if relevant */
  }
  paint();
}

function addCompiledManual(key, value) {
  const v = String(value).trim();
  if (!v) {
    const n = itemNoun(key);
    toast(`Please add ${indefinite(n)} ${n}`, { error: true });
    return;
  }
  const arr = state.compiled[key];
  if (arr.some((e) => e.value.toLowerCase() === v.toLowerCase())) return; // already present
  arr.push({ value: v, source: "manual" });
  justAddedChip = `compiled|${key}|${v}`;
  pendingFocus = { sel: `.settings-add-input[data-kind="compiled"][data-key="${key}"]` };
  paint();
  saveRules();
}

function removeCompiledManual(key, value) {
  const arr = state.compiled[key];
  const i = arr.findIndex((e) => e.value === value && e.source === "manual");
  if (i === -1) return; // rule chips aren't removable here
  arr.splice(i, 1);
  pendingFocus = { sel: `.settings-add-input[data-kind="compiled"][data-key="${key}"]` };
  paint();
  saveRules();
}

function syncComposerFromDom() {
  if (!root) return;
  const verb = root.querySelector("[data-rule-new-verb]");
  const target = root.querySelector("[data-rule-new-target]");
  const term = root.querySelector("[data-rule-new-term]");
  if (verb) composer.verb = verb.value;
  if (target) composer.target = target.value;
  if (term) composer.term = term.value;
}

function enforceComposerConstraint() {
  if (composer.verb === "exclude" && composer.target === "location") composer.target = "title";
}

function splitTerms(raw) {
  return String(raw)
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

function addRuleFromComposer() {
  syncComposerFromDom();
  enforceComposerConstraint();
  const terms = splitTerms(composer.term);
  if (!terms.length) {
    toast("Please enter at least one term", { error: true });
    return;
  }
  state.rules.push({ id: newRuleId(), verb: composer.verb, target: composer.target, terms });
  composer.term = "";
  pendingFocus = { sel: "[data-rule-new-term]" };
  paint();
  saveRules();
}

function addTermToRule(id, raw) {
  const terms = splitTerms(raw);
  if (!terms.length) {
    toast("Please enter a term", { error: true });
    return;
  }
  const rule = state.rules.find((r) => r.id === id);
  if (!rule) return;
  for (const t of terms) {
    if (!rule.terms.some((x) => x.toLowerCase() === t.toLowerCase())) rule.terms.push(t);
  }
  pendingFocus = { sel: `.rule-card[data-rule-id="${id}"] [data-rule-term-input]` };
  paint();
  saveRules();
}

function removeRule(id) {
  state.rules = state.rules.filter((r) => r.id !== id);
  paint();
  saveRules();
}

function removeRuleTerm(id, term) {
  const rule = state.rules.find((r) => r.id === id);
  if (!rule) return;
  rule.terms = rule.terms.filter((t) => t !== term);
  if (!rule.terms.length) state.rules = state.rules.filter((r) => r.id !== id); // last term drops the rule
  paint();
  saveRules();
}

function parseCriteriaError(detail) {
  const text = String(detail || "");
  const field = CRIT_FIELDS.find((f) => text.includes(f)) || null;
  const label = field ? CRIT_LABELS[field] : null;
  let message = text || "Could not save fit criteria.";
  if (label) {
    if (/missing required key/.test(text)) message = `${label} is required.`;
    else if (/must be int/.test(text)) message = `${label} must be a whole number.`;
    else if (/must be list/.test(text)) message = `${label} must be a list.`;
    else if (/must be dict/.test(text)) message = `${label} must be a set of key/value pairs.`;
    else message = `${label}: ${text}`;
  }
  return { field, message };
}

/* Resolve the typed center to coordinates via the offline place table and stage
   a location_radius block (it saves with the rest of Tier 1). A blank query turns
   radius off; an unresolvable place anchors a field error and leaves the prior
   center untouched. */
async function resolveCenter(query) {
  const q = (query || "").trim();
  if (!q) {
    clearRadius();
    return;
  }
  const minsEl = root.querySelector("[data-radius-minutes]");
  const minsNum = minsEl ? parseInt(minsEl.value, 10) : 30;
  const radius_minutes = Number.isInteger(minsNum) && minsNum >= 1 ? minsNum : 30;
  const prev = state.criteria.tier1_params.location_radius || {};
  try {
    const hit = await api.geocode(q);
    criteriaError = null;
    state.criteria.tier1_params.location_radius = {
      center: { lat: hit.lat, lng: hit.lng, label: hit.label },
      radius_minutes,
      ...(prev.estimate ? { estimate: prev.estimate } : {}),
    };
    paint();
  } catch (error) {
    criteriaError = {
      field: "location_radius",
      message:
        error.status === 404
          ? `Couldn't find "${q}" — try "Town, ST" (e.g. Madison, WI).`
          : error.detail || error.message || "Could not resolve that place.",
    };
    paint();
  }
}

function clearRadius() {
  if (state.criteria) state.criteria.tier1_params.location_radius = null;
  criteriaError = null;
  paint();
}

/* Live minutes edit into the staged radius block (no-op until a center exists —
   resolveCenter reads the minutes input when it sets the center). */
function syncRadiusMinutes(value) {
  const r = state.criteria && state.criteria.tier1_params.location_radius;
  if (!r) return;
  const n = parseInt(String(value).trim(), 10);
  if (Number.isInteger(n) && n >= 1) r.radius_minutes = n;
}

async function saveCriteria() {
  criteriaError = null; // fresh attempt re-validates from scratch
  syncCriteriaFromDom();
  // location_allowlist is rule-owned; echo the current compiled value so the
  // criteria write never clobbers it with a stale array.
  if (state.criteria && state.compiled) {
    state.criteria.tier1_params.location_allowlist = (state.compiled.location_allowlist || []).map(
      (e) => e.value
    );
  }
  try {
    const updated = await api.putCriteria(criteriaPayload());
    setCriteria(updated); // re-arms criteriaBaseline (location echoed in) → not dirty
    paint();
    toast("Fit criteria saved — rescore to apply to existing jobs");
  } catch (error) {
    criteriaError =
      error.status === 422
        ? parseCriteriaError(error.detail)
        : { field: null, message: error.detail || error.message };
    paint();
  }
}

/* Decline (or un-decline) using an API key. Repaints immediately so the field +
   Save disable and the warning show, persists the choice, then refreshes the
   setup tracker (declining completes the api_key step; unchecking reopens it).
   Rolls back + repaints on a failed save. */
async function toggleDeclineKey(declined) {
  const prev = state.apiKeyDeclined;
  state.apiKeyDeclined = declined;
  paint();
  try {
    await api.putSetting("api_key_declined", declined);
    refreshOnboardingTracker();
  } catch (error) {
    state.apiKeyDeclined = prev;
    paint();
    toast(error.detail || error.message, { error: true });
  }
}

/* Optimistic save of the macOS-popup toggle; rollback + repaint on failure. */
async function toggleNotifyPopups(on) {
  const prev = state.notifyPopups;
  state.notifyPopups = on;
  try {
    await api.putSetting("notify_popups", on);
  } catch (error) {
    state.notifyPopups = prev;
    paint();
    toast(error.detail || error.message, { error: true });
  }
}

async function saveApiKey(value) {
  const key = (value || "").trim();
  if (!key) {
    toast("Enter an API key first", { error: true });
    return;
  }
  try {
    state.apiKey = await api.putApiKey(key);
    state.apiKeyTest = null; // a new key invalidates any prior Test result
    paint();
    refreshOnboardingTracker(); // a configured key completes the api_key step
    toast("API key saved");
  } catch (error) {
    toast(error.detail || error.message, { error: true });
  }
}

async function testApiKey() {
  state.apiKeyTest = "pending";
  paint();
  try {
    state.apiKeyTest = await api.testApiKey(); // {ok, error}
  } catch (error) {
    // A 503 (no key) or transport failure — surface the detail, don't crash.
    state.apiKeyTest = { ok: false, error: error.detail || error.message };
  }
  paint();
}

async function removeApiKey() {
  const ok = await confirmModal({
    title: "Remove API key?",
    message: "AI features (scoring, compose, tailoring) will turn off until you add a key again.",
    confirmLabel: "Remove",
  });
  if (!ok) return;
  try {
    state.apiKey = await api.deleteApiKey();
    state.apiKeyTest = null;
    paint();
    refreshOnboardingTracker(); // no key + not declined reopens the api_key step
    toast("API key removed");
  } catch (error) {
    toast(error.detail || error.message, { error: true });
  }
}

async function savePersona(name, label) {
  const domain_label = (label || "").trim();
  if (!domain_label) {
    toast("Enter the role you're searching for", { error: true });
    return;
  }
  try {
    // Blank name → null (anonymous). The server normalizes too, but sending null
    // keeps the intent explicit.
    state.persona = await api.putPersona({
      display_name: (name || "").trim() || null,
      domain_label,
    });
    paint();
    toast("Persona saved");
  } catch (error) {
    toast(error.detail || error.message, { error: true });
  }
}

async function saveVoiceGuide(text) {
  try {
    const res = await api.putVoiceGuide(text);
    state.voiceGuide = res.markdown || "";
    paint();
    toast("Voice guide saved");
  } catch (error) {
    toast(error.detail || error.message, { error: true });
  }
}

async function rescore() {
  let est = null;
  try {
    est = await api.rescoreEstimate();
  } catch {
    /* estimate is best-effort — fall back to a generic confirm */
  }
  const message = est
    ? `Re-scores ~${est.to_score} of ${est.active} active jobs with AI (~$${(est.est_cost_usd ?? 0).toFixed(2)}). Takes a few minutes; runs in the background.`
    : "Re-scores every active job against the current criteria. Takes a few minutes; runs in the background.";
  const ok = await confirmModal({ title: "Rescore now?", message, confirmLabel: "Rescore" });
  if (!ok) return;
  try {
    const res = await api.rescore();
    if (res.running) {
      toast("A refresh or rescore is already running");
      return;
    }
  } catch (error) {
    toast(error.detail || error.message, { error: true });
    return;
  }
  state.rescoring = true;
  paint();
  toast("Rescoring all active jobs…");
  pollRescore();
}

let rescorePoll = null;

function settingsMounted() {
  return !!(root && root.querySelector(".settings-view"));
}

function stopRescorePoll() {
  if (rescorePoll) {
    clearInterval(rescorePoll);
    rescorePoll = null;
  }
}

function pollRescore() {
  stopRescorePoll();
  rescorePoll = setInterval(async () => {
    // Self-terminate if Settings is no longer mounted (the user navigated away
    // mid-rescore). app.js renders every view into the same #view element, so a
    // leaked timer's paint() would repaint Settings over whatever view is showing
    // — the "pulled back to Settings" bug. The rescore keeps running server-side;
    // re-entering Settings resumes the poll (see render()).
    if (!settingsMounted()) {
      stopRescorePoll();
      return;
    }
    let status;
    try {
      status = await api.refreshStatus();
    } catch {
      stopRescorePoll();
      state.rescoring = false;
      settleRescorePaint();
      return;
    }
    state.status = status; // keep progress (status.scoring_progress) fresh each tick
    if (!status.running) {
      stopRescorePoll();
      state.rescoring = false;
      settleRescorePaint();
      const r = status.scoring_report;
      toast(
        r
          ? `Rescore complete — ${r.scored} scored` +
              (r.rate_limited ? `, ${r.rate_limited} rate-limited` : "") +
              (r.errors ? `, ${r.errors} errors` : "")
          : "Rescore complete"
      );
      if (r?.errors) buzz("rescore:" + r.at);
      else chime("rescore:" + (r?.at || "done"));
      return;
    }
    // Progress tick — same guard as the settle paths below.
    settleRescorePaint();
  }, 4000);
}

/* Repaint after a poll tick — but NEVER over an actively edited field. A
   wholesale paint() rebuilds root.innerHTML: the API key input renders valueless
   by design (the key is never echoed), persona renders from state, and the
   chip/rule/radius drafts live only in the DOM, so a paint mid-edit wipes the
   value and yanks focus — a pasted key, silently lost. This guard was on the
   progress tick only; the completion and error settles called paint()
   unconditionally, so a rescore finishing (or a status poll failing) while the
   user typed destroyed the input. All three sites route through here now. While
   a control is focused, update the progress line in place; the rest of the
   rescore chrome refreshes on the next paint (a save, a tab switch). */
function settleRescorePaint() {
  if (!settingsMounted()) return;
  const active = document.activeElement;
  const editing =
    root.contains(active) && active.matches?.("input, textarea, select");
  if (editing) updateRescoreProgress();
  else paint();
}

/* The poll's in-place tick: rewrites just the progress span, and clears it once
   the rescore has settled so an edit-guarded completion leaves no stale
   "Rescoring 5/10" behind. */
function updateRescoreProgress() {
  const el = root.querySelector("[data-rescore-progress]");
  if (!el) return;
  const prog = state.rescoring && state.status?.scoring_progress;
  if (!prog) {
    el.textContent = "";
    return;
  }
  el.textContent =
    (prog.total ? `Rescoring ${prog.done}/${prog.total}` : "Rescoring…") +
    (prog.errors ? ` · ${prog.errors} errors` : "");
}

async function reloadSettings() {
  try {
    const [reasons, sources, suggestions, scoringRules] = await Promise.all([
      api.getSetting("dismiss_reasons"),
      api.getSetting("contact_sources"),
      api.getSuggestions(),
      api.getScoringRules(),
    ]);
    state.settings.dismiss_reasons = reasons.value || [];
    state.settings.contact_sources = sources.value || [];
    applySuggestions(suggestions);
    state.scoringRules = scoringRules.rules || [];
    paint();
  } catch {
    /* leave the current view; the failed action already toasted */
  }
}

/* ---------- events ---------- */

function onClick(event) {
  const el = event.target.closest("[data-action]");
  if (!el) return;
  syncCriteriaFromDom(); // capture in-progress criteria text before any repaint
  syncComposerFromDom(); // and the in-progress add-rule composer
  const action = el.dataset.action;

  if (action === "settings-tab") {
    switchTab(el.dataset.tab);
  } else if (action === "tag-add") {
    const input = root.querySelector(
      `.settings-add-input[data-kind="${el.dataset.kind}"][data-key="${el.dataset.key}"]`
    );
    const value = input ? input.value : "";
    if (el.dataset.kind === "compiled") addCompiledManual(el.dataset.key, value);
    else addTag(el.dataset.kind, el.dataset.key, value);
  } else if (action === "tag-remove") {
    if (el.dataset.kind === "compiled") removeCompiledManual(el.dataset.key, el.dataset.value);
    else removeTagAnimated(el.dataset.kind, el.dataset.key, el.dataset.value, el.closest(".settings-tag"));
  } else if (action === "rule-add") {
    addRuleFromComposer();
  } else if (action === "rule-remove") {
    removeRule(el.dataset.ruleId);
  } else if (action === "rule-term-add") {
    const input = root.querySelector(`.rule-card[data-rule-id="${el.dataset.ruleId}"] [data-rule-term-input]`);
    addTermToRule(el.dataset.ruleId, input ? input.value : "");
  } else if (action === "rule-term-remove") {
    removeRuleTerm(el.dataset.ruleId, el.dataset.term);
  } else if (action === "toggle-advanced") {
    const scope = el.dataset.scope;
    const opening = !expandedAdvanced.has(scope);
    if (opening) expandedAdvanced.add(scope);
    else expandedAdvanced.delete(scope);
    paint();
    // Toggle-scoped reveal — see .collapse-enter in app.css (P5).
    if (opening)
      root
        .querySelector(`[data-action="toggle-advanced"][data-scope="${scope}"]`)
        ?.closest(".advanced-details")
        ?.querySelector(".collapse-body")
        ?.classList.add("collapse-enter");
  } else if (action === "suggestion") {
    onSuggestion(el.dataset.type, el.dataset.key, el.dataset.verb);
  } else if (action === "scoring-rule-remove") {
    removeScoringRule(el.dataset.id);
  } else if (action === "map-add") {
    const map = el.dataset.map;
    const list = map === "overrides" ? state.criteria._overrides : state.criteria._bands;
    list.push({ key: "", val: "" });
    pendingFocus = { sel: `.map-row[data-map="${map}"][data-index="${list.length - 1}"] [data-mapkey]` };
    paint();
  } else if (action === "map-remove") {
    const list = el.dataset.map === "overrides" ? state.criteria._overrides : state.criteria._bands;
    list.splice(Number(el.dataset.index), 1);
    paint();
  } else if (action === "t2-add") {
    state.criteria.tier2_criteria.push({ text: "", weight: 1, craft: false, bonus_only: false });
    pendingFocus = {
      sel: `.tier2-row[data-index="${state.criteria.tier2_criteria.length - 1}"] [data-t2]`,
    };
    paint();
  } else if (action === "t2-remove") {
    state.criteria.tier2_criteria.splice(Number(el.dataset.index), 1);
    paint();
  } else if (action === "t2-up" || action === "t2-down") {
    const i = Number(el.dataset.index);
    const j = action === "t2-up" ? i - 1 : i + 1;
    const list = state.criteria.tier2_criteria;
    if (j < 0 || j >= list.length) return;
    [list[i], list[j]] = [list[j], list[i]];
    pendingFocus = { sel: `.tier2-row[data-index="${j}"] [data-action="${action}"]` };
    paint();
  } else if (action === "radius-resolve") {
    const input = root.querySelector("[data-radius-center]");
    resolveCenter(input ? input.value : "");
  } else if (action === "radius-clear") {
    clearRadius();
  } else if (action === "save-criteria") {
    saveCriteria();
  } else if (action === "rescore") {
    rescore();
  } else if (action === "set-theme") {
    setThemePref(el.dataset.value);
    paint(); // refresh aria-pressed on the segment
  } else if (action === "toggle-notify-sound") {
    setSoundEnabled(el.checked);
    if (el.checked) preview(); // audible confirmation; the click doubles as the audio unlock
  } else if (action === "toggle-notify-popups") {
    toggleNotifyPopups(el.checked);
  } else if (action === "toggle-decline-key") {
    toggleDeclineKey(el.checked);
  } else if (action === "save-api-key") {
    const input = root.querySelector("[data-api-key-input]");
    saveApiKey(input ? input.value : "");
  } else if (action === "test-api-key") {
    testApiKey();
  } else if (action === "remove-api-key") {
    removeApiKey();
  } else if (action === "save-persona") {
    const nameEl = root.querySelector("[data-persona-name]");
    const labelEl = root.querySelector("[data-persona-label]");
    savePersona(nameEl ? nameEl.value : "", labelEl ? labelEl.value : "");
  } else if (action === "synthesis-draft") {
    synthesisDraft();
  } else if (action === "synthesis-copy-prompt") {
    synthesisCopyPrompt();
  } else if (action === "synthesis-check") {
    synthesisCheck();
  } else if (action === "synthesis-apply") {
    synthesisApply();
  } else if (action === "synthesis-discard") {
    synthesisDiscard();
  } else if (action === "synthesis-toggle-tier2") {
    state.synthesisApplyTier2 = event.target.checked; // DOM holds the box; no repaint
  } else if (action === "save-voice-guide") {
    const el = root.querySelector("[data-voice-guide]");
    saveVoiceGuide(el ? el.value : state.voiceGuide);
  }
}

function onKeydown(event) {
  if (event.key !== "Enter") return;
  const t = event.target;
  if (t.matches && t.matches("[data-rule-new-term]")) {
    event.preventDefault();
    addRuleFromComposer();
    return;
  }
  if (t.matches && t.matches("[data-rule-term-input]")) {
    event.preventDefault();
    addTermToRule(t.dataset.ruleId, t.value);
    return;
  }
  if (t.matches && t.matches("[data-radius-center]")) {
    event.preventDefault();
    resolveCenter(t.value);
    return;
  }
  const input = t.closest && t.closest("[data-add]");
  if (!input) return;
  event.preventDefault();
  syncCriteriaFromDom();
  if (input.dataset.kind === "compiled") addCompiledManual(input.dataset.key, input.value);
  else addTag(input.dataset.kind, input.dataset.key, input.value);
}

/* Live dirty indicator: typing in a criteria field flips "Unsaved changes"
   without a full repaint (which would steal focus on every keystroke). Bound to
   #view, which is reused across views — guard to the settings DOM so it no-ops
   elsewhere (other views don't reset container.oninput). */
function onInput(event) {
  if (!root || !root.querySelector(".settings-view")) return;
  const t = event.target;
  if (!t.matches) return;
  // Composer selects react live: changing the verb to "exclude" disables the
  // location target (no location-exclude list) and snaps a stale pick back.
  if (t.matches("[data-rule-new-verb],[data-rule-new-target]")) {
    syncComposerFromDom();
    enforceComposerConstraint();
    paint();
    return;
  }
  // The composer term input is synced without a repaint (typing must not steal focus).
  if (t.matches("[data-rule-new-term]")) {
    composer.term = t.value;
    return;
  }
  // The voice-guide textarea is synced to state on every keystroke (no repaint),
  // so a structural repaint elsewhere never drops an in-progress edit.
  if (t.matches("[data-voice-guide]")) {
    state.voiceGuide = t.value;
    return;
  }
  // Same contract for the synthesis paste-back box: sync without a repaint.
  if (t.matches("[data-synthesis-reply]")) {
    state.synthesisReply = t.value;
    return;
  }
  // Persona fields sync per keystroke, mirroring the voice guide: switching tabs
  // (or any structural repaint) rebuilds the panel from state.persona, so an
  // unsaved display-name/role edit would otherwise vanish — the voice-guide
  // field beside them survives precisely because it does this. Save still reads
  // the DOM, so this only keeps the edit buffer alive across a repaint.
  if (t.matches("[data-persona-name]")) {
    if (state.persona) state.persona.display_name = t.value;
    return;
  }
  if (t.matches("[data-persona-label]")) {
    if (state.persona) state.persona.domain_label = t.value;
    return;
  }
  // Radius minutes edits live: write nested state, then refresh the dirty flag
  // without a repaint (typing must not steal focus). The center is resolved
  // separately (button / Enter) because it needs a geocode round-trip.
  if (t.matches("[data-radius-minutes]")) {
    syncRadiusMinutes(t.value);
    updateDirtyIndicator();
    return;
  }
  if (t.matches("[data-param],[data-t2],[data-t2-weight],[data-mapkey],[data-mapval]")) {
    updateDirtyIndicator();
  }
}

/* ---------- entry ---------- */

/* Router hook (app.js): warn before leaving Settings with unsaved fit-criteria
   edits. isCriteriaDirty reads the live DOM so a typed-but-unsynced edit (no
   in-view click happened) is still caught. */
export async function canLeave() {
  if (!root || !state.criteria) return true;
  syncCriteriaFromDom();
  if (!criteriaDirtyFromState()) return true;
  return confirmModal({
    title: "Unsaved fit criteria",
    message: "You have unsaved changes to your fit criteria. Leave Settings and discard them?",
    confirmLabel: "Discard & leave",
  });
}

export async function render(container) {
  root = container;
  stopRescorePoll(); // kill any poll leaked from a prior mount
  activeTab = "sourcing";
  // One-shot tab handoff (the wizard's "Settings → Scoring" synthesis links):
  // hash routing carries no params, so the sender parks the tab id here.
  const handoffTab = sessionStorage.getItem("jshq-settings-tab");
  if (handoffTab && TABS.some((t) => t.id === handoffTab)) {
    sessionStorage.removeItem("jshq-settings-tab");
    activeTab = handoffTab;
  }
  criteriaError = null;
  rulesError = null;
  state.apiKeyTest = null; // a Test result is per-visit, not persisted across mounts
  pendingFocus = null;
  composer = { verb: "include", target: "title", term: "" };
  expandedAdvanced.clear();
  renderLoading(container);
  container.onclick = onClick;
  container.onkeydown = onKeydown;
  container.oninput = onInput;
  try {
    await load();
  } catch (error) {
    renderLoadError(container, error, () => render(container));
    setStats([]);
    return;
  }
  paint();
  if (state.rescoring) pollRescore(); // a rescore I started is still in flight — resume live progress
}
