/* Tokenized date picker (design revamp P3). Replaces the native
   <input type="date"> popup — browser chrome whose sharp corners and
   Chrome-blue selection no page CSS reaches (color-scheme is the ceiling).
   Same architecture as helpHint.js: document-delegated handlers drive ONE
   popup on <body>, so it survives every view innerHTML repaint; call sites
   emit markup via dateFieldHtml() and stay passive.

   The input is readonly: every value in the field was written by this
   module (valid YYYY-MM-DD or empty), which is what lets the views trust
   change/input events wholesale — no partial-date states to gate. On pick,
   the field gets a bubbling `input` then `change`, so both save contracts
   fire: applications commits on change ([data-datepicker] gates), contacts
   rides its generic text path (debounce + equality bail = one PATCH).
   Readonly also means `required` is inert (constraint validation skips
   readonly), so non-emptiness is structural instead: required fields are
   seeded with a date and the popup omits Clear for them.

   Clock: "today" derives from localToday() (bare new Date()) at open time,
   never module scope — a tab left open across midnight would otherwise keep
   highlighting yesterday. Grid math uses explicit-arg new Date(y, m, d)
   rather than date strings: that constructor is pure wall-clock calendar
   arithmetic, so no UTC parsing can shift a cell by a day. */

import { esc, localToday, placeFixed, showPop, hidePop, isPopOpen } from "./ui.js";

const DOW = ["S", "M", "T", "W", "T", "F", "S"]; // Sunday-first, like calendar.js

/** Markup for a picker-backed date field. value is YYYY-MM-DD or falsy.
    opts: {name, field, required, title, ariaLabel} — name for modal form reads,
    data-field for the views' edit-in-place contracts. ariaLabel names the field
    by its PURPOSE ("Applied", "Last contact"); without it the accessible name
    falls back to the formatted-date title (i.e. the value), or nothing when
    empty. */
export function dateFieldHtml(value, { name, field, required, title, ariaLabel } = {}) {
  return `<span class="date-wrap"><input
    class="date-input" type="text" data-datepicker readonly
    autocomplete="off" placeholder="—"
    aria-haspopup="dialog" aria-expanded="false"
    ${name ? `name="${esc(name)}"` : ""}
    ${field ? `data-field="${esc(field)}"` : ""}
    ${ariaLabel ? `aria-label="${esc(ariaLabel)}"` : ""}
    ${required ? "required" : ""}
    ${title ? `title="${esc(title)}"` : ""}
    value="${esc(value || "")}"
  /><svg class="date-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg></span>`;
}

let pop = null; // the single popup node (lazily created, lives on <body>)
let anchor = null; // the input the popup is currently bound to
let view = null; // {y, m} of the displayed month
let focusIso = null; // ISO of the roving-tabindex day

