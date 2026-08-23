/* First-run onboarding wizard (Phase 4). A guided, skippable flow: the only
   required step is "add your first company"; everything else is strongly
   recommended but can be skipped and finished later.

   While first-run is unresolved this view IS the application — app.js routes
   every hash here and hides the chrome (body.wizard-active) — so the wizard's
   own controls are the only exits: "Exit setup" records the dismissal, the done
   step's button records completion, and adding the company ends first-run
   structurally. The exit affordance exists ONLY once the required step is met
   (a company on the board — owner call, 2026-08-16): exiting earlier landed on
   a dashboard with nothing to show and "never refreshed" stats, an empty app
   wearing an error face. A returning visit (the Setup pill deep-links here)
   gets a welcome-back hub with per-step jump buttons instead of the
   first-timer pitch.

   Step order is reflection-first: the fulfillment matrix (what energizes/drains
   you) comes BEFORE the wish list it feeds. The wishlist becomes the Tier 2
   ranked list (rank drives weight — see rampedTier2); the matrix is stored raw
   in the roadmap for a later synthesis pass.

   Same repaint-safe pattern as settings.js: a module-level `s`, a full paint
   from state, delegated onclick/oninput handlers on data-action / data-field.
   Correctness contract (design review, 2026-08-15): every step hydrates from
   the saved config it writes, so an untouched Continue is a round-trip, never a
   wipe; Continue on the key step saves a typed key rather than discarding it;
   the raw roadmap write never depends on the criteria doc loading; and async
   continuations repaint nothing once another view owns #view. */

import { api } from "../api.js";
import { failReason } from "../lib/ats.js";
import { esc, flashRow, setFocusOut, toast } from "../lib/ui.js";
import { refresh as refreshOnboardingTracker } from "../lib/onboardingTracker.js";
import { loadVocab, levelBands } from "../lib/vocab.js";
import { STATUSES, VALUES_FIT } from "./companies.js";

let container = null;
let s = null; // state
let focusedStep = -1; // last step that took the data-autofocus (never re-steal)
let atsPoll = 0; // done-step status poll (setInterval id)

// welcome + done are chrome; the six between are the setup steps the bar counts.
// Matrix before wishlist: the reflection exercise feeds the ranking exercise.
const STEPS = ["welcome", "key", "profile", "filters", "matrix", "wishlist", "company", "done"];
const OPTIONAL = new Set(["key", "profile", "filters", "matrix", "wishlist"]);
const SETUP_TOTAL = 6;
// Per-tab breadcrumb so a mid-wizard reload resumes where the user was rather
// than dropping to the cold Welcome pitch (#39). Only ever restored during
// first-run (a returning user gets the welcome-back hub instead).
const WIZARD_STEP_KEY = "jshq-wizard-step";

// Recommendation strength per optional step (the required company step carries
// its own badge). One visual pill, two words: five identical "optional" tags in
// a row read as "all of this is skippable chores".
const BADGES = {
  key: "optional",
  profile: "recommended",
  filters: "recommended",
  matrix: "optional",
  wishlist: "recommended",
};

// The three region tokens the "remote US is fine" checkbox owns — added when
// checked, REMOVED when unchecked (a toggle, not a one-way ratchet), any other
// configured regions left alone.
const REMOTE_US_TOKENS = ["united states", "us", "usa"];

// The 2x2: [key, quadrant name, prompt, example placeholder]. Grid order is
// row-major under the Strength | Growth-area column headers.
const MATRIX_CELLS = [
  ["energizing_strength", "Keep doing this", "Great at it, and it energizes you.",
    "e.g. Coaching juniors one-on-one; untangling gnarly problems"],
  ["energizing_growth", "Grow into this", "Not there yet, but it excites you.",
    "e.g. Public speaking; owning a budget end to end"],
  ["draining_strength", "The trap", "Good at it, done wanting to do it.",
    "e.g. Rescuing slipping projects; stakeholder diplomacy"],
  ["draining_growth", "Leave these", "Drains you and isn't a strength.",
    "e.g. Weekly status decks; on-call firefighting"],
];

function initState() {
  return {
    loading: true,
    saving: false,
    step: 0,
    onboarding: null, // GET /api/onboarding payload (readiness + first_run)
    keyStatus: null,
    key: "",
    apiKeyDeclined: false, // "I don't want a key" — completes the api_key step
    keyResult: null, // null | "testing" | {ok, error}
    changingKey: false, // true ⇒ show the field even when a key is already set
    aiProviderChoice: "anthropic", // which pane the Turn-on-AI step shows ("anthropic" | "openai_compat")
    aiProviders: null, // GET /api/settings/ai-providers payload (endpoint status)
    compatUrl: "", // endpoint pane field buffers (data-field synced)
    compatKey: "",
    compatModel: "",
    compatModels: [], // model ids from the last endpoint test (datalist food)
    compatSavedModel: "", // the model id the axes already carry; an unchanged Continue must not re-write (and re-flatten) them
    compatResult: null, // null | "testing" | {ok, models?} | {ok:false, error}
    careersSearch: null, // company-step careers probe: null | "searching" | {found, url}
    careersFor: "", // the website value that launched the in-flight probe (stale guard)
    careersAutofilled: false, // the careers field holds a probe result, not a hand-typed URL
    name: "",
    field: "",
    fieldLoaded: "", // what the field input was hydrated with; unchanged ⇒ no write
    domainLabel: "",
    tier1: null, // current tier1_params (co-sent with every criteria write)
    criteriaLoadFailed: false, // GET criteria failed ⇒ banner, not silent no-ops
    roadmapLoadFailed: false, // GET roadmap failed ⇒ park its whole-file PUTs
    rawWishlist: null, // the roadmap's own stored word list (raw capture)
    hydratedWishlistTexts: [], // wishlist texts at mount; unchanged ⇒ keep raw
    compFloor: "",
    remoteUs: false,
    homeTown: "", // location_radius center label ("Evanston, IL")
    driveMins: "", // location_radius.radius_minutes
    locations: "", // MANUAL allowlist towns only — rule-owned ones live in ruleTowns
    ruleTowns: [], // location terms the inclusion-rules compiler owns (read-only here)
    sectors: "", // excluded_sectors as a comma-joined string (a hard filter)
    titleBands: [], // target_title_bands the seniority checkboxes drive
    seededLinkedinTitles: "", // JSON of the LinkedIn defaults THIS session seeded; filters may upgrade only that
    wishlist: [], // ordered Tier2Item objects {text, weight, craft, bonus_only}
    orderChanged: false, // any add/remove/move this session — gates the rank ramp
    touchedWeights: new Set(), // item REFS the user hand-weighted (identity survives reorder)
    wishDraft: "",
    editingWish: -1, // index being edited in place (click the text); -1 = none
    editDraft: "",
    matrix: { energizing_strength: "", energizing_growth: "", draining_growth: "", draining_strength: "" },
    companyName: "",
    companyUrl: "",
    companyCareers: "", // careers page URL — its own field; the most reliable detection input
    companyLocation: "",
    companyPriority: "", // "" = unset, else "1".."5"
    companyStatus: "prospect", // the add-modal's default
    companyValues: "", // "" = unset, else a VALUES_FIT entry
    companies: [], // what's already on the board — the required step shows its evidence
    createdCompany: null, // the created row; the done step polls its ATS status
    schedule: null, // GET /api/schedule payload; the done-step opt-in gates on supported
    installSchedule: true, // "keep this fresh automatically" — pre-checked, Finish installs unless unticked
    errors: {}, // per-field inline validation messages, keyed by data-field
    hydrated: null, // post-load snapshot; "Skip this step" warns when it differs
    exampleOpen: false, // the wishlist's "see a filled-in example" disclosure
    exampleHtml: null, // fetched+parsed example list (cached for repaints)
  };
}

export async function render(el) {
  container = el;
  // Claim the container BEFORE the first paint: owned() treats a container
  // holding another view's DOM as not-ours (that is the async-repaint guard),
  // and on an in-app navigation the previous view's content is still here.
  container.innerHTML = "";
  clearInterval(atsPoll); // a fresh mount never inherits an old done-step poll
  s = initState();
  focusedStep = -1;
  paint();
  container.onclick = onClick;
  container.oninput = onInput;
  container.onkeydown = onKeydown;
  container.onpointerdown = onPointerDown; // see onFocusOut's commit race note
  setFocusOut(container, onFocusOut);
  const [criteria, persona, keyStatus, roadmap, onboarding, companies, inclusionRules, scheduleStatus, aiProviders, aiModels] =
    await Promise.all([
      api.getCriteria().catch(() => null),
      api.getPersona().catch(() => null),
      api.getApiKeyStatus().catch(() => null),
      api.getRoadmap().catch(() => null),
      api.getOnboarding().catch(() => null),
      api.listCompanies().catch(() => []),
      api.getInclusionRules().catch(() => null),
      // The done step's schedule opt-in: a failed fetch just hides the line.
      api.getSchedule().catch(() => null),
      // The Turn-on-AI step's endpoint pane; failures degrade to blank fields.
      api.getAiProviders().catch(() => null),
      api.getAiModels().catch(() => null),
      // The filters step's title-band picker reads the display vocabulary
      // (seniority ladder); loadVocab caches the promise so levelBands() is
      // populated by the time the step paints. A failed fetch degrades to the
      // offline FALLBACK ladder, never blocks the wizard.
      loadVocab().catch(() => null),
    ]);
  s.companies = companies;
  s.onboarding = onboarding;
  s.schedule = scheduleStatus;
  s.aiProviders = aiProviders;
  // Endpoint pane hydration: the saved URL, and the model id the axes (or the
  // switch-back memory) already carry. A returning endpoint user lands on
  // their own pane; everyone else starts on Anthropic.
  s.compatUrl = aiProviders?.base_url || "";
  const rem = aiModels?.remembered;
  s.compatModel =
    (aiModels?.analysis?.provider === "openai_compat" && aiModels.analysis.model) ||
    rem?.analysis?.openai_compat ||
    rem?.writing?.openai_compat ||
    "";
  s.compatSavedModel =
    aiModels?.analysis?.provider === "openai_compat" ? aiModels.analysis.model || "" : "";
  if (aiProviders?.configured && !keyStatus?.configured) s.aiProviderChoice = "openai_compat";
  s.apiKeyDeclined = onboarding?.api_key_declined === true;
  s.criteriaLoadFailed = criteria === null;
  if (criteria?.tier1_params) {
    const t1 = criteria.tier1_params;
    s.tier1 = t1;
    // Hydrate the filters step from the saved config it writes: a returning
    // user's untouched Continue must round-trip these values, not zero them.
    s.compFloor = t1.comp_floor > 0 ? String(t1.comp_floor) : "";
    // location_allowlist has two owners: the inclusion-rules compiler emits
    // the rule terms, and this field edits only the manual extras (the same
    // split settings.js enforces). Rendering the whole array flat let a
    // returning user delete a rule-owned town here — the doc then diverged
    // from the rules, and the next rules save silently resurrected the town.
    const loc = inclusionRules?.compiled?.location_allowlist;
    s.ruleTowns = loc ? loc.filter((e) => e.source === "rule").map((e) => e.value) : [];
    s.locations = loc
      ? loc.filter((e) => e.source === "manual").map((e) => e.value).join(", ")
      : (t1.location_allowlist || []).join(", "); // rules fetch failed: degrade to flat
    s.remoteUs = (t1.remote_regions || []).some((r) => REMOTE_US_TOKENS.includes(r));
    s.sectors = (t1.excluded_sectors || []).join(", ");
    s.titleBands = Array.isArray(t1.target_title_bands) ? [...t1.target_title_bands] : [];
    const radius = t1.location_radius;
    s.homeTown = radius?.center?.label || "";
    s.driveMins = radius?.radius_minutes ? String(radius.radius_minutes) : "";
  }
  // Keep Tier 2 items WHOLE ({text, weight, craft, bonus_only}): sending back
  // text-only copies flattened every weight to 1.0 and stripped the craft/bonus
  // markers (which the API rightly 422s on a marker-bearing doc).
  s.wishlist = criteria?.tier2_criteria ?? [];
  if (persona) {
    s.name = persona.display_name || "";
    // The API serves a neutral default when the doc never answered ("the roles
    // you are searching for") and flags it — placeholder prose, not a value.
    // Treat it as unset: prefilling it once let a user append their real
    // answer to it and trip the 120-char persona rail (an old partial save had
    // written the taxonomy but not the persona, so field.done was true while
    // domain_label was the default). An empty input beats echoed prose.
    s.domainLabel = persona.domain_label_is_default ? "" : persona.domain_label || "";
    // Prefill the field input only when a field is actually configured
    // (readiness `field.done`): an unchanged prefill must NOT re-fire the
    // taxonomy writer (see saveProfile) — write_field would replace a
    // customized taxonomy block with the minimal wizard one.
    if (onboarding?.steps?.field?.done) s.field = s.domainLabel;
    s.fieldLoaded = s.field;
  }
  s.keyStatus = keyStatus;
  // The roadmap PUT replaces the whole file, so a failed GET must PARK the
  // writes (same posture as criteriaLoadFailed): hydrating an empty matrix
  // and then saving would permanently destroy the stored answers.
  s.roadmapLoadFailed = roadmap === null;
  const rm = roadmap?.roadmap ?? {};
  if (rm.matrix) s.matrix = { ...s.matrix, ...rm.matrix };
  if (Array.isArray(rm.wishlist)) s.rawWishlist = rm.wishlist;
  s.hydratedWishlistTexts = s.wishlist.map((item) => item.text);
  seedTouchedWeights(); // protect a returning user's persisted hand weights
  s.hydrated = {
    name: s.name.trim(),
    field: s.field.trim(),
    compFloor: s.compFloor.trim(),
    homeTown: s.homeTown.trim(),
    driveMins: s.driveMins.trim(),
    locations: s.locations.trim(),
    remoteUs: s.remoteUs,
    sectors: s.sectors.trim(),
    titleBands: JSON.stringify(s.titleBands),
    wishlist: JSON.stringify(s.wishlist),
    matrix: JSON.stringify(s.matrix),
  };
  // Resume a first-run walk where it left off (#39): render() re-mounts at step 0,
  // so a reload otherwise drops to the cold Welcome pitch even though the config
  // is all saved. Only for first-run — a returning user's step-0 IS the hub with
  // per-step jumps. Restore only a real setup step (1..SETUP_TOTAL); anything
  // else (welcome/done/garbage) stays at 0.
  if (s.onboarding?.first_run) {
    let saved = NaN;
    try {
      saved = parseInt(sessionStorage.getItem(WIZARD_STEP_KEY) ?? "", 10);
    } catch {
      /* storage unavailable — no resume */
    }
    if (Number.isInteger(saved) && saved >= 1 && saved <= SETUP_TOTAL) s.step = saved;
  }
  s.loading = false;
  paint();
}

