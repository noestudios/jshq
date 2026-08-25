/* Entry point: hash router + nav active state + stale-refresh trigger. */

import { api } from "./api.js";
import * as onboardingTracker from "./lib/onboardingTracker.js";
import { applyTheme } from "./lib/theme.js";
import { toast } from "./lib/ui.js";
import { loadVocab } from "./lib/vocab.js";
import { buzz, chime } from "./lib/notify.js";
import * as applications from "./views/applications.js";
import * as calendar from "./views/calendar.js";
import * as companies from "./views/companies.js";
import * as contacts from "./views/contacts.js";
import * as help from "./views/help.js";
import * as jobs from "./views/jobs.js";
import * as settings from "./views/settings.js";
import * as today from "./views/today.js";
import * as welcome from "./views/welcome.js";

const routes = { today, jobs, applications, companies, contacts, calendar, help, settings, welcome };
const DEFAULT_ROUTE = "today";

/* The #view-heading <h1> is retitled per route so every view has exactly one
   top-level heading (A11Y-02). Routes that render their own visible <h1> —
   Settings, Help, and the wizard (welcome) — are omitted, and the shared heading
   is hidden for them so the page never carries two <h1>s. */
const VIEW_HEADINGS = {
  today: "Today",
  jobs: "Jobs",
  applications: "Applications",
  companies: "Companies",
  contacts: "Contacts",
  calendar: "Calendar",
};

/* #46: keep that <h1> as the FIRST CHILD of the <main id="view"> landmark, so
   the page's only <h1> is never orphaned outside every region. A view's
   innerHTML swap wipes it, so it is (re)created and prepended after each render.
   Hidden (out of the a11y tree) for routes that carry their own visible <h1>. */
function ensureViewHeading(route) {
  const view = document.getElementById("view");
  if (!view) return;
  let h = document.getElementById("view-heading");
  if (!h) {
    h = document.createElement("h1");
    h.id = "view-heading";
    h.className = "sr-only";
  }
  const label = VIEW_HEADINGS[route] || "";
  h.textContent = label;
  h.hidden = !label; // an empty heading must not sit in the a11y tree
  if (view.firstChild !== h) view.insertBefore(h, view.firstChild || null);
}
const STALE_MS = 12 * 60 * 60 * 1000;

/* Routes may carry an id segment (#/jobs/123 preselects that job) and an
   optional query string (#/jobs?company=5 scopes the jobs list to a company).
   Params are passed to the view's render() as a third arg; views that don't
   take it simply ignore it. */
function parseHash() {
  const [, name, id, query] = location.hash.match(/^#\/(\w+)(?:\/(\d+))?(?:\?(.*))?/) || [];
  const params = query ? Object.fromEntries(new URLSearchParams(query)) : {};
  return { route: routes[name] ? name : DEFAULT_ROUTE, id: id ? Number(id) : null, params };
}

function currentRoute() {
  return parseHash().route;
}

/* Leave-guard plumbing (7i): a view may export an async canLeave() that returns
   false to veto navigation (Settings does, to warn about unsaved fit criteria).
   hashchange fires after the hash already changed, so a veto restores the prior
   hash; `restoring` short-circuits the guard on that programmatic re-entry. */
let mountedRoute = null;
let lastHash = location.hash;
let restoring = false;

/* While a fresh install's first-run is unresolved, the wizard IS the
   application (owner decision, Phase 4): there is nothing to show on any other
   route, so every hash resolves to #/welcome. The flag starts from boot()'s
   fetch; any navigation attempt while it is set re-checks the server, because
   the wizard's own exits (add a company, Exit setup, finish) are what end
   first-run — the gate lifts itself on the very navigation they trigger. */
let firstRun = false;

async function render() {
  if (restoring) {
    restoring = false;
    return;
  }
  const prev = mountedRoute ? routes[mountedRoute] : null;
  if (prev && typeof prev.canLeave === "function") {
    let ok = true;
    try {
      ok = await prev.canLeave();
    } catch {
      ok = true; // never trap the user behind a guard error
    }
    if (!ok) {
      restoring = true;
      location.hash = lastHash; // re-fires hashchange; handled by the guard above
      return;
    }
  }
  let { route, id, params } = parseHash();
  if (firstRun && route !== "welcome") {
    const ob = await api.getOnboarding().catch(() => null);
    firstRun = !!ob?.first_run; // null (backend down) unlocks — never trap
    if (firstRun) {
      route = "welcome";
      id = null;
      params = {};
    }
  }
  // Visual half of the takeover: while the welcome route is mounted (first-run
  // or a returning visit) the app chrome is hidden and the brand inert — the
  // wizard's own controls are the only exits. See body.wizard-active in app.css.
  document.body.classList.toggle("wizard-active", route === "welcome");
  // A11Y (#47): the brand logotype routes to #/today and bounces back during the
  // wizard takeover — a phantom no-op tab stop. CSS already blocks the mouse
  // (pointer-events:none); make it inert to keyboard/AT to match.
  const brand = document.querySelector(".brand-mark");
  if (brand) {
    if (route === "welcome") {
      brand.setAttribute("tabindex", "-1");
      brand.setAttribute("aria-hidden", "true");
    } else {
      brand.removeAttribute("tabindex");
      brand.removeAttribute("aria-hidden");
    }
  }
  // The scoring vocabulary (level bands, quadrant/tension labels) has to
  // be in hand before a view paints, or the first frame shows raw tokens like
  // "senior_director" and then swaps. loadVocab() caches its promise, so only
  // the boot render actually waits; every later render costs one microtask.
  // Awaited here rather than at module top level so it covers every entry path
  // (boot, hashchange, the programmatic re-renders below) with one line.
  await loadVocab();
  document.querySelectorAll(".nav-tab").forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.route === route);
  });
  await routes[route].render(document.getElementById("view"), id, params);
  ensureViewHeading(route);
  // Unawaited: the Setup pill is chrome, and a hung onboarding GET must never
  // delay a view. It repaints only when the completeness signature changes.
  onboardingTracker.refresh();
  mountedRoute = route;
  lastHash = location.hash;
}