const pad2 = (n) => String(n).padStart(2, "0");
const isoOf = (d) => `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
const parseIso = (s) => {
  if (!s || !/^\d{4}-\d{2}-\d{2}$/.test(s)) return null;
  const d = new Date(`${s}T00:00:00`); // local midnight, the app-wide idiom
  return isNaN(d) ? null : d;
};

function ensurePop() {
  if (pop) return pop;
  pop = document.createElement("div");
  pop.className = "date-pop";
  pop.setAttribute("role", "dialog");
  pop.setAttribute("aria-modal", "false");
  pop.setAttribute("aria-label", "Choose date");
  pop.hidden = true;
  pop.addEventListener("click", onPopClick);
  document.body.appendChild(pop);
  return pop;
}

/* Fixed 6x7 grid so the popup never changes height while paging. */
function renderPop() {
  const today = localToday();
  const selected = anchor?.value || "";
  const first = new Date(view.y, view.m, 1);
  const label = first.toLocaleDateString(undefined, { month: "long", year: "numeric" });
  const cells = [];
  for (let i = 0; i < 42; i++) {
    const d = new Date(view.y, view.m, 1 - first.getDay() + i);
    const iso = isoOf(d);
    // modifiers carry the block prefix: a bare "today" also matched the Today
    // VIEW's container rule (.today — padding/overflow/width), which blew the
    // cell up to 48px with a scrollbar in it (review catch, P3)
    const cls = [
      "date-pop-day",
      d.getMonth() !== view.m ? "date-pop-other" : "",
      iso === today ? "date-pop-today" : "",
      iso === selected ? "date-pop-sel" : "",
    ]
      .filter(Boolean)
      .join(" ");
    cells.push(
      `<button type="button" class="${cls}" data-date="${iso}"
        tabindex="${iso === focusIso ? 0 : -1}"
        ${iso === today ? `aria-current="date"` : ""}
        aria-label="${esc(d.toLocaleDateString(undefined, { weekday: "long", year: "numeric", month: "long", day: "numeric" }))}"
      >${d.getDate()}</button>`
    );
  }
  pop.innerHTML = `
    <div class="date-pop-head">
      <button type="button" class="date-pop-nav" data-nav="-1" aria-label="Previous month">‹</button>
      <span class="date-pop-label">${esc(label)}</span>
      <button type="button" class="date-pop-nav" data-nav="1" aria-label="Next month">›</button>
    </div>
    <div class="date-pop-grid">
      ${DOW.map((d) => `<span class="date-pop-dow" aria-hidden="true">${d}</span>`).join("")}
      ${cells.join("")}
    </div>
    <div class="date-pop-foot">
      <button type="button" class="date-pop-action" data-pick-today>Today</button>
      ${anchor?.required ? "" : `<button type="button" class="date-pop-action" data-pick-clear>Clear</button>`}
    </div>`;
}

function open(input) {
  ensurePop();
  if (anchor && anchor !== input) anchor.setAttribute("aria-expanded", "false");
  anchor = input;
  const start = parseIso(input.value) || parseIso(localToday());
  view = { y: start.getFullYear(), m: start.getMonth() };
  focusIso = isoOf(start);
  renderPop();
  showPop(pop);
  placeFixed(pop, input.closest(".date-wrap") || input);
  input.setAttribute("aria-expanded", "true");
  // preventScroll everywhere inside the pop: a focus-induced scroll would
  // trip the capture scroll listener below and close the pop mid-open
  pop.querySelector('[tabindex="0"]')?.focus({ preventScroll: true });
}

function close({ refocus = false, instant = false } = {}) {
  if (!isPopOpen(pop)) return; // an exiting pop is already closed; don't rerun anchor cleanup
  hidePop(pop, { instant });
  if (anchor) {
    anchor.setAttribute("aria-expanded", "false");
    if (refocus && anchor.isConnected) anchor.focus();
  }
  anchor = null;
}

const isOpen = () => isPopOpen(pop);

/* Write-back: the one place a value enters the field. Bubbling input+change
   reach the view container's delegated handlers (the popup itself lives on
   <body>, outside every view root). */
function commit(value) {
  let input = anchor;
  close({ refocus: true });
  if (input && !input.isConnected) {
    // A focusout-triggered save can repaint the pane UNDER the open popup
    // (edit a text field, click straight into a date field: the text field's
    // focusout save rebuilds the pane after we anchored) — the pick must
    // land on the identical rebuilt field, not drop silently (review catch).
    // Re-resolve by identity: data-field in the views, name in the modals;
    // each is unique on whatever surface is mounted.
    const sel = input.dataset.field
      ? `[data-datepicker][data-field="${input.dataset.field}"]`
      : input.name
        ? `[data-datepicker][name="${input.name}"]`
        : null;
    input = sel ? document.querySelector(sel) : null;
    input?.focus(); // mirror the refocus close() skipped on the dead node
  }
  if (!input || !input.isConnected) return;
  if (input.value === value) return;
  input.value = value;
  input.dispatchEvent(new Event("input", { bubbles: true }));
  input.dispatchEvent(new Event("change", { bubbles: true }));
}

function moveFocus(days, months = 0) {
  const d = parseIso(focusIso) || parseIso(localToday());
  if (months) {
    // clamp day-of-month so Jan 31 -> Feb 28, not Mar 3
    const target = new Date(d.getFullYear(), d.getMonth() + months, 1);
    const last = new Date(target.getFullYear(), target.getMonth() + 1, 0).getDate();
    d.setFullYear(target.getFullYear(), target.getMonth(), Math.min(d.getDate(), last));
  } else {
    d.setDate(d.getDate() + days);
  }
  focusIso = isoOf(d);
  if (d.getFullYear() !== view.y || d.getMonth() !== view.m) {
    view = { y: d.getFullYear(), m: d.getMonth() };
  }
  renderPop();
  pop.querySelector('[tabindex="0"]')?.focus({ preventScroll: true });
}

function onPopClick(e) {
  const day = e.target.closest("[data-date]");
  if (day) {
    commit(day.dataset.date);
    return;
  }
  const nav = e.target.closest("[data-nav]");
  if (nav) {
    const delta = Number(nav.dataset.nav);
    view = { y: view.y + Math.floor((view.m + delta) / 12), m: (((view.m + delta) % 12) + 12) % 12 };
    // keep the roving day inside the shown month so arrows resume sensibly
    const f = parseIso(focusIso);
    if (!f || f.getFullYear() !== view.y || f.getMonth() !== view.m) {
      focusIso = isoOf(new Date(view.y, view.m, 1));
    }
    renderPop();
    // the click destroyed the old button with the innerHTML rebuild —
    // refocus its same-direction replacement so paging can be repeated
    pop.querySelector(`[data-nav="${delta}"]`)?.focus({ preventScroll: true });
    return;
  }
  if (e.target.closest("[data-pick-today]")) {
    commit(localToday());
    return;
  }
  if (e.target.closest("[data-pick-clear]")) {
    commit("");
  }
}

/* Open on click (primary path — works on touch; the input is readonly so no
   caret is lost). A click on the input while open toggles closed. */
document.addEventListener(
  "click",
  (e) => {
    const input = e.target.closest && e.target.closest("[data-datepicker]");
    if (input) {
      if (isOpen() && anchor === input) close();
      else open(input);
      return;
    }
    if (isOpen() && !(e.target.closest && e.target.closest(".date-pop"))) close();
  },
  true
);

/* One CAPTURE-phase keyboard handler. Capture is load-bearing for Escape:
   ui.js's modal escListener is a document-level BUBBLE listener with no
   target check, so inside the reminder/activity modals a bubbled Escape
   would close the whole modal — capture fires first and stops it, so Esc
   peels the picker only. */
document.addEventListener(
  "keydown",
  (e) => {
    if (!isOpen()) {
      // Enter/Space/ArrowDown on the field opens (focus alone never does —
      // tabbing through a form must not pop a calendar).
      const input = e.target.closest && e.target.closest("[data-datepicker]");
      if (input && (e.key === "Enter" || e.key === " " || e.key === "ArrowDown")) {
        e.preventDefault(); // Enter would submit the surrounding modal form
        e.stopPropagation();
        open(input);
      }
      return;
    }
    const inPop = e.target.closest && e.target.closest(".date-pop");
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      close({ refocus: true });
      return;
    }
    if (!inPop) return;
    const NAV = {
      ArrowLeft: () => moveFocus(-1),
      ArrowRight: () => moveFocus(1),
      ArrowUp: () => moveFocus(-7),
      ArrowDown: () => moveFocus(7),
      PageUp: () => moveFocus(0, -1),
      PageDown: () => moveFocus(0, 1),
      Home: () => moveFocus(-parseIso(focusIso).getDay()),
      End: () => moveFocus(6 - parseIso(focusIso).getDay()),
    };
    if (NAV[e.key]) {
      e.preventDefault();
      e.stopPropagation();
      NAV[e.key]();
      return;
    }
    if (e.key === "Tab") {
      // trap: cycle prev-month .. Clear (day cells other than the roving one
      // are tabindex=-1, so the natural order is nav, day, foot buttons)
      const focusables = [...pop.querySelectorAll("button")].filter(
        (b) => b.tabIndex >= 0
      );
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      if (!e.shiftKey && e.target === last) {
        e.preventDefault();
        first.focus();
      } else if (e.shiftKey && e.target === first) {
        e.preventDefault();
        last.focus();
      }
      e.stopPropagation();
    }
  },
  true
);

/* The popup is fixed to an anchor inside scrollable, repaintable panes —
   close rather than chase (helpHint's ruling; scroll is capture so any
   container's scroll counts). Instant: a fixed pop fading while its anchor
   scrolls away reads as detached (P5). */
document.addEventListener("scroll", () => close({ instant: true }), true);
window.addEventListener("resize", () => close({ instant: true }));
window.addEventListener("hashchange", () => close({ instant: true }));