/* ---- rendering ---------------------------------------------------------- */

/* True while the wizard still owns #view. Async continuations (a slow save, the
   key test) land after the user may have navigated away; painting then would
   resurrect the wizard over the next view (the same hazard settings.js guards
   in its onInput). An empty container is ours — the mount paint. */
function owned() {
  return !!container && (!container.firstElementChild || !!container.querySelector(".wizard"));
}

function returning() {
  return !!s.onboarding && s.onboarding.first_run === false;
}

function exitLabel() {
  return returning() ? "Back to dashboard →" : "Exit setup →";
}

/* The required step means it: while the board is EMPTY there is no exit and no
   company-step skip ANYWHERE in the flow — the way out is adding the company
   (every other step remains individually skippable on the way there). Once
   companies exist the requirement is met: the company step becomes an ordinary
   skippable one and the exit / Back-to-dashboard affordance appears. */
function companyRequired() {
  return s.companies.length === 0;
}

function skippable(id) {
  return OPTIONAL.has(id) || (id === "company" && !companyRequired());
}

function paint() {
  if (!owned()) return;
  const id = STEPS[s.step];
  const showBar = s.step >= 1 && s.step <= SETUP_TOTAL;
  // Position AND completion, honestly: segments behind you are filled, the one
  // you are on is highlighted, the rest are track — the old single bar read
  // "100%" while the required step was still undone.
  const segs = Array.from({ length: SETUP_TOTAL }, (_, i) => {
    const n = i + 1;
    const cls = n < s.step ? " wizard-seg-done" : n === s.step ? " wizard-seg-current" : "";
    return `<span class="wizard-seg${cls}"></span>`;
  }).join("");
  // The no-exit-while-the-board-is-empty trap is a FIRST-RUN rule: dismissal
  // there strands the user on an empty dashboard. A returning user's exit is
  // just navigation back to a dashboard they already live in — hiding it
  // (with all app chrome hidden under body.wizard-active) left someone who
  // deleted their last company with no way out of the hub but browser-back.
  const showExit = id !== "done" && (returning() || !companyRequired());
  container.innerHTML = `
    <div class="wizard">
      <div class="wizard-head">
        <span class="wizard-kicker">Getting set up</span>
        ${showExit
          ? `<button class="wizard-skip-all" data-action="skip-all">${exitLabel()}</button>`
          : ""}
      </div>
      ${showBar
        ? `<div class="wizard-segs" role="progressbar" aria-label="Setup progress" aria-valuenow="${s.step - 1}" aria-valuemin="0" aria-valuemax="${SETUP_TOTAL}" aria-valuetext="Step ${s.step} of ${SETUP_TOTAL}, ${s.step - 1} behind you">${segs}</div>
           <div class="wizard-stepcount">Step ${s.step} of ${SETUP_TOTAL}</div>`
        : ""}
      <div class="wizard-card section">${stepBody(id)}</div>
      <div class="wizard-navbar">
        <div class="wizard-navbar-inner">${navRow(id)}</div>
      </div>
    </div>`;
  // Autofocus only when the step CHANGES: re-focusing on every repaint stole
  // focus from the wishlist's reorder buttons after each press.
  if (focusedStep !== s.step) {
    const focus = container.querySelector("[data-autofocus]");
    if (focus) focus.focus();
    // A11Y (#41): the field autofocus tells a screen-reader user the new field's
    // label but not that they advanced or which step this is. Announce the step
    // through the persistent polite live region (it lives outside #view, which
    // the wizard repaints wholesale).
    const live = document.getElementById("wizard-live");
    const title = container.querySelector(".wizard-title")?.textContent?.trim();
    if (live && title) {
      live.textContent = showBar ? `Step ${s.step} of ${SETUP_TOTAL}: ${title}` : title;
    }
    focusedStep = s.step;
  }
}

function badge(id) {
  const word = BADGES[id];
  return word ? ` <span class="wizard-optional">${word}</span>` : "";
}

function fieldError(name) {
  const msg = s.errors[name];
  // role="alert" so the validation message is spoken when it appears, and a
  // stable id so the input can point aria-describedby at it (see errAttrs).
  return msg
    ? `<p class="wizard-note wizard-err" id="wiz-err-${name}" role="alert">${esc(msg)}</p>`
    : "";
}

/* The a11y attributes a validated input carries WHILE its error is showing:
   aria-invalid marks the field, aria-describedby links it to the fieldError <p>
   (id "wiz-err-<name>"). Cleared automatically once s.errors[name] is deleted by
   the onInput handler. */
function errAttrs(name) {
  return s.errors[name] ? ` aria-invalid="true" aria-describedby="wiz-err-${name}"` : "";
}

/* After a validation-failure repaint, move focus to the first invalid field so
   the reason is announced (role="alert") in context. Used by both the filters
   and the required company step. */
function focusFirstError() {
  container.querySelector('[aria-invalid="true"]')?.focus();
}

/* A lightbulb in the app's inline-SVG icon style (Feather/Lucide stroke set:
   24×24, currentColor, width 2, round joins) — the leading marker that flags a
   line as a tip. CSS (.wizard-hint svg) sizes and mutes it. */
function hintIcon() {
  return `<svg class="wizard-hint-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="9" y1="18" x2="15" y2="18"/><line x1="10" y1="22" x2="14" y2="22"/><path d="M15.09 14c.18-.98.65-1.74 1.41-2.5A4.65 4.65 0 0 0 18 8 6 6 0 0 0 6 8c0 1 .23 2.23 1.5 3.5A4.61 4.61 0 0 1 8.91 14"/></svg>`;
}

/* The criteria doc failed to load at mount: the steps that write through it say
   so instead of letting Continue silently save nothing. wordsSafe adds the
   reassurance for steps whose input has a separate raw capture (the roadmap) —
   the profile's name/field have no such store, so its variant omits it rather
   than promise something untrue. */
function criteriaBanner({ wordsSafe = true } = {}) {
  if (!s.criteriaLoadFailed) return "";
  const safe = wordsSafe ? " Your words are still kept safe." : "";
  return `<p class="wizard-note wizard-err">Couldn't load your criteria — these
    answers can't be written to it yet.${safe}</p>`;
}

/* The roadmap failed to load at mount: its PUT replaces the whole file, so
   saving on top of an empty hydrate would destroy the stored answers —
   writes are parked (putRoadmapGuarded) and these two steps say so. */
function roadmapBanner() {
  if (!s.roadmapLoadFailed) return "";
  return `<p class="wizard-note wizard-err">Couldn't load your saved reflection —
    answers to this exercise can't be saved right now. Reload to try again.</p>`;
}

function navRow(id) {
  if (id === "welcome") {
    const label = returning() ? "Walk through from the top" : "Get started";
    return `<div class="wizard-nav wizard-nav-single">
      <button class="btn btn-accent" data-action="next">${label}</button>
    </div>`;
  }
  if (id === "done") {
    return `<div class="wizard-nav wizard-nav-single">
      <button class="btn btn-accent" data-action="finish"${s.saving ? " disabled" : ""}>Go to my dashboard</button>
    </div>`;
  }
  const back = `<button class="btn btn-ghost" data-action="back">Back</button>`;
  const skip = skippable(id)
    ? `<button class="btn btn-ghost" data-action="skip">Skip this step</button>`
    : "";
  const nextLabel = s.saving ? "Saving…" : id === "company" ? "Add & finish setup" : "Continue";
  const next = `<button class="btn btn-accent" data-action="next"${s.saving ? " disabled" : ""}>${nextLabel}</button>`;
  return `<div class="wizard-nav">
    <div>${back}</div>
    <div class="wizard-nav-right">${skip}${next}</div>
  </div>`;
}

function stepBody(id) {
  switch (id) {
    case "welcome":
      return returning() ? welcomeBackStep() : welcomeStep();
    case "key":
      return keyStep();
    case "profile":
      return profileStep();
    case "filters":
      return filtersStep();
    case "matrix":
      return matrixStep();
    case "wishlist":
      return wishlistStep();
    case "company":
      return companyStep();
    case "done":
      return doneStep();
  }
  return "";
}

function welcomeStep() {
  return `
    <h1 class="wizard-title">Welcome to Job Search HQ</h1>
    <p class="wizard-lead">One dashboard for your whole search: the companies you pick,
      every opening they post, and once you tell it what matters to you, which postings
      deserve your energy. A few minutes of setup gets your first board watched.</p>
    <p class="wizard-lead">It's yours, and it stays yours. Everything runs on your own
      machine. No accounts, no tracking, no cloud. The only things that ever leave are
      requests to job-board services to find and pull each company's openings (plus a
      logo lookup per company), and, if you add a key, calls to Anthropic with that key.</p>
    <p class="wizard-lead">A few minutes now makes the rest much better. The more
      you tell it about what you're after, the sharper the results.</p>`;
}

/* A returning visit (the Setup pill, or curiosity) is not a first run: greet
   the progress, list the steps with jump buttons, and never re-pitch. */
function welcomeBackStep() {
  const ob = s.onboarding;
  const st = ob?.steps ?? {};
  // One row per COUNTED setup step, so the list and the "N of M done" header
  // agree. The voice guide is deliberately not among them — it's optional and
  // AI-only, lives in Settings, and has no wizard step (the same reason the
  // persona display name is uncounted); it gets a non-counted pointer below
  // instead. The profile row no longer waits on a name (persona) — that's
  // optional, so it never gates completion.
  const rows = [
    { id: "key", label: "API key", done: !!st.api_key?.done },
    { id: "profile", label: "Your field & name", done: !!st.field?.done },
    { id: "filters", label: "Hard limits", done: !!st.hard_filters?.done },
    { id: "matrix", label: "Fulfillment matrix", done: !!st.matrix?.done },
    { id: "wishlist", label: "Wish list", done: !!st.wishlist?.done },
    { id: "company", label: "A company to track", done: !!st.company?.done },
  ]
    .map(
      ({ id, label, done }) => `
      <li class="wb-row">
        <span class="wb-mark${done ? " wb-mark-done" : ""}" aria-hidden="true">${done ? "✓" : ""}</span>
        <span class="wb-label">${label}</span>
        <button class="btn btn-ghost wb-go" data-action="jump" data-step="${id}">${done ? "Revisit" : "Finish"}</button>
      </li>`
    )
    .join("");
  return `
    <h1 class="wizard-title">Welcome back</h1>
    <p class="wizard-lead">${ob ? `${ob.complete_count} of ${ob.total} setup steps are done. ` : ""}Pick
      up wherever you like — each step saves on Continue, and nothing you've already set
      changes unless you change it.</p>
    <ul class="wb-list">${rows}</ul>
    ${
      roadmapPayload().wishlist.length || roadmapPayload().matrix
        ? `<p class="wizard-hint">${hintIcon()}Your matrix and wish-list words can become scoring
          guidance the scorer reads — <a class="wizard-link" href="#/settings" data-action="go-synthesis">Settings → Scoring</a>.</p>`
        : ""
    }
    <p class="wizard-hint">${hintIcon()}Optional, and not part of the count: the voice guide shapes how
      the AI sounds when it drafts in your voice — refine it in
      <a class="wizard-link" href="#/settings" data-action="go-voice">Settings → System</a>.</p>`;
}