/* the scheduled refresh is best-effort (the machine may sleep
   through it); this staleness check is the guarantee. It runs on load AND
   whenever the tab becomes visible again: a load-only check lied through an
   overnight sleep — the tab stays mounted, the data goes stale, and no load
   ever re-runs it. The throttle keeps tab-switching from hammering
   /api/refresh/status. */
const STALE_CHECK_THROTTLE_MS = 5 * 60 * 1000;
let lastStaleCheck = 0;

async function checkStaleRefresh() {
  if (Date.now() - lastStaleCheck < STALE_CHECK_THROTTLE_MS) return;
  lastStaleCheck = Date.now();
  let status;
  try {
    status = await api.refreshStatus();
  } catch {
    return; // backend unreachable — views surface their own error
  }
  if (status.running) {
    pollUntilRefreshed();
    return;
  }
  // Nothing pullable yet (the wizard guarantees a company, but a name-only or
  // manual one has no connectable board): a refresh would be a no-op that only
  // stamps last_refresh, flipping the board off "day one" and unmasking the
  // backup/stale banners seconds in. Skip it — and skip the refresh-every-load
  // loop a manual-only install would otherwise be stuck in (#34).
  if (!status.connectable) return;
  const stale = !status.last_refresh || Date.now() - Date.parse(status.last_refresh) > STALE_MS;
  if (!stale) return;
  try {
    await api.triggerRefresh();
    toast("Job boards are stale — refreshing…");
    // Re-render so the mounted view shows the refreshing bar NOW — without
    // this, the first page load of the day triggers the refresh but keeps
    // showing the pre-trigger render until a manual reload.
    if (currentRoute() === "jobs" || currentRoute() === "today") render();
    pollUntilRefreshed();
  } catch {
    /* non-fatal; next load retries */
  }
}

let refreshPollTimer = null; // one poll loop, however many visibility re-checks find it running

function pollUntilRefreshed() {
  if (refreshPollTimer) return;
  let misses = 0;
  refreshPollTimer = setInterval(async () => {
    try {
      const status = await api.refreshStatus();
      misses = 0;
      if (!status.running) {
        clearInterval(refreshPollTimer);
        refreshPollTimer = null;
        toast("Job boards refreshed");
        if (status.refresh_error) buzz("refresh:" + status.refresh_error.at);
        else chime("refresh:" + (status.refresh_report?.at || "done"));
        if (currentRoute() === "jobs" || currentRoute() === "today") render();
      }
    } catch {
      // Tolerate blips before giving up. This is the load-time background
      // watcher, so the give-up stays quiet — the user never asked for it.
      if (++misses >= 3) {
        clearInterval(refreshPollTimer);
        refreshPollTimer = null;
      }
    }
  }, 5000);
}

window.addEventListener("hashchange", render);

/* Skip link (A11Y): move focus into the view without touching the hash — a bare
   "#view" would otherwise resolve to the default route. Focusing #view (which
   carries tabindex="-1") lands the reader inside the content; the next Tab hits
   the first real control. */
document.querySelector(".skip-link")?.addEventListener("click", (event) => {
  event.preventDefault();
  const view = document.getElementById("view");
  view?.focus();
  view?.scrollIntoView();
});

applyTheme(); // boot script already set data-theme; this re-syncs the meta + registers the OS listener
boot();

/* Wake-up path for the staleness guarantee (see checkStaleRefresh). Safe on a
   first-run install: the function's own connectable/throttle guards make the
   visibility checks no-ops until a pullable board exists. */
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") checkStaleRefresh();
});

/* First-run gate (Phase 4): on a fresh install (setup untouched, no company
   yet) the wizard is the whole application — EVERY hash lands on #/welcome
   until the wizard's own exits end first-run (see the firstRun note above the
   router). Also suppress the day-one stale-refresh until at least one company
   exists — a refresh over zero boards is a no-op that only stamps
   last_refresh. A function declaration so it can be called before its body. */
async function boot() {
  let onboarding = null;
  try {
    onboarding = await api.getOnboarding();
  } catch {
    /* backend unreachable — fall through to the normal render path */
  }
  firstRun = !!onboarding?.first_run;
  if (onboarding) onboardingTracker.seed(onboarding); // paint now, no second GET
  if (firstRun && parseHash().route !== "welcome") {
    location.hash = "#/welcome"; // fires hashchange -> render()
  } else {
    render();
  }
  if (!onboarding || onboarding.company_count > 0) checkStaleRefresh();
}
