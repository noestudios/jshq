/* Tokenized time picker. Replaces the last native browser control in the app —
   <input type="time"> — whose stepper chrome, sharp corners and locale-driven
   layout no page CSS reaches.

   Same architecture as datepicker.js, for the same reasons: document-delegated
   handlers drive ONE popup on <body>, so it survives every innerHTML repaint;
   call sites emit markup via timeFieldHtml() and stay passive.

   VALUE vs DISPLAY. The stored value is 24h "HH:MM" (what the API takes); the
   field shows 12h ("2:30 PM"), which is what the user reads. Those cannot be the
   same string, so the wrap carries two inputs: a readonly text input the user
   sees and clicks, and a hidden input that owns `name` and the canonical
   value. That keeps the existing call-site contract byte-for-byte —
   form.due_time.value is still "HH:MM" or "" — instead of asking every reader
   to learn a new accessor.

   TYPING IS THE PRIMARY PATH (revised 2026-08-07; the previous design was
   three listbox columns — hour, minute, AM/PM — that only moved a selection,
   so one value cost up to four clicks plus a Done). The evidence for the
   inversion is the data: across both databases the eight reminder times that
   exist have zero repeats, and one of them (10:10) sits off any grid a column
   could offer. There is no cluster to mine, so a grid can only ever be a
   convenience and the text box has to be the workhorse. It is therefore first
   in the popup, focused and text-selected on open, and Enter commits it.

   The chips are the coarse fast path: nine hours, 9 AM to 5 PM, matching the
   business-hours span every recorded reminder falls inside. A chip commits on
   the single click — there is no Done, because with nothing to "select" there
   is nothing to confirm.

   Nothing re-renders after open. The old popup rebuilt itself on every
   keystroke to mirror typing onto the columns, which needed a caret
   save/restore dance to stay usable; with no columns to mirror there is
   nothing to sync, so keystrokes only clear the invalid flag.

   Clock: "now" derives from a bare new Date() at CALL time, never at module
   scope — a tab left open across midnight would otherwise keep reporting
   yesterday. */

import { esc, placeFixed, showPop, hidePop, isPopOpen } from "./ui.js";

/* 24h hours offered as chips. Business hours: every reminder time on record
   (08:30 through 16:00) falls inside this span. Anything outside it, and every
   non-zero minute, is typed. */
const PRESETS = [9, 10, 11, 12, 13, 14, 15, 16, 17];
const PRESET_COLS = 3; // mirrors the CSS grid; Up/Down step by this

const pad2 = (n) => String(n).padStart(2, "0");

/** 13 -> "1 PM". Bare hours: ":00" on all nine chips is noise, but the
    meridiem stays, because "1" alone beside "9" is genuinely ambiguous. */
const presetLabel = (h) => `${h % 12 === 0 ? 12 : h % 12} ${h < 12 ? "AM" : "PM"}`;

/** "HH:MM" (24h) -> {h, m}, or null. Anything else is not our value. */
function parse24(s) {
  const m = /^(\d{1,2}):(\d{2})$/.exec((s || "").trim());
  if (!m) return null;
  const h = Number(m[1]);
  const mi = Number(m[2]);
  return h >= 0 && h <= 23 && mi >= 0 && mi <= 59 ? { h, m: mi } : null;
}

/** {h,m} -> "2:30 PM". The one place 24h becomes something the user reads. */
export function fmt12(value) {
  const t = typeof value === "string" ? parse24(value) : value;
  if (!t) return "";
  const ap = t.h < 12 ? "AM" : "PM";
  const h12 = t.h % 12 === 0 ? 12 : t.h % 12;
  return `${h12}:${pad2(t.m)} ${ap}`;
}

/** Tolerant free-entry parser: "14:30", "1430", "2:30pm", "230 p", "2 pm",
    "14". Returns {h,m} or null. Bare hours <= 12 with no meridiem read as the
    literal hour (so "7" is 7 AM, not 7 PM) — guessing intent would be worse
    than being predictable. */