function keyStep() {
  const st = s.keyStatus;
  const shadowed = !!st && st.configured && st.editable === false;
  // Three states: a configured key shows Test/Change (not the raw field); an
  // env-shadowed key can't be changed here at all; otherwise the editing view.
  const editing = s.changingKey || !st?.configured;
  let result = "";
  if (s.keyResult === "testing") result = `<p class="wizard-note">Testing…</p>`;
  else if (s.keyResult?.ok) result = `<p class="wizard-note wizard-ok">The key works.</p>`;
  else if (s.keyResult?.error) result = `<p class="wizard-note wizard-err">${esc(s.keyResult.error)}</p>`;
  else if (st?.configured && !editing)
    // A saved key that last tested 401 gets a standing warning, not the reassuring
    // "a key is set" — so a rejected key never silently reads as AI-on (#33).
    result = s.onboarding?.api_key_rejected
      ? `<p class="wizard-note wizard-err">This key was rejected (401) when last tested — replace it, or test it again.</p>`
      : `<p class="wizard-note wizard-ok">A key is set (${esc(st.masked || "•••")}).</p>`;
  const testing = s.keyResult === "testing";
  const compat = s.aiProviderChoice === "openai_compat";
  // "I don't want to use AI" declines the whole feature, not just the key —
  // while checked, the endpoint path greys out too (only meaningful when
  // neither provider is set up, the same gate the checkbox itself renders on).
  const declinedAll = !st?.configured && !s.aiProviders?.configured && s.apiKeyDeclined === true;
  const chooser = `
    <div class="theme-seg wizard-provider-seg" role="group" aria-label="AI provider">
      <button type="button" data-action="ai-choice" data-value="anthropic" aria-pressed="${!compat}">Anthropic (Claude)</button>
      <button type="button" data-action="ai-choice" data-value="openai_compat" aria-pressed="${compat}"${declinedAll ? " disabled" : ""}>My own endpoint</button>
    </div>`;
  if (compat) {
    return `
    <h1 class="wizard-title">Turn on AI${badge("key")}</h1>
    <p class="wizard-lead">jshq scores each posting against your criteria, explains why, and
      drafts outreach and cover letters in your voice — on Anthropic with your own API key,
      or on any OpenAI-compatible endpoint you run or trust (Ollama, LM Studio, a hosted
      provider). Without either, everything else still works — postings are pulled and
      filtered as normal.</p>
    ${chooser}
    ${compatPane()}
    <p class="wizard-fineprint">The URL is saved on this machine; the optional key goes to a .env file in your data directory. Job text and your documents are sent only to this endpoint, for the tasks it runs — a localhost URL keeps everything on this machine.</p>`;
  }
  const body = editing ? keyEditView(st, shadowed, testing) : keySetView(shadowed, testing);
  return `
    <h1 class="wizard-title">Turn on AI${badge("key")}</h1>
    <p class="wizard-lead">jshq scores each posting against your criteria, explains why, and
      drafts outreach and cover letters in your voice — on Anthropic with your own API key,
      or on any OpenAI-compatible endpoint you run or trust (Ollama, LM Studio, a hosted
      provider). Without either, everything else still works — postings are pulled and
      filtered as normal.</p>
    ${chooser}
    ${body}
    <div aria-live="polite">${result}
    ${shadowed
      ? `<p class="wizard-note">Your key comes from an environment variable, which wins over
           anything saved here — manage it in your shell. AI is already on.</p>`
      : ""}</div>
    <p class="wizard-fineprint">Saved to a .env file in your data directory on this machine, and sent only to api.anthropic.com.</p>`;
}

/* The OpenAI-compatible pane of the Turn-on-AI step (2026-08-22, owner
   direction: the endpoint must be choosable here, not only in Settings).
   Save & test writes the endpoint, points every AI task at it (both axes on
   the typed model id), then probes GET /models — the probe is informational:
   only a failed SAVE blocks, matching the key pane's rule. */
function compatPane() {
  const p = s.aiProviders;
  let result = "";
  if (s.compatResult === "testing") result = `<p class="wizard-note">Testing…</p>`;
  else if (s.compatResult?.ok)
    result = `<p class="wizard-note wizard-ok">The endpoint works${s.compatResult.models?.length ? ` — serving ${s.compatResult.models.length} model${s.compatResult.models.length === 1 ? "" : "s"}` : ""}. AI tasks run on it.</p>`;
  else if (s.compatResult?.error)
    result = `<p class="wizard-note wizard-err">${esc(s.compatResult.error)}</p>`;
  else if (p?.configured)
    result = `<p class="wizard-note wizard-ok">An endpoint is set (${esc(p.base_url || "")}).</p>`;
  const busy = s.compatResult === "testing";
  return `
    <div class="form-field">
      <label for="wiz-compat-url">Endpoint base URL</label>
      <input id="wiz-compat-url" type="text" data-field="compatUrl" placeholder="http://localhost:11434/v1" value="${esc(s.compatUrl)}" autocomplete="off" spellcheck="false" data-autofocus />
    </div>
    <div class="form-field">
      <label for="wiz-compat-key">API key (optional — local runtimes have none)</label>
      <input id="wiz-compat-key" type="password" data-field="compatKey" autocomplete="off" />
    </div>
    <div class="form-field">
      <label for="wiz-compat-model">Model id</label>
      <input id="wiz-compat-model" type="text" data-field="compatModel" placeholder="e.g. llama3.3" value="${esc(s.compatModel)}" list="wiz-compat-models" autocomplete="off" spellcheck="false" />
      <datalist id="wiz-compat-models">${(s.compatModels || []).map((id) => `<option value="${esc(id)}"></option>`).join("")}</datalist>
    </div>
    <div class="wizard-inline">
      <button class="btn" data-action="save-test-endpoint"${busy ? " disabled" : ""}>Save &amp; test</button>
    </div>
    <div aria-live="polite">${result}</div>`;
}

/* A key is already set: no raw field, just verify or begin a replacement. Change
   stays enabled even when the key is env-shadowed — clicking it reveals the edit
   view, where the field/Save are disabled and the env notice explains why it
   can't be changed here (matching Settings). A dead greyed button read as broken. */
function keySetView(shadowed, testing) {
  return `
    <div class="wizard-inline">
      <button class="btn" data-action="test-key"${testing ? " disabled" : ""}>Test API</button>
      <button class="btn btn-ghost" data-action="change-key"${testing ? " disabled" : ""}>Change API key</button>
    </div>`;
}

/* Editing (no key yet, or "Change API key" was pressed): the field + Save & test
   + Get a key, plus a Cancel back to the key-set view whenever a key is already
   stored (including the shadowed case, so Change is never a one-way trap). */
function keyEditView(st, shadowed, testing) {
  const cancel = st?.configured
    ? `<button class="btn btn-ghost" data-action="cancel-change-key"${testing ? " disabled" : ""}>Cancel new API</button>`
    : "";
  // The decline option only makes sense with no AI at all — no key AND no
  // endpoint (either one means AI is already on). Checking it disables the
  // field + Save (unchecking re-enables) and completes the api_key setup step.
  const noKey = !st?.configured && !s.aiProviders?.configured;
  const declined = noKey && s.apiKeyDeclined === true;
  const fieldOff = shadowed || declined;
  const saveOff = shadowed || testing || declined;
  return `
    <div class="form-field">
      <label for="wiz-key">Anthropic API key</label>
      <input id="wiz-key" type="password" data-field="key" placeholder="sk-ant-…" value="${esc(s.key)}" autocomplete="off"${fieldOff ? " disabled" : ""}${declined ? "" : " data-autofocus"} />
    </div>
    <div class="wizard-inline">
      <button class="btn" data-action="save-test-key"${saveOff ? " disabled" : ""}>Save &amp; test</button>
      <a class="wizard-link" href="https://platform.claude.com/settings/keys" target="_blank" rel="noopener">Get a key →</a>
      ${cancel}
    </div>
    ${
      noKey
        ? `<label class="wizard-check wizard-decline">
             <input type="checkbox" data-action="toggle-decline-key" ${declined ? "checked" : ""} />
             I don't want to use AI
           </label>
           ${
             declined
               ? `<p class="wizard-note wizard-err">AI features stay off: postings aren't scored or explained, and there are no AI-drafted messages, cover-letter tailoring, or job-URL prefill. Everything else — pulling, filtering, tracking — works as normal. Uncheck this to set up a provider.</p>`
               : ""
           }`
        : ""
    }`;
}

function profileStep() {
  return `
    <h1 class="wizard-title">What are you looking for?${badge("profile")}</h1>
    <p class="wizard-lead">This is the one answer that tunes scoring to you: postings in a
      different discipline get flagged instead of blending in.</p>
    ${criteriaBanner({ wordsSafe: false })}
    <div class="form-field">
      <label for="wiz-field">The kind of roles you're searching for</label>
      <input id="wiz-field" type="text" data-field="field" placeholder="e.g. backend engineering, product design, data science" value="${esc(s.field)}" data-autofocus${errAttrs("field")} />
      ${fieldError("field")}
      <p class="wizard-hint">${hintIcon()}jshq matches these words against job titles and pulls in the
        postings that contain them, so use terms that appear in real titles. A few comma-separated
        role words is plenty — this same answer becomes the one-line description of your search,
        so keep it short rather than exhaustive.</p>
      <details class="wizard-example">
        <summary>See examples</summary>
        <div class="wizard-example-body">
          <ul class="wizard-example-list">
            <li>"product manager, product" pulls in Product Manager, Senior Product Manager, Director of Product.</li>
            <li>"nurse, nursing, RN" pulls in Registered Nurse, Nurse Manager, RN Case Manager.</li>
            <li>"data, analytics" pulls in Data Analyst, Data Scientist, Analytics Engineer.</li>
            <li>"project management, program management, PMO" pulls in Project Manager, Program
              Manager, PMO Director. Keep it to the role family — certifications like PMP rarely
              appear in job titles.</li>
          </ul>
          <p class="wizard-hint">${hintIcon()}You can refine these any time in Settings → Sourcing.</p>
        </div>
      </details>
    </div>
    <div class="form-field">
      <label for="wiz-name">Your name</label>
      <input id="wiz-name" type="text" data-field="name" placeholder="e.g. Sam Lee" value="${esc(s.name)}"${errAttrs("name")} />
      ${fieldError("name")}
      <p class="wizard-hint">${hintIcon()}Goes on the drafts the AI writes. Leave it blank to stay anonymous.</p>
    </div>`;
}

function filtersStep() {
  return `
    <h1 class="wizard-title">Your hard limits${badge("filters")}</h1>
    <p class="wizard-lead">These filter postings out before scoring — a floor you won't go
      below, and where you can work. Leave them empty and nothing is filtered.</p>
    ${criteriaBanner()}
    <div class="form-field">
      <label for="wiz-comp">Minimum base salary (USD)</label>
      <input id="wiz-comp" type="text" inputmode="numeric" data-field="compFloor" placeholder="e.g. 150000 or 150k" value="${esc(s.compFloor)}" data-autofocus${errAttrs("compFloor")} />
      ${fieldError("compFloor")}
    </div>
    <label class="wizard-check">
      <input type="checkbox" data-field="remoteUs" ${s.remoteUs ? "checked" : ""} />
      <span>Remote roles anywhere in the US are fine</span>
    </label>
    <div class="wizard-cols">
      <div class="form-field">
        <label for="wiz-home">Home base (town, ST)</label>
        <input id="wiz-home" type="text" data-field="homeTown" placeholder="e.g. Evanston, IL" value="${esc(s.homeTown)}"${errAttrs("homeTown")} />
        ${fieldError("homeTown")}
      </div>
      <div class="form-field">
        <label for="wiz-drive">Longest drive you'd accept (minutes)</label>
        <input id="wiz-drive" type="text" inputmode="numeric" data-field="driveMins" placeholder="e.g. 30" value="${esc(s.driveMins)}" />
      </div>
    </div>
    <p class="wizard-hint">${hintIcon()}Together these draw a drive-time circle around you — onsite
      postings outside it get filtered. US towns only for now; leave home blank to skip.</p>
    <div class="form-field">
      <label for="wiz-towns">Other towns that work anyway (comma-separated)</label>
      <input id="wiz-towns" type="text" data-field="locations" placeholder="e.g. Chicago, Oak Park" value="${esc(s.locations)}" />
    </div>
    ${s.ruleTowns.length ? `<p class="wizard-hint">${hintIcon()}Also always included, from your
      inclusion rules: ${esc(s.ruleTowns.join(", "))}. Edit those in Settings → Sourcing.</p>` : ""}
    <div class="form-field">
      <label for="wiz-sectors">Sectors to exclude (comma-separated)</label>
      <input id="wiz-sectors" type="text" data-field="sectors" placeholder="e.g. gambling, tobacco, defense" value="${esc(s.sectors)}" />
      <p class="wizard-hint">${hintIcon()}A posting whose company is tagged with one of these is filtered out
        before scoring — a hard limit, like salary.</p>
    </div>
    <fieldset class="form-field wizard-bands">
      <legend>Target seniority (optional)</legend>
      <div class="wizard-band-grid">
        ${levelBands()
          .map(
            (b) => `<label class="wizard-check"><input type="checkbox" data-action="toggle-band" data-band="${esc(b.value)}" ${s.titleBands.includes(b.value) ? "checked" : ""} /><span>${esc(b.label)}</span></label>`
          )
          .join("")}
      </div>
      <p class="wizard-hint">${hintIcon()}The levels you're aiming for. Roles at other levels still appear —
        set which ones to downrank in Settings → Scoring.</p>
    </fieldset>`;
}

