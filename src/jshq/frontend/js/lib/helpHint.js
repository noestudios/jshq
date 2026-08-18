/* Inline contextual help (Phase 9, sub-pass b): a small focusable "?" button
   that reveals a one-line hint in a lightweight popover. ONE document-level
   delegated handler drives a single popover appended to <body> — views repaint
   via innerHTML, so per-node listeners would die, but delegation survives every
   repaint. All hint copy lives in HINTS so it's maintained in one place. */

import { esc, placeFixed, showPop, hidePop, isPopOpen } from "./ui.js";

const HINTS = {
  "fit-score": {
    title: "Fit score",
    body: "A 0–100 match score from your criteria. A dash means it isn't scored yet; 0 means it failed a hard rule. The color is a quick read: high / mid / low.",
  },
  "search-syntax": {
    title: "Search shortcuts",
    body: "Filters by title, company, or location. A comma means either (director, engineering); a plus means both (director + remote).",
  },
  "tier1-tier2": {
    title: "How scoring works",
    body: "“Hard rules” are pass/fail gates — fail one and the job scores 0. “What I'm looking for” is your ranked list of what makes a job great; the model weighs it once a job is past the gates.",
  },
  "comp-floor-target": {
    title: "Floor vs target",
    body: "Floor: pay below this is rejected. Target: pay below this is kept but flagged. Jobs with no listed pay are never rejected on comp.",
  },
  "commute-radius": {
    title: "Commute radius",
    body: "Maximum drive time in minutes, not miles. The app estimates drive time to each town; some towns are measured exactly. Clear the center to use the allowlist only.",
  },
  "title-bands": {
    title: "Title bands",
    body: "Target bands pass; flag bands are kept but marked for a second look. Neither is an outright reject.",
  },
  "tier2-weight": {
    title: "Importance weight",
    body: "Each ranked criterion has an importance dial (× from 0.25 to 4). 1 is normal, 2 counts it about double, 0.5 counts it half. The model blends this with the rank order.",
  },
  "inclusion-rules": {
    title: "Inclusion rules",
    body: "Say “always include” or “never include” by title or location in plain language, instead of editing keyword lists by hand.",
  },
  "learned-rules": {
    title: "Description-based rules",
    body: "Rules the app suggests after reading jobs you dismissed. Approve or ignore each; accepted ones take effect on the next Rescore.",
  },
  "application-status": {
    title: "Application status",
    body: "The pipeline runs drafting → applied → screen → interview → offer. Rejected and withdrawn are terminal.",
  },
  "tailoring": {
    title: "Resume tailoring",
    body: "Generates resume edits and a cover-letter draft from the job description. Approve changes line by line, optionally refine by chat, then Apply for versioned PDFs. Nothing is sent — you send them yourself.",
  },
  "stale-banners": {
    title: "Health warnings",
    body: "These flag when job listings are overdue for a refresh, a company’s jobs stopped loading, or the last backup is old.",
  },
};

/** Markup for an inline "?" hint. Returns "" for an unknown id so a typo can
    never inject a dead control. */
export function helpHintHtml(id) {
  const hint = HINTS[id];
  if (!hint) return "";
  return `<button type="button" class="help-hint" data-help="${esc(id)}" aria-label="Help: ${esc(hint.title)}" aria-expanded="false">?</button>`;
}

let pop = null; // the single popover node (lazily created, lives on <body>)
let anchor = null; // the button the popover is currently bound to
let closeTimer = null;

function cancelClose() {
  if (closeTimer) {
    clearTimeout(closeTimer);
    closeTimer = null;
  }
}

function scheduleClose() {
  cancelClose();
  closeTimer = setTimeout(close, 140);
}

function ensurePop() {
  if (pop) return pop;
  pop = document.createElement("div");
  pop.className = "help-pop";
  pop.setAttribute("role", "tooltip");
  pop.hidden = true;
  pop.addEventListener("mouseenter", cancelClose); // keep open while reading it
  pop.addEventListener("mouseleave", scheduleClose);
  document.body.appendChild(pop);
  return pop;
}

function open(btn) {
  cancelClose();
  if (anchor === btn && isPopOpen(pop)) return; // already showing this one
  const hint = HINTS[btn.dataset.help];
  if (!hint) return;
  const p = ensurePop();
  if (anchor && anchor !== btn) anchor.setAttribute("aria-expanded", "false");
  p.innerHTML = `<div class="help-pop-title">${esc(hint.title)}</div><div class="help-pop-body">${esc(hint.body)}</div>`;
  // showPop, not bare hidden=false: the pop is ONE shared node hopping between
  // anchors, and it may still be mid-exit from the previous hint — the pending
  // hide timer must be cancelled or it would hide the pop we just re-aimed (P5).
  showPop(p);
  placeFixed(p, btn); // shared popover placement (ui.js) — close-on-scroll keeps it honest
  btn.setAttribute("aria-expanded", "true");
  anchor = btn;
}

function close({ instant = false } = {}) {
  cancelClose();
  if (!isPopOpen(pop)) return;
  hidePop(pop, { instant });
  if (anchor) anchor.setAttribute("aria-expanded", "false");
  anchor = null;
}

/* Click/tap opens (the primary path; works on touch + keyboard). Capture phase
   with stopPropagation so a "?" sitting inside a clickable row never triggers
   the row's own click handler. A click outside the popover and any hint closes. */
document.addEventListener(
  "click",
  (e) => {
    const btn = e.target.closest && e.target.closest(".help-hint");
    if (btn) {
      e.preventDefault();
      e.stopPropagation();
      open(btn);
      return;
    }
    if (isPopOpen(pop) && !(e.target.closest && e.target.closest(".help-pop"))) {
      close();
    }
  },
  true
);

// Hover only on real hover devices, so touch never races synthetic mouse events.
if (window.matchMedia && window.matchMedia("(hover: hover)").matches) {
  document.addEventListener("mouseover", (e) => {
    const btn = e.target.closest && e.target.closest(".help-hint");
    if (btn) open(btn);
  });
  document.addEventListener("mouseout", (e) => {
    if (e.target.closest && e.target.closest(".help-hint")) scheduleClose();
  });
}

// Keyboard focus opens/closes too.
document.addEventListener("focusin", (e) => {
  const btn = e.target.closest && e.target.closest(".help-hint");
  if (btn) open(btn);
});
document.addEventListener("focusout", (e) => {
  if (e.target.closest && e.target.closest(".help-hint")) scheduleClose();
});

// Dismiss on Escape, route change, and any scroll/resize (the popover is fixed
// to a moving button — close rather than chase it; instant, since a fading pop
// detached from its scrolling anchor reads wrong — P5).
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") close();
});
document.addEventListener("scroll", () => close({ instant: true }), true);
window.addEventListener("resize", () => close({ instant: true }));
window.addEventListener("hashchange", () => close({ instant: true }));