export function parseLoose(raw) {
  let s = (raw || "").trim().toLowerCase().replace(/\./g, "");
  if (!s) return null;
  let ap = null;
  const apm = /([ap])m?\s*$/.exec(s);
  if (apm) {
    ap = apm[1];
    s = s.slice(0, apm.index).trim();
  }
  let h;
  let m = 0;
  let mm = /^(\d{1,2}):(\d{1,2})$/.exec(s);
  if (mm) {
    h = Number(mm[1]);
    m = Number(mm[2]);
  } else if ((mm = /^(\d{1,2})$/.exec(s))) {
    h = Number(mm[1]);
  } else if ((mm = /^(\d{3,4})$/.exec(s))) {
    // "1430" / "230" — the trailing two digits are always the minutes
    h = Number(mm[1].slice(0, -2));
    m = Number(mm[1].slice(-2));
  } else {
    return null;
  }
  if (m > 59) return null;
  if (ap) {
    if (h < 1 || h > 12) return null;
    h = ap === "a" ? h % 12 : (h % 12) + 12;
  } else if (h > 23) {
    return null;
  }
  return { h, m };
}

/** Markup for a picker-backed time field. value is "HH:MM" (24h) or falsy.
    opts: {name, field, required, title} — `name` lands on the hidden input so
    form.NAME.value keeps returning the 24h value; `field` mirrors the views'
    edit-in-place contract. */
export function timeFieldHtml(value, { name, field, required, title } = {}) {
  const v = parse24(value) ? value : "";
  return `<span class="time-wrap"${name ? ` data-timefield="${esc(name)}"` : ""}${field ? ` data-timefieldkey="${esc(field)}"` : ""}><input
    class="time-input" type="text" data-timepicker readonly
    autocomplete="off" placeholder="—"
    aria-haspopup="dialog" aria-expanded="false"
    ${required ? "data-time-required" : ""}
    ${title ? `title="${esc(title)}"` : ""}
    value="${esc(fmt12(v))}"
  /><input type="hidden"${name ? ` name="${esc(name)}"` : ""}${field ? ` data-field="${esc(field)}"` : ""} value="${esc(v)}" /><svg class="time-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15 14"/></svg></span>`;
}

let pop = null; // the single popup node (lazily created, lives on <body>)
let anchor = null; // the visible input the popup is currently bound to
let openWidth = 0; // innerWidth at open — the resize handler's keyboard test

const hiddenOf = (input) => input?.closest(".time-wrap")?.querySelector('input[type="hidden"]');
const freeBox = () => pop?.querySelector(".time-pop-free");

function ensurePop() {
  if (pop) return pop;
  pop = document.createElement("div");
  pop.className = "time-pop";
  pop.setAttribute("role", "dialog");
  pop.setAttribute("aria-modal", "false");
  pop.setAttribute("aria-label", "Choose time");
  pop.hidden = true;
  pop.addEventListener("click", onPopClick);
  pop.addEventListener("input", onPopInput);
  document.body.appendChild(pop);
  return pop;
}

/* Rendered once per open. `cur` seeds the entry box and marks the matching
   chip; after that the entry box owns the truth and nothing here redraws. */
function renderPop(cur) {
  const allowClear = !anchor?.hasAttribute("data-time-required");
  const chips = PRESETS.map((h, i) => {
    const on = cur.h === h && cur.m === 0;
    // Roving tabindex: the chip grid is ONE tab stop and arrows move inside it.
    const rove = on || (i === 0 && !PRESETS.some((p) => cur.h === p && cur.m === 0));
    return `<button type="button" class="time-pop-chip${on ? " time-pop-sel" : ""}"
      data-preset="${h}" tabindex="${rove ? 0 : -1}"
      ${on ? 'aria-current="true"' : ""}>${esc(presetLabel(h))}</button>`;
  }).join("");
  pop.innerHTML = `
    <input class="time-pop-free" type="text" inputmode="text" autocomplete="off"
      aria-label="Type a time" placeholder="e.g. 2:45 PM" value="${esc(fmt12(cur))}" />
    <div class="time-pop-presets" role="group" aria-label="Common times">${chips}</div>
    <div class="time-pop-foot">
      <button type="button" class="time-pop-action" data-pick-now>Now</button>
      ${allowClear ? `<button type="button" class="time-pop-action" data-pick-clear>Clear</button>` : ""}
    </div>`;
}