function matrixStep() {
  // A11Y (#45): name the textarea by heading + the two axes it sits on
  // (energizing/draining × strength/growth), and link the prompt via
  // aria-describedby so it isn't an orphaned <div>. Key is "<row>_<col>".
  const cell = ([key, heading, prompt, example]) => {
    const [row, col] = key.split("_");
    const axis = `${row}, ${col === "strength" ? "a strength" : "a growth area"}`;
    return `
    <div class="matrix-cell">
      <div class="matrix-cell-head">${heading}</div>
      <p class="matrix-cell-prompt" id="matrix-prompt-${key}">${prompt}</p>
      <textarea data-field="matrix.${key}" aria-label="${heading} — ${axis}" aria-describedby="matrix-prompt-${key}" rows="4" placeholder="${example}">${esc(s.matrix[key])}</textarea>
    </div>`;
  };
  return `
    <h1 class="wizard-title">The fulfillment matrix${badge("matrix")}</h1>
    <p class="wizard-lead">A quick self-check before you rank anything. Think of the actual
      work you've done — concrete activities, not adjectives — and sort it two ways: does it
      energize or drain you, and are you good at it yet? A few entries per cell is plenty,
      in whatever words come naturally.</p>
    ${roadmapBanner()}
    <details class="wizard-example">
      <summary>Why this exercise?</summary>
      <div class="wizard-example-body">
        <p class="wizard-hint">${hintIcon()}The top-left cell is what your next role should be built
          around — the work that is both yours and good for you. The bottom-left is the
          trap: being good at something invites more of it, even when it drains you, and
          naming it is how you stop accepting it. The next step turns these answers into
          your ranked wish list, and your exact words are kept for a later AI-assisted
          pass at your criteria.</p>
      </div>
    </details>
    <div class="matrix-grid">
      <div class="matrix-axis matrix-axis-tl"></div>
      <div class="matrix-axis">Strength</div>
      <div class="matrix-axis">Growth area</div>
      <div class="matrix-axis matrix-axis-row">Energizing</div>
      ${cell(MATRIX_CELLS[0])}
      ${cell(MATRIX_CELLS[1])}
      <div class="matrix-axis matrix-axis-row">Draining</div>
      ${cell(MATRIX_CELLS[2])}
      ${cell(MATRIX_CELLS[3])}
    </div>`;
}

function wishlistStep() {
  const items = s.wishlist
    .map(
      (item, i) => `
      <li class="wish-item">
        <span class="wish-rank">${i + 1}</span>
        ${
          i === s.editingWish
            ? `<input class="wish-edit-input" data-field="editDraft" aria-label="Edit criterion" value="${esc(s.editDraft)}" />`
            : `<button type="button" class="wish-text" data-action="wish-edit" data-i="${i}" title="Click to edit">${esc(item.text)}</button>`
        }
        <label class="wish-weight" title="Importance weight (1 = normal, 2 = counts ~twice)">
          <span class="wish-weight-label">Weight</span>
          <input type="number" data-wish-weight data-i="${i}" min="0.25" max="4" step="0.25" value="${item.weight}" aria-label="Weight for criterion ${i + 1}" />
        </label>
        <span class="wish-actions">
          <button class="wish-btn" data-action="wish-up" data-i="${i}" title="Move up" aria-label="Move criterion ${i + 1} up" ${i === 0 ? "disabled" : ""}><span aria-hidden="true">↑</span></button>
          <button class="wish-btn" data-action="wish-down" data-i="${i}" title="Move down" aria-label="Move criterion ${i + 1} down" ${i === s.wishlist.length - 1 ? "disabled" : ""}><span aria-hidden="true">↓</span></button>
          <button class="wish-btn" data-action="wish-remove" data-i="${i}" title="Remove" aria-label="Remove criterion ${i + 1}"><span aria-hidden="true">✕</span></button>
        </span>
      </li>`
    )
    .join("");
  return `
    <h1 class="wizard-title">Your wish list${badge("wishlist")}</h1>
    <p class="wizard-lead">What matters most in your next role? Add each one in your own
      words, most important first — the order sets how heavily each counts when postings
      are scored, shown as the weight beside each, which is yours to fine-tune. Your matrix
      answers are good raw material.</p>
    ${criteriaBanner()}
    ${roadmapBanner()}
    <details class="wizard-example"${s.exampleOpen ? " open" : ""}>
      <summary data-action="toggle-example">See a filled-in example</summary>
      <div class="wizard-example-body">${s.exampleHtml || ""}</div>
    </details>
    <div class="wish-add">
      <input type="text" data-field="wishDraft" aria-label="Add a criterion" placeholder="e.g. Sustainable pace, real decision authority" value="${esc(s.wishDraft)}" data-autofocus />
      <button class="btn" data-action="wish-add">Add</button>
    </div>
    ${s.wishlist.length ? `<ol class="wish-list">${items}</ol>` : `<p class="wizard-empty">No criteria yet — add a few above.</p>`}
    <p class="wizard-hint">${hintIcon()}Marking one item as your central trade-off, or another as
      upside-only, happens later in Settings → Scoring — the ranking here is what scoring starts from.</p>`;
}

/* What's already on the board, as evidence beside the "required" badge — a
   returning user (or a Back after adding) must never re-add just because the
   step LOOKS unmet. Same ruled-row format as the welcome-back hub. */
function companyBoardHtml() {
  if (!s.companies.length) return "";
  const rows = s.companies
    .map((c) => {
      const n = c.active_job_count ?? 0;
      return `
      <li class="wb-row">
        <span class="wb-label">${esc(c.name)}</span>
        <span class="wizard-hint">${n ? `${n} active job${n === 1 ? "" : "s"}` : esc(c.ats_last_status === "checking" ? "checking…" : c.website || "")}</span>
      </li>`;
    })
    .join("");
  return `
    <p class="wizard-hint">Already on your board — the required part is done:</p>
    <ul class="wb-list">${rows}</ul>`;
}

function companyStep() {
  const required = companyRequired();
  return `
    <h1 class="wizard-title">${required ? "Add your first company" : "Add a company"}
      ${required ? '<span class="wizard-required">required</span>' : '<span class="wizard-optional">optional</span>'}</h1>
    ${companyBoardHtml()}
    <p class="wizard-lead">${required
      ? "Pick a company you'd want to work for. jshq looks for its job board and pulls the openings it can, then scores them against everything you just set up."
      : "Add another if you like — each company's board gets watched the same way."}</p>
    <p class="form-req-note"><span class="req-mark" aria-hidden="true">*</span> required</p>
    <div class="form-field">
      <label for="wiz-company">Company name <span class="req-mark" aria-hidden="true">*</span></label>
      <input id="wiz-company" type="text" data-field="companyName" placeholder="e.g. Discord" value="${esc(s.companyName)}" aria-required="true" data-autofocus${errAttrs("companyName")} />
      ${fieldError("companyName")}
    </div>
    <div class="wizard-cols">
      <div class="form-field">
        <label for="wiz-website">Website (optional)</label>
        <input id="wiz-website" type="text" data-field="companyUrl" placeholder="e.g. discord.com" value="${esc(s.companyUrl)}" />
      </div>
      <div class="form-field">
        <label for="wiz-careers">Careers page (optional)</label>
        <input id="wiz-careers" type="text" data-field="companyCareers" placeholder="e.g. discord.com/careers" value="${esc(s.companyCareers)}" />
        <div id="wiz-careers-status" class="wizard-careers-status" aria-live="polite">${careersStatusHtml()}</div>
      </div>
    </div>
    <p class="wizard-hint wizard-hint-spaced">${hintIcon()}Either one lets jshq look for their job board — a direct careers link works
      best. Not every company can be read automatically (custom or gated career sites); those you
      track by hand, and jshq still helps. Without a URL there's nothing to look up.</p>
    <div class="wizard-cols">
      <div class="form-field">
        <label for="wiz-location">Location (optional)</label>
        <input id="wiz-location" type="text" data-field="companyLocation" placeholder="e.g. Chicago / remote" value="${esc(s.companyLocation)}" />
      </div>
      <div class="form-field">
        <label for="wiz-priority">Priority (optional)</label>
        <select id="wiz-priority" data-field="companyPriority">
          <option value="">—</option>
          ${[1, 2, 3, 4, 5].map((n) => `<option value="${n}"${String(n) === s.companyPriority ? " selected" : ""}>${n}</option>`).join("")}
        </select>
      </div>
    </div>
    <div class="wizard-cols">
      <div class="form-field">
        <label for="wiz-status">Status</label>
        <select id="wiz-status" data-field="companyStatus">
          ${STATUSES.map((v) => `<option value="${esc(v)}"${v === s.companyStatus ? " selected" : ""}>${esc(v)}</option>`).join("")}
        </select>
      </div>
      <div class="form-field">
        <label for="wiz-values">Values fit (optional)</label>
        <select id="wiz-values" data-field="companyValues">
          <option value="">—</option>
          ${VALUES_FIT.map((v) => `<option value="${esc(v)}"${v === s.companyValues ? " selected" : ""}>${esc(v)}</option>`).join("")}
        </select>
      </div>
    </div>`;
}

/* The careers-probe status — a field helper anchored under the Careers input
   (its own node so it updates in place, no step repaint that would steal focus
   from a field the user tabbed into). Plain helper text, not a tip: no icon,
   small and muted. */
function careersStatusHtml() {
  const cs = s.careersSearch;
  if (cs === "searching")
    return `<span class="careers-status">Looking for their job board…</span>`;
  if (cs?.found)
    return `<span class="careers-status careers-status-ok">Found their job board — filled in above.</span>`;
  if (cs?.error)
    return `<span class="careers-status">Couldn't check for a job board just now — add a careers link if you have one, or edit the website to retry.</span>`;
  if (cs && !cs.found)
    return `<span class="careers-status">No job board found automatically — add a careers link if you have one.</span>`;
  return "";
}

/* Reflect careersSearch/companyCareers into the live DOM without a full repaint:
   swap the status line's markup, and mirror an auto-filled careers value into
   its input. Fill even when the input is focused IF it's still empty (the user
   tabbed in but hasn't typed) — that focused-and-empty case is exactly why the
   field stayed blank while the status claimed it was filled. Only a non-empty
   focused field (they're typing their own) is left alone. */
function paintCareers() {
  if (!owned() || STEPS[s.step] !== "company") return;
  const line = container.querySelector("#wiz-careers-status");
  if (line) line.innerHTML = careersStatusHtml();
  const input = container.querySelector('[data-field="companyCareers"]');
  const typing = input && document.activeElement === input && input.value !== "";
  if (input && !typing && input.value !== s.companyCareers) {
    input.value = s.companyCareers;
  }
}

/* Derive a careers/board URL from the typed website (the no-write preview
   endpoint) and fill the optional Careers field. Fires on website blur and
   re-fires on a later edit. Never overwrites a hand-typed careers URL; a stale
   response (the website changed again mid-flight) is dropped. */
async function probeCareers() {
  syncFromDom();
  const website = s.companyUrl.trim();
  if (!website) return resetCareers();
  const careers = s.companyCareers.trim();
  // A hand-typed careers URL is authoritative — leave it and don't probe.
  if (careers && !s.careersAutofilled) return resetCareers();
  // Already resolved for this exact website — no need to hit the network again.
  // A failed probe doesn't count as resolved: the next blur retries it.
  if (
    s.careersFor === website &&
    s.careersSearch &&
    s.careersSearch !== "searching" &&
    !s.careersSearch.error
  )
    return;
  // A stale auto-fill from a previous website: clear it before the new probe.
  if (careers && s.careersAutofilled) {
    s.companyCareers = "";
    s.careersAutofilled = false;
  }
  s.careersFor = website;
  s.careersSearch = "searching";
  paintCareers();
  let res;
  try {
    res = await api.previewCareers({ name: s.companyName.trim(), website, careers_url: null });
  } catch {
    if (s.careersFor !== website) return; // superseded by a newer edit
    // A failed lookup is not "no board found" (F6) — say the check itself
    // didn't run, and let a later blur retry it.
    s.careersSearch = { error: true };
    return paintCareers();
  }
  if (s.careersFor !== website) return; // a newer probe owns the field now
  if (res.found && res.careers_url) {
    // Fill only if still empty — the user may have typed one while we searched.
    if (!s.companyCareers.trim()) {
      s.companyCareers = res.careers_url;
      s.careersAutofilled = true;
    }
    s.careersSearch = { found: true, url: res.careers_url };
  } else {
    s.careersSearch = { found: false };
  }
  paintCareers();
}

function resetCareers() {
  s.careersSearch = null;
  s.careersFor = "";
  paintCareers();
}

/* ---- the done step: a live payoff, not a static claim -------------------- */

/* companies.js's categorization, minus staleness (irrelevant seconds after
   creation). The done step must never say "being checked" when the backend
   isn't checking — a name-only company is never probed at all. */
function atsCategory(c) {
  const status = c?.ats_last_status;
  if (status === "checking") return "checking";
  if (status && status.startsWith("error:")) return "failing";
  if (!c?.ats_type || c.ats_type === "manual" || (status && status.startsWith("none"))) return "none";
  return "ok";
}

function atsLineHtml() {
  const c = s.createdCompany;
  const name = esc(c?.name || s.companyName);
  if (!c) return `Your dashboard is ready.`;
  if (!c.website && !c.careers_url) {
    return `<strong>${name}</strong> is on your board. Without a website its job board
      can't be found — add one from <a class="wizard-link" href="#/companies">Companies</a>
      and openings will pull automatically.`;
  }
  switch (atsCategory(c)) {
    case "checking":
      return `Looking for <strong>${name}</strong>'s job board now…`;
    case "ok": {
      const n = c.active_job_count ?? 0;
      return `Found <strong>${name}</strong>'s job board — ${n} opening${n === 1 ? "" : "s"} pulled.`;
    }
    case "none":
      return `<strong>${name}</strong> is on your board. Its postings can't be pulled
        automatically — plenty of career sites are custom or don't allow it — so you'll
        track this one by hand: add openings yourself and use the LinkedIn role checks from
        <a class="wizard-link" href="#/companies">Companies</a>. A direct careers URL added
        there gets it one more look.`;
    default:
      return `Couldn't reach <strong>${name}</strong>'s job board
        (${esc(failReason(c.ats_last_status))}) — check the URL from
        <a class="wizard-link" href="#/companies">Companies</a>.`;
  }
}

/* A key that is present AND not known-rejected. The "scoring/AI is on" copy
   keys on this, not raw `configured`: a key that last tested 401 is configured
   but useless, and must not imply scoring is live (#33). */
/* "AI is on": a usable Anthropic key OR a configured endpoint. The name
   predates the endpoint path; every done-step line keys off this. */
function keyUsable() {
  return (
    (!!s.keyStatus?.configured && !s.onboarding?.api_key_rejected) ||
    !!s.aiProviders?.configured
  );
}

/* What actually happens to incoming postings, given what THIS user set up —
   never a conditional promise ("if you added a key…") the UI could check
   itself. */
function scoringLine() {
  const hasKey = keyUsable();
  const hasWishlist = s.wishlist.length > 0;
  if (hasKey && hasWishlist)
    return "Every posting that comes in gets filtered by your hard limits and scored against your wish list.";
  if (hasKey)
    return "Postings get filtered by your hard limits. Scoring starts once you add a wish list — it's the one exercise still waiting.";
  return "Postings get filtered by your hard limits. Turn on AI later (an Anthropic key or your own endpoint, in Settings → System) to get scoring and drafts.";
}

const STEP_LABELS = {
  api_key: "API key",
  field: "your field",
  hard_filters: "hard limits",
  wishlist: "wish list",
  matrix: "fulfillment matrix",
};

/* Enumerate what is still open among the COUNTED steps — instead of gesturing at
   "anything you skipped". The voice guide is uncounted (optional, Settings-only),
   so it never lands here; voiceLineHtml points at it separately. */
function remainingHtml() {
  const st = s.onboarding?.steps;
  if (!st) return "";
  const open = Object.entries(STEP_LABELS)
    .filter(([k]) => !st[k]?.done)
    .map(([, label]) => label);
  if (!open.length)
    return `<p class="wizard-lead">Every setup step is done — nothing left but the search itself.</p>`;
  return `<p class="wizard-lead">Still open when you want ${open.length === 1 ? "it" : "them"}:
    ${esc(open.join(" · "))} — pick up any of it from the Setup pill up top.</p>`;
}

/* The synthesis pointer: only when raw words actually exist (roadmapPayload
   reflects both the hydrated roadmap and anything captured this walk). */
function synthesisLineHtml() {
  const p = roadmapPayload();
  if (!p.wishlist.length && !p.matrix) return "";
  const keyed = keyUsable();
  return `<p class="wizard-lead">Your matrix and wish-list words are kept verbatim.
    ${keyed ? "Have AI turn them" : "Turn them — with a key, or through any chat AI —"}
    into scoring guidance in <a class="wizard-link" href="#/settings" data-action="go-synthesis">Settings → Scoring</a>.</p>`;
}

/* A non-counted pointer to the voice guide (optional, AI-only, lives in
   Settings). The done step is the ONE first-run surface for it — the
   welcome-back hub only shows on return visits — but it only matters once AI is
   actually on, so it appears only when a key is set. */
function voiceLineHtml() {
  if (!keyUsable()) return "";
  return `<p class="wizard-lead">Optional, for later: the voice guide shapes how the AI sounds
    when it drafts outreach and cover letters in your voice — refine it in
    <a class="wizard-link" href="#/settings" data-action="go-voice">Settings → System</a>.</p>`;
}

/* First-class next actions on the done step. The marquee payoff — scored
   postings — never fires in session one for the privacy-minded persona who
   declines a key AND whose company has no readable board: no key ⇒ no scoring,
   no ATS ⇒ nothing pulled, so setup ends on an empty dashboard with no move to
   make. The honest lines above stay; these are the tappable ways to get value
   now. Plain anchors (hash navigation) for the two Companies deep links — the
   done step's existing #/settings link proves navigation off a satisfied
   first-run works — and the key jump reuses the go-* settings-tab handoff. */
function doneCtas() {
  const c = s.createdCompany;
  // "No auto-pull" = the same track-by-hand case atsLineHtml's none/no-website
  // branches describe: manual/undetected board, or no URL to look one up.
  const noAuto = !!c && ((!c.website && !c.careers_url) || atsCategory(c) === "none");
  const keyless = !keyUsable();
  if (!noAuto && !keyless) return ""; // auto-pull + a key: the payoff already fires
  // Both the "+ Add job" affordance and the LinkedIn role checks live on the
  // company's own detail page, so deep-link there (the real existing route);
  // fall back to the list if the id somehow isn't in hand.
  const companyHref = c ? `#/companies/${c.id}` : "#/companies";
  // #42: three peer next-actions, ONE component (.btn) so they read as siblings —
  // not two buttons plus a stray text link. The single highest-leverage action is
  // the accent primary: adding a key (turns on scoring) when keyless, else adding
  // the first job.
  const ctas = [];
  if (noAuto) {
    ctas.push(`<a class="btn${keyless ? "" : " btn-accent"}" href="${companyHref}">Add a job you found →</a>`);
    ctas.push(`<a class="btn" href="${companyHref}">Run the LinkedIn role checks →</a>`);
  }
  if (keyless) {
    ctas.push(`<a class="btn btn-accent" href="#/settings" data-action="go-key">Add an API key →</a>`);
  }
  // Lead copy states the point of the buttons, keyed to which case brought them
  // here (scoring already on vs. still off).
  const lead = noAuto
    ? `Get your first posting in by hand — it's scored ${keyless ? "the moment you add a key" : "the moment you add it"}.`
    : `One step turns on scoring for every posting that comes in:`;
  return `
    <p class="wizard-lead">${lead}</p>
    <div class="wizard-inline wizard-cta-row">${ctas.join("")}</div>`;
}

/* The schedule opt-in (pre-checked, owner call): Finish installs the OS
   scheduler entries — refresh twice a day, backup nightly — unless unticked.
   Hidden when this system has no supported scheduler or entries already
   exist; the checkbox mirrors into state without a repaint (toggle-band's
   focus rule). */
function scheduleOptInHtml() {
  const sch = s.schedule;
  if (!sch || !sch.supported) return "";
  if (sch.installed?.refresh || sch.installed?.backup) return "";
  return `
    <label class="wizard-check wizard-schedule-optin">
      <input type="checkbox" data-action="toggle-schedule" ${s.installSchedule ? "checked" : ""} />
      Keep this fresh automatically: check the boards twice a day and back up nightly. Writes a scheduler entry on this machine — change or remove it anytime in Settings → System.
    </label>`;
}

function doneStep() {
  return `
    <h1 class="wizard-title">You're set.</h1>
    <p class="wizard-lead" data-ats-line>${atsLineHtml()}</p>
    <p class="wizard-lead">${scoringLine()}</p>
    ${doneCtas()}
    ${scheduleOptInHtml()}
    ${synthesisLineHtml()}
    ${voiceLineHtml()}
    <div data-remaining>${remainingHtml()}</div>`;
}

/* Poll the created company until its check settles, updating the status line in
   place (node-level, so the Go button never loses focus). Self-clears when the
   user leaves the done step or the wizard. */
function startAtsPoll() {
  clearInterval(atsPoll);
  const id = s.createdCompany?.id;
  if (!id) return;
  atsPoll = setInterval(async () => {
    if (!owned() || STEPS[s.step] !== "done") return clearInterval(atsPoll);
    const c = await api.getCompany(id).catch(() => null);
    if (!c || !owned()) return;
    s.createdCompany = c;
    const line = container.querySelector("[data-ats-line]");
    if (line) line.innerHTML = atsLineHtml();
    if (atsCategory(c) !== "checking") clearInterval(atsPoll);
  }, 3000);
}

/* Fresh readiness for the remaining-steps list — the mount payload predates
   everything the walk just saved. Fire-and-forget from the company save. */
async function refreshDoneData() {
  const ob = await api.getOnboarding().catch(() => null);
  if (!ob || !owned()) return;
  s.onboarding = ob;
  const box = container.querySelector("[data-remaining]");
  if (box) box.innerHTML = remainingHtml();
}

/* ---- events ------------------------------------------------------------- */

function setField(el) {
  const field = el.dataset.field;
  if (!field) return;
  if (field.startsWith("matrix.")) s.matrix[field.slice(7)] = el.value;
  else if (el.type === "checkbox") s[field] = el.checked;
  else s[field] = el.value;
}

function onInput(e) {
  if (!owned()) return;
  // Live sync so a structural repaint keeps in-progress text; no repaint here so
  // focus/caret never jump.
  setField(e.target);
  const field = e.target.dataset.field;
  // Weights sync live too (they carry no data-field): a structural repaint —
  // the in-place editor's blur-commit, say — must never drop a half-typed one.
  if (e.target.matches?.("[data-wish-weight]")) {
    const item = s.wishlist[Number(e.target.dataset.i)];
    if (item) {
      // #31: any edit to the field is a deliberate pin — even re-affirming the
      // shown (ramped) value. touchedWeights then protects it from the rank
      // ramp, so the number saved is the number the user last set here.
      item.weight = readWeight(e.target.value);
      s.touchedWeights.add(item);
    }
    return;
  }
  // A settled key-test verdict describes a key that is no longer in the field —
  // drop it (and its note) the moment the input changes. Node removal, not a
  // repaint, so the caret stays put.
  if (field === "key" && s.keyResult && s.keyResult !== "testing") {
    s.keyResult = null;
    container.querySelector(".wizard-note")?.remove();
  }
  // A hand edit to the careers field makes it authoritative — the probe must
  // never overwrite it, and a later website edit must not clear it.
  if (field === "companyCareers") s.careersAutofilled = false;
  // Typing into an errored field clears its inline error on the next repaint.
  if (s.errors[field]) delete s.errors[field];
}

/* Read every input into state at action time (like settings.js's
   syncCriteriaFromDom). The authoritative read — robust even if an input event
   was missed — so Add / Continue always see what's on screen. */
function syncFromDom() {
  container.querySelectorAll("[data-field]").forEach(setField);
  container.querySelectorAll("[data-wish-weight]").forEach((el) => {
    const item = s.wishlist[Number(el.dataset.i)];
    if (!item) return;
    const w = readWeight(el.value);
    if (w !== item.weight) {
      item.weight = w;
      s.touchedWeights.add(item);
    }
  });
}

/* Same normalization as the Settings Tier 2 editor. */
function readWeight(v) {
  const n = Number(String(v).trim());
  if (!Number.isFinite(n) || n <= 0) return 1;
  return Math.min(4, Math.max(0.25, n));
}

function onKeydown(e) {
  if (!owned()) return;
  const field = e.target.dataset.field;
  if (e.key === "Escape" && field === "editDraft") {
    e.preventDefault();
    s.editingWish = -1; // cancel: the item keeps its original text
    s.editDraft = "";
    return paint();
  }
  if (e.key !== "Enter") return;
  // Enter acts only in single-line inputs; textareas keep it for newlines.
  if (!field || e.target.tagName === "TEXTAREA" || field.startsWith("matrix.")) return;
  e.preventDefault();
  syncFromDom();
  if (field === "editDraft") return commitEdit();
  if (field === "wishDraft") return addWish();
  if (field === "key") return saveTestKey(); // the field's own affordance
  next(); // everywhere else Enter is the step's primary action
}

/* Clicking away from the in-place editor commits it (Escape is the cancel).
   After an Enter/Escape already settled the edit, the input's teardown fires a
   stray focusout — commitEdit's editingWish guard makes that a no-op.

   When the blur is caused by PRESSING a control (Add, ↑/↓, ✕, Continue), the
   commit must NOT run here: focusout fires between mousedown and mouseup, and
   commitEdit's repaint detaches the pressed button, so the click never
   dispatches — the control silently did nothing and needed a second press.
   onPointerDown marks that case; onClick's commit-first branch then commits
   on the still-intact DOM before acting. */
let pointerOnAction = false;

function onPointerDown(e) {
  pointerOnAction = !!e.target.closest?.("[data-action]");
}

function onFocusOut(e) {
  if (!owned()) return;
  const field = e.target.dataset?.field;
  if (field === "editDraft") {
    setField(e.target);
    if (pointerOnAction) return; // the click's commit-first branch owns it
    commitEdit();
    return;
  }
  // Leaving the company website (with a real value) kicks off the careers-URL
  // probe — the same discovery add-time detection runs, one step earlier so the
  // field is filled before the company is created. Re-fires on a later edit.
  if (field === "companyUrl") probeCareers();
}