function open(input) {
  ensurePop();
  if (anchor && anchor !== input) anchor.setAttribute("aria-expanded", "false");
  anchor = input;
  const stored = parse24(hiddenOf(input)?.value);
  let cur;
  if (stored) {
    cur = { ...stored };
  } else {
    // Empty field: start at the current wall clock rounded to a quarter, so the
    // entry box opens on something useful. Nothing is committed until Enter.
    const now = new Date();
    const m = Math.round(now.getMinutes() / 15) * 15;
    cur = { h: (now.getHours() + (m === 60 ? 1 : 0)) % 24, m: m === 60 ? 0 : m };
  }
  renderPop(cur);
  showPop(pop);
  placeFixed(pop, input.closest(".time-wrap") || input);
  openWidth = window.innerWidth; // baseline for the keyboard-vs-resize test
  input.setAttribute("aria-expanded", "true");
  // preventScroll everywhere inside the pop: a focus-induced scroll would trip
  // the capture scroll listener below and close the pop mid-open. Selecting the
  // text means typing replaces it, which is the whole point of opening here.
  const free = freeBox();
  free?.focus({ preventScroll: true });
  free?.select();
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

/* Write-back: the one place a value enters the field. value is "HH:MM" or "".
   Bubbling input+change fire from the HIDDEN input — it is the element that
   owns the name and the canonical value, so a delegated view handler reading
   e.target.value gets "14:30" rather than the display string. */
function commit(value, { refocus = true } = {}) {
  let input = anchor;
  close({ refocus });
  if (input && !input.isConnected) {
    // A focusout-triggered save can repaint the pane UNDER the open popup, so
    // the field we anchored to may be a dead node by now. Re-resolve by
    // identity, the same way datepicker.js does.
    const wrap = input.closest(".time-wrap");
    const nm = wrap?.dataset.timefield;
    const key = wrap?.dataset.timefieldkey;
    const sel = key
      ? `.time-wrap[data-timefieldkey="${key}"] [data-timepicker]`
      : nm
        ? `.time-wrap[data-timefield="${nm}"] [data-timepicker]`
        : null;
    input = sel ? document.querySelector(sel) : null;
    input?.focus();
  }
  if (!input || !input.isConnected) return;
  const hidden = hiddenOf(input);
  if (!hidden || hidden.value === value) return;
  hidden.value = value;
  input.value = fmt12(value);
  hidden.dispatchEvent(new Event("input", { bubbles: true }));
  hidden.dispatchEvent(new Event("change", { bubbles: true }));
}

const commitParts = (t, opts) => commit(`${pad2(t.h)}:${pad2(t.m)}`, opts);

/** Commit what the entry box says. Empty means clear, which is what emptying a
    text box reads as — except on a required field, where clearing is not on
    offer and the box is flagged instead. */
function commitCurrent() {
  const free = freeBox();
  const typed = (free?.value ?? "").trim();
  if (!typed) {
    if (anchor?.hasAttribute("data-time-required")) {
      free?.classList.add("time-pop-free-bad");
      return;
    }
    commit("");
    return;
  }
  const parsed = parseLoose(typed);
  if (!parsed) {
    // unparseable free text: keep the popup open and say so, rather than
    // silently committing something the user did not ask for
    free?.classList.add("time-pop-free-bad");
    return;
  }
  commitParts(parsed);
}

/** Outside-click dismissal. The entry box is focused and text-selected on open
    and TYPING IS THE PRIMARY PATH, so "type a time, then click Save (or another
    field)" is the expected flow — but that click landed on the capture handler
    below, which called bare close() and silently dropped the typed value, so the
    reminder saved with no time. Persist a valid (or clearable) value on the way
    out; for unparseable text fall back to a plain close, because dismissing a
    stray click must never trap the user the way commitCurrent's flag-and-stay
    does. No refocus: the user clicked elsewhere on purpose. */
function commitOrClose() {
  const free = freeBox();
  const typed = (free?.value ?? "").trim();
  if (!typed) {
    if (anchor?.hasAttribute("data-time-required")) close();
    else commit("", { refocus: false });
    return;
  }
  const parsed = parseLoose(typed);
  if (parsed) commitParts(parsed, { refocus: false });
  else close();
}

function onPopInput(e) {
  // Nothing to keep in sync any more — just drop the invalid flag as they fix it.
  if (e.target.closest(".time-pop-free")) e.target.classList.remove("time-pop-free-bad");
}

function onPopClick(e) {
  const chip = e.target.closest("[data-preset]");
  if (chip) {
    commitParts({ h: Number(chip.dataset.preset), m: 0 });
    return;
  }
  if (e.target.closest("[data-pick-now]")) {
    const now = new Date();
    commitParts({ h: now.getHours(), m: now.getMinutes() });
    return;
  }
  if (e.target.closest("[data-pick-clear]")) commit("");
}

/** Move the roving tabindex within the chip grid without committing. */
function roveTo(chips, i) {
  const next = chips[(i + chips.length) % chips.length];
  chips.forEach((c) => c.setAttribute("tabindex", c === next ? "0" : "-1"));
  next.focus({ preventScroll: true });
}

/* Open on click (primary path — works on touch; the input is readonly so no
   caret is lost). A click on the input while open toggles closed. */
document.addEventListener(
  "click",
  (e) => {
    const input = e.target.closest && e.target.closest("[data-timepicker]");
    if (input) {
      if (isOpen() && anchor === input) close();
      else open(input);
      return;
    }
    if (isOpen() && !(e.target.closest && e.target.closest(".time-pop"))) commitOrClose();
  },
  true
);

/* One CAPTURE-phase keyboard handler. Capture is load-bearing for Escape:
   ui.js's modal escListener is a document-level BUBBLE listener with no target
   check, so inside the reminder modal a bubbled Escape would close the whole
   modal — capture fires first and stops it, so Esc peels the picker only. */
document.addEventListener(
  "keydown",
  (e) => {
    if (!isOpen()) {
      const input = e.target.closest && e.target.closest("[data-timepicker]");
      if (input && (e.key === "Enter" || e.key === " " || e.key === "ArrowDown")) {
        e.preventDefault(); // Enter would submit the surrounding modal form
        e.stopPropagation();
        open(input);
      }
      return;
    }
    const inPop = e.target.closest && e.target.closest(".time-pop");
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      close({ refocus: true });
      return;
    }
    if (!inPop) return;
    const chip = e.target.closest("[data-preset]");
    if (e.key === "Enter") {
      e.preventDefault(); // never submit the surrounding modal from in here
      e.stopPropagation();
      // On a chip, Enter means that chip; anywhere else it means the entry box.
      if (chip) commitParts({ h: Number(chip.dataset.preset), m: 0 });
      else commitCurrent();
      return;
    }
    if (chip) {
      const chips = [...pop.querySelectorAll("[data-preset]")];
      const i = chips.indexOf(chip);
      const NAV = {
        ArrowLeft: () => roveTo(chips, i - 1),
        ArrowRight: () => roveTo(chips, i + 1),
        ArrowUp: () => roveTo(chips, i - PRESET_COLS),
        ArrowDown: () => roveTo(chips, i + PRESET_COLS),
        Home: () => roveTo(chips, 0),
        End: () => roveTo(chips, chips.length - 1),
      };
      if (NAV[e.key]) {
        e.preventDefault();
        e.stopPropagation();
        NAV[e.key]();
        return;
      }
    }
    if (e.key === "Tab") {
      // trap: cycles the entry box, the roving chip and the foot actions
      const focusables = [...pop.querySelectorAll("button, input")].filter((b) => b.tabIndex >= 0);
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

/** Re-anchor after a viewport change that did not warrant closing. */
function reposition() {
  if (anchor?.isConnected) placeFixed(pop, anchor.closest(".time-wrap") || anchor);
  else close({ instant: true });
}

/* The popup is fixed to an anchor inside scrollable, repaintable panes — close
   rather than chase (helpHint's ruling; scroll is capture so any container's
   scroll counts). */
/* Instant: a fixed pop fading while its anchor scrolls away reads as
   detached (P5). */
/* EXCEPTION, and it is a mobile bug fix, not a softening of the ruling: while
   focus is INSIDE the pop, a scroll is the browser reacting to the picker
   itself — scrolling the focused entry box clear of a soft keyboard — not the
   user scrolling the anchor away. Closing there kills the picker the instant it
   opens. Re-anchor instead; the user-scrolls-away case still closes, because
   that scroll happens with focus on the page, not in the pop. */
document.addEventListener(
  "scroll",
  () => {
    if (!isOpen()) return;
    if (pop.contains(document.activeElement)) reposition();
    else close({ instant: true });
  },
  true
);

/* A SOFT KEYBOARD IS A RESIZE, and this closed the picker on Android the moment
   it opened. The entry box takes focus on open (typing is the primary path), the
   keyboard follows, and Chromium — unlike Firefox — shrinks the LAYOUT viewport
   to make room, which fires window.resize. The old unconditional close() meant
   the picker summoned the keyboard and was then killed by it: both vanished
   together. Reported on Brave and Chrome, working on Firefox, which is exactly
   the split that difference predicts.

   Width is the discriminator. A keyboard changes height only; a rotation or a
   real window resize changes width. Height-only changes re-anchor instead,
   which is what the keyboard's reflow needs anyway. */
window.addEventListener("resize", () => {
  if (!isOpen()) return;
  if (window.innerWidth !== openWidth) close({ instant: true });
  else reposition();
});
window.addEventListener("hashchange", () => close({ instant: true }));