function commitEdit() {
  if (s.editingWish === -1) return;
  const text = s.editDraft.trim();
  // An emptied editor cancels — a blur must never silently DELETE an item;
  // the ✕ button is the explicit remove.
  if (text) s.wishlist[s.editingWish].text = text;
  s.editingWish = -1;
  s.editDraft = "";
  paint();
}

/* "Skip this step" must not silently eat typed-but-unsaved input — compare the
   step's fields against the hydrated/saved snapshot and say when something is
   being left behind. */
function stepDirty(id) {
  const h = s.hydrated;
  if (!h) return false;
  if (id === "key")
    return (
      !!s.key.trim() ||
      (s.aiProviderChoice === "openai_compat" && !s.aiProviders?.configured && !!s.compatUrl.trim())
    );
  if (id === "profile") return s.name.trim() !== h.name || s.field.trim() !== h.field;
  if (id === "filters")
    return (
      s.compFloor.trim() !== h.compFloor ||
      s.homeTown.trim() !== h.homeTown ||
      s.driveMins.trim() !== h.driveMins ||
      s.locations.trim() !== h.locations ||
      s.remoteUs !== h.remoteUs ||
      s.sectors.trim() !== h.sectors ||
      JSON.stringify(s.titleBands) !== h.titleBands
    );
  if (id === "matrix") return JSON.stringify(s.matrix) !== h.matrix;
  if (id === "wishlist")
    return !!s.wishDraft.trim() || JSON.stringify(s.wishlist) !== h.wishlist;
  if (id === "company")
    return !!s.companyName.trim() || !!s.companyUrl.trim() || !!s.companyCareers.trim();
  return false;
}

/* After a successful save the snapshot follows, so a later Skip on a re-visited
   step doesn't warn about input that WAS saved. */
function snapshotStep(id) {
  const h = s.hydrated;
  if (!h) return;
  if (id === "profile") {
    h.name = s.name.trim();
    h.field = s.field.trim();
  } else if (id === "filters") {
    h.compFloor = s.compFloor.trim();
    h.homeTown = s.homeTown.trim();
    h.driveMins = s.driveMins.trim();
    h.locations = s.locations.trim();
    h.remoteUs = s.remoteUs;
    h.sectors = s.sectors.trim();
    h.titleBands = JSON.stringify(s.titleBands);
  } else if (id === "matrix") {
    h.matrix = JSON.stringify(s.matrix);
  } else if (id === "wishlist") {
    h.wishlist = JSON.stringify(s.wishlist);
    h.matrix = JSON.stringify(s.matrix);
  }
}

async function onClick(e) {
  if (!owned()) return;
  pointerOnAction = false; // the press landed; focusout may commit again
  const btn = e.target.closest("[data-action]");
  if (!btn) return;
  syncFromDom();
  const action = btn.dataset.action;
  // Any action while the in-place editor is open commits it first, so an edit
  // is never lost to a reorder/Continue and indices stay true. (Clicks inside
  // the editor itself carry no data-action and never reach here.)
  if (s.editingWish !== -1 && action !== "wish-edit") commitEdit();
  if (action === "back") return go(s.step - 1);
  if (action === "skip") {
    if (stepDirty(STEPS[s.step])) toast("Skipped — nothing from this step was saved.");
    return go(s.step + 1);
  }
  if (action === "next") return next();
  if (action === "skip-all") return dismiss();
  if (action === "finish") return finish();
  if (action === "save-test-key") return saveTestKey();
  if (action === "test-key") return testKey();
  if (action === "toggle-decline-key") return toggleDeclineKey(btn.checked);
  if (action === "ai-choice") {
    s.aiProviderChoice = btn.dataset.value; // typed fields already synced above
    return paint();
  }
  if (action === "save-test-endpoint") return saveTestEndpoint();
  if (action === "toggle-schedule") {
    // Mirror into state without a repaint (see toggle-band's focus rule).
    s.installSchedule = btn.checked;
    return;
  }
  if (action === "toggle-band") {
    // The checkbox DOM already reflects the toggle; just mirror it into state
    // (no repaint — a repaint would fight focus as the user ticks several).
    const band = btn.dataset.band;
    s.titleBands = btn.checked
      ? [...new Set([...s.titleBands, band])]
      : s.titleBands.filter((b) => b !== band);
    return;
  }
  if (action === "change-key") {
    s.changingKey = true;
    s.keyResult = null; // the "a key is set" note gives way to the field
    return paint();
  }
  if (action === "cancel-change-key") {
    s.changingKey = false;
    s.key = "";
    s.keyResult = null;
    return paint();
  }
  if (action === "go-synthesis") {
    // Park the target tab, then let the anchor's own #/settings navigation run.
    sessionStorage.setItem("jshq-settings-tab", "scoring");
    return;
  }
  if (action === "go-voice") {
    // The voice guide lives in Settings → System; park that tab, let the
    // anchor navigate.
    sessionStorage.setItem("jshq-settings-tab", "system");
    return;
  }
  if (action === "go-key") {
    // The API key card is Settings → System too; park that tab, let the
    // done-step CTA's anchor navigate.
    sessionStorage.setItem("jshq-settings-tab", "system");
    return;
  }
  if (action === "wish-add") return addWish();
  if (action === "jump") {
    const target = STEPS.indexOf(btn.dataset.step);
    if (target > 0) go(target);
    return;
  }
  if (action === "toggle-example") {
    // Let the native <details> toggle run, then read where it landed and lazily
    // fetch the example into the (persistent) body node — no repaint, so the
    // disclosure doesn't jump.
    const details = btn.closest("details");
    setTimeout(() => {
      s.exampleOpen = details.open;
      if (details.open) loadExample(details.querySelector(".wizard-example-body"));
    }, 0);
    return;
  }
  if (action === "wish-edit") {
    commitEdit(); // an edit already open elsewhere lands before this one begins
    const i = Number(btn.dataset.i);
    s.editingWish = i;
    s.editDraft = s.wishlist[i].text;
    paint();
    const input = container.querySelector('[data-field="editDraft"]');
    if (input) {
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
    }
    return;
  }
  if (action === "wish-remove") {
    s.wishlist.splice(Number(btn.dataset.i), 1);
    s.orderChanged = true;
    applyRamp(); // the ranks below shifted — reflect the new weights live (#31)
    return paint();
  }
  if (action === "wish-up" || action === "wish-down") {
    const i = Number(btn.dataset.i);
    const j = action === "wish-up" ? i - 1 : i + 1;
    if (j < 0 || j >= s.wishlist.length) return;
    [s.wishlist[i], s.wishlist[j]] = [s.wishlist[j], s.wishlist[i]];
    s.orderChanged = true;
    applyRamp(); // the two ranks swapped — reflect the new weights live (#31)
    paint();
    // The repaint destroyed the pressed button and any motion with it: restore
    // focus to the moved item's same button (keyboard users nudge repeatedly)
    // and flash the row at its new rank — the same arrival cue a deep-linked
    // job row gets, so the eye tracks where the item went.
    flashRow(container.querySelectorAll(".wish-item")[j]);
    container.querySelector(`[data-action="${action}"][data-i="${j}"]`)?.focus();
    return;
  }
}

function go(step) {
  s.step = Math.max(0, Math.min(STEPS.length - 1, step));
  // Remember the place so a reload resumes here (#39). Best-effort — a locked-down
  // sessionStorage must never break navigation.
  try {
    sessionStorage.setItem(WIZARD_STEP_KEY, String(s.step));
  } catch {
    /* private mode / disabled storage — resume just won't be available */
  }
  // Transient per-step UI never leaks across a navigation: leaving the key step
  // mid-edit must not reopen the field on return, and a careers probe belongs to
  // the company step it ran in.
  s.changingKey = false;
  s.careersSearch = null;
  s.careersFor = "";
  paint();
}

/* Forget the resume breadcrumb — on every exit (dismiss/finish), so a later
   visit lands on the welcome-back hub, not a stale mid-wizard step. */
function clearResumeStep() {
  try {
    sessionStorage.removeItem(WIZARD_STEP_KEY);
  } catch {
    /* ignore */
  }
}

/* Push a non-empty "add a criterion" draft onto the list (no repaint). Returns
   true when it added. The single choke point so a typed-but-unadded draft is
   committed on Continue too — saveWishlist calls it, not just Enter/Add — rather
   than being silently dropped when the user types a criterion and hits Continue
   without pressing Add first. */
function commitWishDraft() {
  const text = s.wishDraft.trim();
  if (!text) return false;
  s.wishlist.push({ text, weight: 1.0, craft: false, bonus_only: false });
  s.orderChanged = true; // a new item changes every rank below it
  applyRamp(); // show each row its real ramped weight, not a placeholder 1 (#31)
  s.wishDraft = "";
  return true;
}

function addWish() {
  if (!commitWishDraft()) return;
  paint();
  // Flash the new row in (the jobs-list arrival cue) and hand focus back to
  // the add input — the repaint destroyed the one being typed in, and without
  // this an Enter-chain of adds went dead after the first.
  const rows = container.querySelectorAll(".wish-item");
  flashRow(rows[rows.length - 1]);
  container.querySelector('[data-field="wishDraft"]')?.focus();
}

function errMsg(err, fallback) {
  // FastAPI validation errors carry an array in .detail; never render "[object
  // Object]" at the user.
  return typeof err?.detail === "string" ? err.detail : fallback;
}

/* The shipped Alex Rivera reference, fetched on demand for the wishlist's
   "see a filled-in example" disclosure. Rendered as the ranked list itself
   (markers/weights stripped), not a markdown wall. */
function exampleListHtml(markdown) {
  const span = markdown.match(/<!-- tier2:start -->\n([\s\S]*?)\n<!-- tier2:end -->/);
  if (!span) return `<p class="wizard-empty">Couldn't read the example.</p>`;
  const items = [];
  for (const line of span[1].split("\n")) {
    const head = line.match(/^\s*\d+\.\s+(.*)/);
    if (head) items.push(head[1]);
    else if (items.length && line.trim()) items[items.length - 1] += " " + line.trim();
  }
  // Headline + first clause only: Alex's full items carry scoring-rubric
  // detail ("score +1 when…") that would bury the calibration this exists for
  // — what a good criterion LOOKS like, name plus a line of specifics.
  const clean = items.map((t) => {
    const flat = t
      .replace(/\s*\[(?:craft|bonus|w:[^\]]*)\]/gi, "")
      .replace(/\*\*/g, "")
      .trim();
    const [head, ...rest] = flat.split(" — ");
    let detail = rest.join(" — ");
    const stop = detail.search(/[.:;] /);
    if (stop > -1) detail = detail.slice(0, stop);
    if (detail.length > 90) detail = `${detail.slice(0, 90).replace(/\s+\S*$/, "")}…`;
    return { head: head.trim(), detail: detail.trim() };
  });
  if (!clean.length) return `<p class="wizard-empty">Couldn't read the example.</p>`;
  return `<p class="wizard-hint">${hintIcon()}A filled-in example — one design leader's ranked list:</p>
    <ol class="wizard-example-list">${clean
      .map((i) => `<li><strong>${esc(i.head)}</strong>${i.detail ? ` — ${esc(i.detail)}` : ""}</li>`)
      .join("")}</ol>
    <p class="wizard-hint">${hintIcon()}The same shape works in any field — a nurse manager might rank:</p>
    <ol class="wizard-example-list">
      <li><strong>Day shifts, predictable schedule</strong> — no rotating nights; overtime is the exception, not the staffing plan</li>
      <li><strong>A fully staffed unit</strong> — openings from growth, not chronic turnover</li>
      <li><strong>Teaching is part of the job</strong> — precepting students and mentoring new grads, on the clock</li>
      <li><strong>Union representation</strong> — a contract, a grievance process, annual steps</li>
    </ol>`;
}

async function loadExample(box) {
  if (!box) return;
  if (s.exampleHtml) {
    box.innerHTML = s.exampleHtml;
    return;
  }
  box.innerHTML = `<p class="wizard-empty">Loading…</p>`;
  try {
    const resp = await api.getCriteriaExample();
    s.exampleHtml = exampleListHtml(resp.markdown || "");
  } catch {
    box.innerHTML = `<p class="wizard-empty">Couldn't load the example.</p>`;
    return;
  }
  if (owned()) box.innerHTML = s.exampleHtml;
}

/* Store the key, then test it. Returns true when the key was SAVED — a failing
   TEST still returns true, because Continue must not gate on api.anthropic.com
   being reachable; the verdict line says what happened either way. */
async function storeAndTestKey(key) {
  try {
    await api.putApiKey(key);
  } catch (err) {
    s.keyResult = { error: errMsg(err, "Couldn't save the key.") };
    return false;
  }
  s.key = ""; // stored: the field clears, the status line carries the state
  try {
    s.keyResult = await api.testApiKey();
  } catch (err) {
    s.keyResult = { error: errMsg(err, "Saved, but the test call failed.") };
  }
  s.keyStatus = await api.getApiKeyStatus().catch(() => s.keyStatus);
  return true;
}

/* Persist the endpoint pane: save the URL (+ optional key), then point every
   AI task at it — both axes on the typed model id, the choice this pane
   announces. Returns false when either write is refused (a coded 422 rides
   the toast-free wizard-note instead). Never probes; see saveTestEndpoint. */
async function storeEndpoint() {
  const url = s.compatUrl.trim();
  const model = s.compatModel.trim();
  try {
    s.aiProviders = await api.putAiProviders(url, s.compatKey.trim() || undefined);
    await api.putAiModels(
      { provider: "openai_compat", model },
      { provider: "openai_compat", model }
    );
    s.compatSavedModel = model;
    return true;
  } catch (error) {
    s.compatResult = { ok: false, error: error.detail || error.message };
    return false;
  }
}

async function saveTestEndpoint() {
  if (!s.compatUrl.trim()) return toast("Enter the endpoint base URL first", { error: true });
  if (!s.compatModel.trim()) return toast("Enter the model id your endpoint serves", { error: true });
  s.compatResult = "testing";
  paint();
  if (!(await storeEndpoint())) return paint();
  await syncOnboarding(); // a configured endpoint completes the api_key step
  // The probe is informational — GET /models against the just-saved URL. A
  // failure warns but never blocks: the save already landed (the key pane's
  // only-a-failed-SAVE-blocks rule).
  try {
    const test = await api.testAiProviders();
    s.compatResult = test.ok ? { ok: true, models: test.models } : { ok: false, error: test.error };
    if (test.ok) s.compatModels = test.models || [];
  } catch (error) {
    s.compatResult = { ok: false, error: error.detail || error.message };
  }
  paint();
}

async function saveTestKey() {
  const key = s.key.trim();
  if (!key) return toast("Paste a key first", { error: true });
  s.keyResult = "testing";
  paint();
  const ok = await storeAndTestKey(key);
  if (ok) await syncOnboarding(); // a configured key completes the api_key step
  paint();
}

/* Re-pull the readiness payload so the key-step badge and the tracker reflect a
   just-changed api_key step (a saved key, or the decline toggle). */
async function syncOnboarding() {
  const ob = await api.getOnboarding().catch(() => null);
  if (ob && owned()) {
    s.onboarding = ob;
    s.apiKeyDeclined = ob.api_key_declined === true;
  }
  refreshOnboardingTracker();
}

/* Decline (or un-decline) using a key: keyless is a first-class mode. Repaint
   immediately so the field + Save disable and the warning show, persist the
   choice, then sync the badge/tracker (declining completes the api_key step;
   unchecking reopens it). Roll back on a failed save. */
async function toggleDeclineKey(declined) {
  s.apiKeyDeclined = declined;
  paint();
  try {
    await api.putSetting("api_key_declined", declined);
  } catch {
    s.apiKeyDeclined = !declined;
    paint();
    return toast("Couldn't save that preference", { error: true });
  }
  await syncOnboarding();
  paint();
}

/* Verify the already-stored key without changing it — the "Test API" affordance
   on the key-set view. The verdict ({ok} | {error}) surfaces verbatim through
   the shared keyResult render. */
async function testKey() {
  s.keyResult = "testing";
  paint();
  try {
    s.keyResult = await api.testApiKey();
  } catch (err) {
    s.keyResult = { error: errMsg(err, "The test call failed.") };
  }
  paint();
}

/* Save the current step's inputs (best-effort), then advance. Optional steps
   with nothing entered just advance. */
async function next() {
  if (s.saving) return; // Enter can arrive while a save is in flight
  const id = STEPS[s.step];
  s.saving = true;
  paint();
  try {
    if (id === "key") {
      if (s.aiProviderChoice === "openai_compat") {
        // Typed endpoint config must not be discarded by the primary button:
        // save it on the way through. A half-filled pane blocks with the
        // missing piece named; only a failed SAVE blocks otherwise.
        const url = s.compatUrl.trim();
        const model = s.compatModel.trim();
        const unchanged =
          url === (s.aiProviders?.base_url || "") && model === s.compatSavedModel;
        if ((url || model) && !unchanged) {
          if (!url || !model) {
            toast(!url ? "Enter the endpoint base URL first" : "Enter the model id your endpoint serves", { error: true });
            s.saving = false;
            return paint();
          }
          if (!(await storeEndpoint())) {
            s.saving = false;
            return paint();
          }
          await syncOnboarding();
        }
      } else {
        // A typed, unsaved key must not be discarded by the primary button: save
        // (and test) it on the way through. Only a failed SAVE blocks.
        const typed = s.key.trim();
        const editable = s.keyStatus?.editable !== false;
        if (typed && editable && !s.keyResult?.ok) {
          s.keyResult = "testing";
          paint();
          if (!(await storeAndTestKey(typed))) {
            s.saving = false;
            return paint();
          }
          // Refresh readiness so the tracker/badge reflect the just-saved key's
          // verdict on the way through — Continue used to skip this, so a rejected
          // key advanced still reading as done until a later mount (#33).
          await syncOnboarding();
        }
      }
    } else if (id === "profile") {
      if (!(await saveProfile())) {
        s.saving = false;
        paint();
        return focusFirstError();
      }
    } else if (id === "filters") {
      if (!(await saveFilters())) {
        s.saving = false;
        paint();
        return focusFirstError();
      }
    } else if (id === "wishlist") await saveWishlist();
    else if (id === "matrix") await saveMatrix();
    else if (id === "company") {
      if (!(await saveCompany())) {
        s.saving = false;
        paint();
        return focusFirstError();
      }
    }
    snapshotStep(id);
  } catch (err) {
    s.saving = false;
    paint();
    return toast(errMsg(err, "Couldn't save that step."), { error: true });
  }
  s.saving = false;
  go(s.step + 1);
  if (id === "company") startAtsPoll(); // the done step just painted — watch the check live
}

/* Discipline → practitioner noun by last-word suffix ("backend engineering" →
   "backend engineer", "product design" → "product designer"): unlocks the
   IC-ladder titles people actually hold. An unmapped suffix returns null and
   the IC bands fall back to the bare field words. */
const FIELD_NOUN_SUFFIXES = {
  engineering: "engineer",
  design: "designer",
  science: "scientist",
  development: "developer",
  research: "researcher",
  architecture: "architect",
  analytics: "analyst",
  analysis: "analyst",
  writing: "writer",
  management: "manager",
};

function fieldNoun(f) {
  const words = f.split(/\s+/);
  const noun = FIELD_NOUN_SUFFIXES[words[words.length - 1].toLowerCase()];
  return noun ? [...words.slice(0, -1), noun].join(" ") : null;
}

/* Band slug → title phrases for one field label (its practitioner noun along-
   side, when derivable). These feed LinkedIn people searches (quoted keywords,
   not exact titles), so phrases real titles contain beat grammatically clever
   ones. Slugs a custom level_bands doc adds aren't here — they're skipped;
   the anchors below keep the list useful anyway. */
const LINKEDIN_TITLE_TEMPLATES = {
  vp_plus: (f) => [`VP of ${f}`],
  senior_director: (f) => [`Senior Director of ${f}`],
  // Both orders: quoted searches are token-order-sensitive, and real titles
  // come in both ("Director of Product Design" AND "Product Design Director").
  director: (f) => [`Director of ${f}`, `${f} director`],
  // The comma form is the convention senior-manager titles actually use.
  senior_manager: (f) => [`Senior Manager, ${f}`],
  manager: (f) => [`${f} manager`],
  distinguished: (f, n) => [n ? `Distinguished ${n}` : f],
  principal: (f, n) => [n ? `Principal ${n}` : f],
  senior_staff: (f, n) => [n ? `Senior Staff ${n}` : f],
  staff: (f, n) => [n ? `Staff ${n}` : f],
  ic: (f, n) => (n ? [`Senior ${n}`, n] : [f]),
  junior: (f, n) => [n ? `Junior ${n}` : `Junior ${f}`],
};

/* Compose default LinkedIn role-check titles from the field answer × the
   selected seniority bands — offline, deterministic, keyless. Band titles come
   first in levelBands() order (most senior wins the cap), then per-field
   networking ANCHORS regardless of bands: the discipline's leadership and the
   people who carry it in their titles. Role checks exist to find people to
   talk to at a company, not only the title the user would hold — a single
   chip is too sparse to network with. Capped at 10 to match the combined
   search URL's MAX_COMBINED_TITLES. */
function deriveLinkedinTitles(field, bandSlugs) {
  const fields = field.split(",").map((t) => t.trim()).filter(Boolean);
  if (!fields.length) return [];
  const selected = new Set(bandSlugs);
  const seen = new Set();
  const out = [];
  const push = (t) => {
    const k = t.toLowerCase();
    if (!seen.has(k)) {
      seen.add(k);
      out.push(t);
    }
  };
  for (const band of levelBands()) {
    const tpl = LINKEDIN_TITLE_TEMPLATES[band.value];
    if (!selected.has(band.value) || !tpl) continue;
    for (const f of fields) tpl(f, fieldNoun(f)).forEach(push);
  }
  for (const f of fields) {
    push(`Head of ${f}`);
    const n = fieldNoun(f);
    if (n) push(n);
    push(f);
  }
  return out.slice(0, 10);
}

async function saveProfile() {
  const name = s.name.trim();
  const field = s.field.trim();
  // Mirror criteria.py's PERSONA_MAX_LEN (120) rail BEFORE any write. The
  // server 422s a long domain_label — but only after write_field has already
  // replaced the taxonomy with the paragraph, and the raw detail names
  // persona['domain_label'], an internal nothing on this screen is called.
  // Validate up front so nothing partial lands, in the input's own terms.
  if (field.length > 120) {
    s.errors.field =
      "Keep this under 120 characters — a few comma-separated role words that appear in real job titles, not a description.";
    return false;
  }
  if (name.length > 120) {
    s.errors.name = "Keep the name under 120 characters.";
    return false;
  }
  // Only a CHANGED field re-fires the taxonomy writer: write_field replaces the
  // whole taxonomy block with the minimal wizard one, so an untouched Continue
  // must not clobber vocabulary built up in Settings.
  if (field && field !== s.fieldLoaded) {
    await api.putDiscipline(field);
    // The same words become the first sourcing rule (the ingestion gate ships
    // empty, so nothing narrows the pull until this). Reserved id
    // "wizard-field" gives replace-on-rerun semantics; other rules and manual
    // chips are echoed back verbatim. Rule write LAST: if it throws, the step
    // toasts, fieldLoaded stays stale, and the next Continue re-runs both
    // writes (both are idempotent). A failed GET skips the rule quietly — the
    // hint under the input already points at Settings → Sourcing.
    const now = await api.getInclusionRules().catch(() => null);
    if (now) {
      const terms = field.split(",").map((t) => t.trim()).filter(Boolean);
      const rules = (now.rules || []).filter((r) => r.id !== "wizard-field");
      rules.push({ id: "wizard-field", verb: "include", target: "title", terms });
      const manual = {};
      for (const arr of ["title_keywords", "title_exclude_keywords", "location_allowlist"]) {
        manual[arr] = (now.compiled?.[arr] || [])
          .filter((e) => e.source === "manual")
          .map((e) => e.value);
      }
      await api.putInclusionRules({ rules, manual });
    }
    s.fieldLoaded = field; // discipline + rules landed; domainLabel commits after putPersona below
    // Seed the per-company LinkedIn role checks: real title phrases composed
    // from the same role words × the seniority bands (hydrated defaults at this
    // point — the filters step, which comes later, re-derives with the bands
    // the user actually confirms). Only when the default is still unset —
    // never clobber one the user has customized.
    const seededTitles = deriveLinkedinTitles(field, s.titleBands);
    if (seededTitles.length) {
      const cur = await api.getSetting("linkedin_title_defaults").catch(() => null);
      if (!cur) titleSeedFailed();
      else if (!(cur.value || []).length) {
        try {
          await api.putSetting("linkedin_title_defaults", seededTitles);
          s.seededLinkedinTitles = JSON.stringify(seededTitles);
        } catch {
          titleSeedFailed();
        }
      }
    }
  }
  if (name || field) {
    const domainLabel = field || s.domainLabel || "the roles you are searching for";
    await api.putPersona({ display_name: name || null, domain_label: domainLabel });
    // Commit only AFTER the write succeeds. This used to be assigned mid-save,
    // before putPersona — a rejected value stayed in s.domainLabel, where the
    // empty-field fallback silently RESENT it on the next Continue: the user
    // deleted the words from the input, and the save still "thought they were
    // there". Only a hard refresh cleared it.
    s.domainLabel = domainLabel;
  }
  return true;
}

/* Rank → weight (owner decision): the order the user just confirmed IS the
   importance signal. Linear ramp from 1.5 (top) to 0.75 (last), two decimals,
   inside the API's [0.25, 4] bounds; a single item keeps the 1.0 default (no
   relative rank to express). craft/bonus markers ride along untouched. */
function rampedTier2() {
  const n = s.wishlist.length;
  return s.wishlist.map((item, i) => ({
    ...item,
    weight: n < 2 ? 1.0 : Math.round((1.5 - (0.75 * i) / (n - 1)) * 100) / 100,
  }));
}

/* #31: make the ramp EAGER. After any structural change (add, remove, move),
   fold the rank ramp into item.weight for every item the user hasn't
   hand-weighted, so the number shown in the field is exactly the number
   finalTier2() saves — the control stops showing "1" while a descending ramp is
   what actually persists. Touched items keep their pinned weight; a lone item
   stays 1.0. Called only from the reorder handlers, so a returning user's
   untouched Continue never re-ramps (orderChanged stays false at mount). */
function applyRamp() {
  const ramped = rampedTier2();
  s.wishlist.forEach((item, i) => {
    if (!s.touchedWeights.has(item)) item.weight = ramped[i].weight;
  });
}

/* touchedWeights protects hand-set weights by object identity, but a RETURNING
   user's hydrated doc carries hand weights with no such mark — an empty set —
   so their first reorder/add would re-ramp every one away (finalTier2 sees
   nothing protected). Seed the set at mount from any hydrated weight that isn't
   what the rank ramp would produce for its current position: a ramp-matching
   weight stays re-rampable (indistinguishable from a ramp-derived one), while a
   weight chosen here or in Settings is protected, honoring "hand-set weights
   always survive" across sessions, not only within one. */
function seedTouchedWeights() {
  const ramped = rampedTier2();
  s.wishlist.forEach((item, i) => {
    if (item.weight !== ramped[i].weight) s.touchedWeights.add(item);
  });
}

/* Weight policy (owner decision, 2026-08-15): hand-set weights always survive;
   the rank ramp re-derives ONLY after an actual reorder (add, remove, move)
   and only for items never hand-weighted this session. An untouched Continue
   sends the list verbatim — it must round-trip, not quietly re-ramp weights
   set here or in Settings. */
function finalTier2() {
  if (!s.orderChanged) return s.wishlist;
  const ramped = rampedTier2();
  return s.wishlist.map((item, i) => (s.touchedWeights.has(item) ? item : ramped[i]));
}

/* Parse a salary the way people type it: "150000", "150,000", "$150k", "150k".
   Empty ⇒ 0 (no floor). Unparseable ⇒ null (inline error, never a silent $1
   floor from parseInt("1e5")). */
function parseComp(text) {
  const t = text.trim().toLowerCase().replace(/[$,\s]/g, "");
  if (!t) return 0;
  const m = t.match(/^(\d+(?:\.\d+)?)(k)?$/);
  if (!m) return null;
  return Math.round(parseFloat(m[1]) * (m[2] ? 1000 : 1));
}

async function saveFilters() {
  const floor = parseComp(s.compFloor);
  if (floor === null) {
    s.errors.compFloor = "Enter a number, like 150000 or 150k.";
    return false;
  }
  if (!s.tier1) return true; // banner already explains; nothing to write through
  const params = { ...s.tier1 };
  // Home base → a drive-time circle (location_radius). An unchanged label keeps
  // the resolved center (no re-geocode); a changed one resolves through the
  // bundled offline US place table, and an unknown town blocks with an inline
  // error rather than saving a filter that means nothing. Blank turns it off —
  // the same semantics as the Settings radius editor.
  const home = s.homeTown.trim();
  const mins = parseInt(s.driveMins, 10);
  const radius_minutes = Number.isInteger(mins) && mins >= 1 ? mins : 30;
  if (!home) {
    params.location_radius = null;
  } else if (home === (s.tier1.location_radius?.center?.label || "")) {
    params.location_radius = { ...s.tier1.location_radius, radius_minutes };
  } else {
    try {
      const hit = await api.geocode(home);
      params.location_radius = {
        center: { lat: hit.lat, lng: hit.lng, label: hit.label },
        radius_minutes,
        ...(s.tier1.location_radius?.estimate ? { estimate: s.tier1.location_radius.estimate } : {}),
      };
    } catch (err) {
      s.errors.homeTown =
        err.status === 404
          ? `Couldn't find "${home}" — try "Town, ST" (e.g. Madison, WI).`
          : errMsg(err, "Couldn't look that place up.");
      return false;
    }
  }
  params.comp_floor = floor;
  // Echo the rule-owned towns, refreshed at save time, under the manual ones
  // from the field: a criteria write must never clobber what the
  // inclusion-rules compiler owns (settings.js saveCriteria has the same
  // guard). Terms are lowercase on both sides, so the Set dedupes cleanly.
  const rulesNow = await api.getInclusionRules().catch(() => null);
  if (rulesNow?.compiled?.location_allowlist) {
    s.ruleTowns = rulesNow.compiled.location_allowlist
      .filter((e) => e.source === "rule")
      .map((e) => e.value);
  }
  const manualTowns = s.locations
    .split(",")
    .map((x) => x.trim().toLowerCase())
    .filter(Boolean);
  params.location_allowlist = [...new Set([...s.ruleTowns, ...manualTowns])];
  const regions = new Set(params.remote_regions || []);
  if (s.remoteUs) REMOTE_US_TOKENS.forEach((r) => regions.add(r));
  else REMOTE_US_TOKENS.forEach((r) => regions.delete(r));
  params.remote_regions = [...regions];
  // Sectors are a hard filter; target bands mark the levels you're aiming for
  // (every checkbox value comes from levelBands(), so it's an emittable band —
  // the loader never 422s on a name it can't produce). flag_title_bands (the
  // downrank map) rides along verbatim from s.tier1 — it's set in Settings.
  params.excluded_sectors = s.sectors
    .split(",")
    .map((x) => x.trim())
    .filter(Boolean);
  params.target_title_bands = [...s.titleBands];
  // Tier 2 rides along VERBATIM — this step never saw the list, so it must not
  // reweight or rebuild it.
  const resp = await api.putCriteria({ tier1_params: params, tier2_criteria: s.wishlist });
  s.tier1 = resp.tier1_params;
  // Merge the response INTO the existing item objects instead of adopting the
  // fresh ones: touchedWeights tracks items by OBJECT IDENTITY, so swapping
  // in response objects here silently dropped every hand-set weight's touched
  // mark — a later reorder then re-ramped the ×3 the owner-decision policy
  // exists to protect. The list rode verbatim, so the shapes line up 1:1.
  resp.tier2_criteria.forEach((item, i) => {
    if (s.wishlist[i]) Object.assign(s.wishlist[i], item);
  });
  // Reflect the canonical geocoded label ("evanston, il" → "Evanston, IL") so
  // the snapshot and a later Back-visit show what was actually stored.
  const savedRadius = resp.tier1_params.location_radius;
  s.homeTown = savedRadius?.center?.label || "";
  if (savedRadius?.radius_minutes) s.driveMins = String(savedRadius.radius_minutes);
  // The seniority bands are confirmed HERE, after the profile step seeded the
  // LinkedIn defaults with the hydrated ones. Re-derive with the bands the
  // user actually chose — but only over an empty setting or this session's own
  // seed, never over a list the user has hand-edited (in Settings → Sourcing
  // or a prior run). Best-effort: a failed write never blocks the step.
  const fieldNow = s.field.trim() || s.fieldLoaded;
  const derived = deriveLinkedinTitles(fieldNow, s.titleBands);
  if (derived.length) {
    const cur = await api.getSetting("linkedin_title_defaults").catch(() => null);
    if (!cur) titleSeedFailed();
    else {
      const curJson = JSON.stringify(cur.value || []);
      const owned = curJson === "[]" || curJson === s.seededLinkedinTitles;
      if (owned && curJson !== JSON.stringify(derived)) {
        try {
          await api.putSetting("linkedin_title_defaults", derived);
          s.seededLinkedinTitles = JSON.stringify(derived);
        } catch {
          titleSeedFailed();
        }
      }
    }
  }
  return true;
}

/* The title-defaults seed is best-effort by design (owner call: a failed
   write never blocks the step) but no longer silent (F6): it decides what
   the per-company LinkedIn role checks offer later, so a user should hear
   when it didn't land and where to fix it. */
function titleSeedFailed() {
  toast("Couldn't save the LinkedIn title defaults — you can set them in Settings → Sourcing.", {
    error: true,
  });
}

/* The raw-capture payload. Matrix cells ride only when one has content: the
   readiness signal must not count four empty strings as a done exercise, and a
   wishlist-only save must not plant them. The wishlist half preserves the
   roadmap's OWN stored words while the on-screen list is untouched since
   mount: the on-screen items are criteria texts, which synthesis or Settings
   may have refined — an untouched Continue on a revisit must not overwrite
   the "RAW inputs verbatim" store with derived prose. Editing any item makes
   the user's current words the new raw capture. */
function roadmapPayload() {
  const matrix = {};
  for (const [k, v] of Object.entries(s.matrix)) if (v.trim()) matrix[k] = v;
  const texts = s.wishlist.map((item) => item.text);
  const untouched =
    JSON.stringify(texts) === JSON.stringify(s.hydratedWishlistTexts);
  const body = { wishlist: untouched && s.rawWishlist ? s.rawWishlist : texts };
  if (Object.keys(matrix).length) body.matrix = matrix;
  return body;
}

/* Write the raw capture unless the mount-time GET failed (see initState). */
async function putRoadmapGuarded() {
  if (s.roadmapLoadFailed) return;
  const payload = roadmapPayload();
  if (payload.wishlist.length || payload.matrix) {
    await api.putRoadmap(payload);
    s.rawWishlist = payload.wishlist;
  }
}

async function saveWishlist() {
  // Commit a criterion typed into the add box but not yet Added: Continue must
  // save it, not drop it (the empty-ranking bug — a user types their wish list
  // and hits Continue expecting it to persist).
  commitWishDraft();
  // Raw words first: the roadmap write must never depend on the criteria doc
  // loading — it is the capture a later synthesis pass consumes.
  await putRoadmapGuarded();
  if (!s.tier1) return; // banner already explains the criteria write is parked
  const resp = await api.putCriteria({ tier1_params: s.tier1, tier2_criteria: finalTier2() });
  s.tier1 = resp.tier1_params;
  // Merge the response INTO the existing item objects, exactly as saveFilters
  // does. touchedWeights tracks items by OBJECT IDENTITY, so replacing s.wishlist
  // with the fresh response objects AND clearing the set (as this did) dropped
  // every hand-set weight's protection at once — a later reorder then re-ramped
  // the ×3 the weight policy exists to protect. The list rode as finalTier2(),
  // so the shapes line up 1:1.
  resp.tier2_criteria.forEach((item, i) => {
    if (s.wishlist[i]) Object.assign(s.wishlist[i], item);
  });
  // The saved weights are the new baseline, so an untouched Continue now
  // round-trips verbatim. The hand-set marks persist (identities preserved), so
  // a later reorder still re-ramps only the never-hand-weighted items.
  s.orderChanged = false;
}

async function saveMatrix() {
  await putRoadmapGuarded();
}

async function saveCompany() {
  const name = s.companyName.trim();
  if (!name) {
    s.errors.companyName = "Add a company name to finish setup.";
    // next() repaints and focuses the invalid field (focusFirstError).
    return false;
  }
  const body = { name, status: s.companyStatus || null };
  const url = s.companyUrl.trim();
  if (url) body.website = url;
  const careers = s.companyCareers.trim();
  if (careers) body.careers_url = careers;
  const location = s.companyLocation.trim();
  if (location) body.location = location;
  if (s.companyPriority) body.priority = Number(s.companyPriority);
  if (s.companyValues) body.values_fit = s.companyValues;
  try {
    s.createdCompany = await api.createCompany(body);
  } catch (err) {
    if (err.status !== 409) throw err;
    // Already on the board — the required step is SATISFIED, not failed. The
    // 409 carries the existing row's id (error.info, same shape as add-job);
    // reuse it so the done step reports its real status.
    s.createdCompany = err.info?.company_id
      ? await api.getCompany(err.info.company_id).catch(() => null)
      : null;
    if (s.createdCompany) s.companyName = s.createdCompany.name;
    toast(errMsg(err, "Already on your board — using the existing entry."));
  }
  // Keep the step's board list truthful for a Back-visit (and the skip/exit
  // affordances, which key on the count).
  if (s.createdCompany && !s.companies.some((c) => c.id === s.createdCompany.id)) {
    s.companies.push(s.createdCompany);
  }
  refreshDoneData(); // fire-and-forget: the done step's remaining-list catches up
  return true;
}

async function dismiss() {
  clearInterval(atsPoll);
  clearResumeStep();
  if (returning()) {
    // Not a dismissal — they finished (or skipped) setup long ago and are just
    // heading back.
    location.hash = "#/today";
    return;
  }
  try {
    await api.putOnboarding({ dismissed: true });
  } catch {
    /* non-fatal */
  }
  toast("Setup dismissed — pick it up anytime from the Setup pill up top.");
  location.hash = "#/today";
}

async function finish() {
  clearInterval(atsPoll);
  clearResumeStep();
  s.saving = true;
  paint();
  // The opt-in install, before the exit: non-fatal, but never silent — a
  // failure toasts with the scheduler's own words and Settings → System keeps
  // the control for another try.
  const sch = s.schedule;
  if (s.installSchedule && sch?.supported && !(sch.installed?.refresh || sch.installed?.backup)) {
    try {
      await api.installSchedule();
    } catch (error) {
      toast(error.detail || error.message, { error: true });
    }
  }
  try {
    await api.putOnboarding({ completed: true });
  } catch {
    /* non-fatal */
  }
  location.hash = "#/today";
}
