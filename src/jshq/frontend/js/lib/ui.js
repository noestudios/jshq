/* Small DOM helpers: escaping, modals, confirm dialog, toasts.
   Every piece of user data rendered via template literals MUST pass through
   esc() — this is the only XSS guard in a framework-free app. */

/* The app binds to 127.0.0.1, so the browser runs on the server's own
   machine — client-side platform detection is authoritative for copy and
   for hiding features the backend only implements on one OS. */
export const isMac = navigator.platform.toUpperCase().includes("MAC");

export function esc(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

/** Escape for use inside an attribute that is itself a URL. Allows only
    http(s) and mailto so javascript: URLs can never land in an href. */
export function escUrl(value) {
  const raw = String(value || "").trim();
  if (!/^(https?:\/\/|mailto:)/i.test(raw)) return "";
  return esc(raw);
}

let activeModal = null;
// A guard consulted only by the ACCIDENTAL close vectors (backdrop-click,
// Escape); returning false vetoes the close. The explicit Cancel button and
// submit never consult it. Used by the compose modal to confirm a dirty draft
// inline, since a confirm-modal would replace this one (single active modal).
let activeBeforeClose = null;
// One-up id source so each dialog's title gets a unique node to point
// aria-labelledby at (A11Y: the dialog must have an accessible name).
let modalSeq = 0;
let fieldSeq = 0; // unique ids minted to associate modal-form labels with controls
// The element that had focus before the modal opened, restored when it closes
// (WCAG 2.4.3): without this, dismissing a modal drops focus to <body> and a
// keyboard/screen-reader user loses their place.
let restoreFocusTo = null;

/* Our own message for each way a field can fail. The browser's own strings
   ("Please fill out this field.") are what we are replacing, so validationMessage
   is deliberately not used — it would put the same sentence in a different box.
   Ordered: the first matching flag wins, and valueMissing outranks the rest
   because an empty url field is missing, not malformed. */
function fieldError(el, label) {
  const v = el.validity;
  if (v.valueMissing) return `${label} is required.`;
  if (v.typeMismatch) return el.type === "email" ? "Enter a valid email address." : "Enter a full URL (https://…).";
  if (v.rangeUnderflow) return `Must be ${el.min} or more.`;
  if (v.rangeOverflow) return `Must be ${el.max} or less.`;
  if (v.tooShort) return `At least ${el.minLength} characters.`;
  if (v.tooLong) return `At most ${el.maxLength} characters.`;
  if (v.stepMismatch || v.badInput) return "That value isn't valid.";
  if (v.patternMismatch) return "That format isn't valid.";
  return "That value isn't valid.";
}

/* The field's own label, minus the " *" that marks it required — the asterisk
   is a decoration on the label, not part of the field's name. Falls back to the
   control's name attribute so a field with no <label> still gets a sentence. */
function fieldLabel(field, el) {
  const text = field?.querySelector("label")?.textContent?.trim();
  if (text) return text.replace(/\s*\*$/, "");
  return el.name ? el.name.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()) : "This field";
}

function clearFieldError(field) {
  if (!field) return;
  field.classList.remove("has-error");
  field.querySelector(".field-error")?.remove();
}

/* Constraint validation, ours instead of the browser's. The form carries
   novalidate (below), which suppresses the native bubble WITHOUT disabling the
   constraints — element.validity still populates, so required / type=url /
   type=email / min stay declared on the inputs and this reads them.

   The readonly date fields are the one gap the platform leaves: per spec,
   constraint validation skips a readonly control, so `required` on a
   datepicker anchor never reports valueMissing (datepicker.js says as much).
   Checked explicitly here so a required-but-empty date is caught like anything
   else. Returns true when the form may submit. */
function validateForm(form) {
  // Clear in its OWN pass. A .form-field can hold more than one form control —
  // the add-job modal puts the posting-URL input and its Fetch button in the
  // same field, and the date fields pair an anchor input with a picker button —
  // so clearing inline would let a later sibling wipe the error the input just
  // earned.
  form.querySelectorAll(".form-field").forEach(clearFieldError);

  let firstBad = null;
  for (const el of form.elements) {
    const field = el.closest(".form-field");
    if (!field || el.disabled || field.classList.contains("has-error")) continue;
    const missing = el.hasAttribute("required") && !String(el.value || "").trim();
    if (!missing && (!el.willValidate || el.checkValidity())) continue;
    const label = fieldLabel(field, el);
    field.classList.add("has-error");
    const note = document.createElement("div");
    note.className = "field-error";
    note.setAttribute("role", "alert");
    // The readonly case reports no flags at all, so it can't go through
    // fieldError() — it would fall to the generic message.
    note.textContent = el.validity.valid ? `${label} is required.` : fieldError(el, label);
    field.appendChild(note);
    if (!firstBad) firstBad = el;
  }
  if (!firstBad) return true;
  firstBad.focus();
  return false;
}

export function openModal({ title, body, footer, onSubmit, beforeClose }) {
  // Instant: a replacing modal must not stack over one still fading out.
  // keepFocus: this swap must NOT restore focus to the outgoing modal's opener
  // — the incoming modal inherits the recorded target, so the eventual real
  // close returns focus to whatever opened the FIRST modal in the stack.
  const replacing = !!activeModal;
  closeModal({ instant: true, keepFocus: true });
  if (!replacing) {
    // Record the opener (skip <body>, which means nothing was focused) so
    // closeModal can hand focus back to it. Captured after the no-op replace
    // close above, so it is the true page focus, never a torn-down field.
    const opener = document.activeElement;
    restoreFocusTo = opener && opener !== document.body ? opener : null;
  }
  activeBeforeClose = beforeClose || null;
  const titleId = `modal-title-${++modalSeq}`;
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  // novalidate: Chrome's constraint-validation bubble is browser chrome — CSS
  // cannot reach its colour, its icon or its motion, so it is the one surface
  // in the app that ignores the design system entirely. validateForm() above
  // replaces it with inline field errors on our own tokens.
  // aria-labelledby -> the title node: the dialog's accessible name (A11Y).
  overlay.innerHTML = `
    <form class="modal" role="dialog" aria-modal="true" aria-labelledby="${titleId}" novalidate>
      <div class="modal-head">
        <div class="modal-title" id="${titleId}">${esc(title)}</div>
      </div>
      <div class="modal-body">${body}</div>
      <div class="modal-foot">${footer}</div>
    </form>`;

  const form = overlay.querySelector("form");
  // A11Y: associate each field's <label> with its control. Every modal form is
  // authored as <div class="form-field"><label>Name</label><input …></div> — the
  // label is a SIBLING, not a wrapper, so without a for/id link it names nothing
  // to a screen reader (and clicking it doesn't focus the field). Wire it here,
  // once, for every modal rather than editing dozens of markup sites. Skip a
  // field the author already wired, and one whose only content is a static value
  // (the "Linked to" rows have a label but no control).
  form.querySelectorAll(".form-field").forEach((field) => {
    const label = field.querySelector("label");
    if (!label || label.hasAttribute("for")) return;
    // First labelable control; a field may also hold a button (the posting-URL
    // Fetch), which the label does not name — inputs/selects/textareas only.
    const control = field.querySelector("input, select, textarea");
    if (!control) return;
    if (!control.id) control.id = `mf-${++fieldSeq}`;
    label.setAttribute("for", control.id);
  });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!validateForm(form)) return;
    if (onSubmit) onSubmit(form);
  });
  // Clear a field's error as soon as it is touched — re-validating on every
  // keystroke would flip the message back and forth while a URL is half typed.
  // `input` covers typing; `change` covers selects and the date/time pickers,
  // which write their value programmatically.
  const clearOnEdit = (event) => clearFieldError(event.target.closest?.(".form-field"));
  form.addEventListener("input", clearOnEdit);
  form.addEventListener("change", clearOnEdit);
  overlay.addEventListener("click", (event) => {
    if (event.target.closest("[data-action='modal-close']")) {
      closeModal(); // explicit Cancel — a deliberate dismissal, never guarded
    } else if (event.target === overlay) {
      if (activeBeforeClose && activeBeforeClose() === false) return; // backdrop — guarded
      closeModal();
    }
  });
  document.addEventListener("keydown", escListener);
  document.addEventListener("keydown", trapFocus);

  document.body.appendChild(overlay);
  activeModal = overlay;
  // First a data field, else the first focusable control (a confirm dialog has
  // no fields — landing on its Cancel button both engages the trap and puts the
  // keyboard user on the safe option).
  const first =
    overlay.querySelector("input, select, textarea") || overlay.querySelector(FOCUSABLE_SELECTOR);
  if (first) first.focus();
  return overlay;
}

function escListener(event) {
  if (event.key !== "Escape") return;
  if (activeBeforeClose && activeBeforeClose() === false) return; // e.g. a dirty compose draft
  closeModal();
}

const FOCUSABLE_SELECTOR =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/* Focus trap (WCAG 2.4.3): keep Tab / Shift+Tab inside the open dialog so focus
   can never fall onto the page behind the overlay. Bubble phase, and scoped to
   focus that is ACTUALLY inside the overlay — a date/time picker popover lives
   on document.body (outside the overlay) and runs its own capture-phase Tab
   cycle with stopPropagation while open, so this handler never sees those
   events and the two traps don't fight. defaultPrevented is a second belt for
   any nested widget that already consumed the key. */
function trapFocus(event) {
  if (event.key !== "Tab" || !activeModal || event.defaultPrevented) return;
  const active = document.activeElement;
  if (!activeModal.contains(active)) return; // in a picker popover, not ours to manage
  const items = [...activeModal.querySelectorAll(FOCUSABLE_SELECTOR)].filter(
    (el) => el.getClientRects().length > 0
  );
  if (!items.length) return;
  const first = items[0];
  const last = items[items.length - 1];
  if (event.shiftKey && active === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && active === last) {
    event.preventDefault();
    first.focus();
  }
}

/* Exit (P5): activeModal is nulled before the fade so the single-active-modal
   convention holds while the old overlay animates out (Esc and a new open see
   "no modal"). Teardown is a timeout, never animationend — a hidden document
   never runs the animation. */
export function closeModal({ instant = false, keepFocus = false } = {}) {
  if (!activeModal) return;
  const overlay = activeModal;
  activeModal = null;
  activeBeforeClose = null;
  document.removeEventListener("keydown", escListener);
  document.removeEventListener("keydown", trapFocus);
  // Return focus to the opener (WCAG 2.4.3). Skipped on the instant swap a
  // replacing openModal performs (keepFocus) so the incoming modal keeps the
  // original target for its own eventual close. Done now, not after the exit
  // animation, so the keyboard user is never parked on the fading overlay.
  if (!keepFocus) restoreFocus();
  if (instant || reducedMotion()) {
    overlay.remove();
    return;
  }
  overlay.classList.add("modal-exit");
  setTimeout(() => overlay.remove(), durationMs("--t-dur-base", 300));
}

/* Refocus the recorded opener, once. Guarded because the opener can be gone —
   a submit that re-renders its view detaches the triggering row/button — in
   which case focus simply stays put rather than throwing on a dead node. */
function restoreFocus() {
  const el = restoreFocusTo;
  restoreFocusTo = null;
  if (el && document.contains(el) && typeof el.focus === "function") el.focus();
}

/** Confirmation dialog; resolves true only on explicit confirm. */
export function confirmModal({ title, message, confirmLabel = "Delete", confirmClass = "btn-danger" }) {
  return new Promise((resolve) => {
    const overlay = openModal({
      title,
      body: `<p>${esc(message)}</p>`,
      footer: `
        <button type="button" class="btn" data-action="modal-close">Cancel</button>
        <button type="submit" class="btn ${confirmClass}">${esc(confirmLabel)}</button>`,
      onSubmit: () => {
        closeModal();
        resolve(true);
      },
    });
    overlay.addEventListener("click", (event) => {
      if (event.target === overlay || event.target.closest("[data-action='modal-close']")) {
        resolve(false);
      }
    });
  });
}

/* List scroll preservation (QA pass 1): paint() rebuilds .list-pane via
   innerHTML, losing scrollTop. Views capture before and restore after. A
   display:none pane (mobile detail open) reads scrollTop 0 — getListScroll
   returns null then so saved state is never clobbered; restoring into a
   hidden pane is a harmless no-op. */
export function getListScroll(root) {
  const pane = root?.querySelector(".list-pane");
  return pane && pane.clientHeight ? pane.scrollTop : null;
}

export function setListScroll(root, top) {
  const pane = root?.querySelector(".list-pane");
  if (pane) pane.scrollTop = top;
}

/* Deep-link mounts (#/jobs/123 from Today, a company page, etc.): paint()
   restores the pane's PREVIOUS scroll position, which says nothing about
   where the route-selected row sits — so the detail loads while the list
   shows an unrelated stretch. Called by views after the mount paint, only
   when the selection came from the route (in-list clicks never yank the
   pane). Top-aligns the row (owner review, 2026-08-05 — centring left it floating
   mid-pane) with a short settle: land SETTLE px shy, then a small smooth
   scroll seats the row's top edge at the pane top — motion that orients
   without a full-height sweep. Scoped to the pane (scrollIntoView could
   scroll ancestors); a hidden pane (mobile detail open) is a no-op. The CSS
   reduced-motion guard can't reach JS scrolling, hence the explicit check.

   The settle is hand-driven rather than scrollTo({behavior:"smooth"}):
   the detail loaders (activities/tailoring/files/top-jobs) each end in a
   paint(), and that innerHTML rebuild destroys the very element the native
   animation is running on — the row just appeared seated, no motion
   (owner review, 2026-08-05). Re-querying the pane every frame makes a mid-flight
   repaint cost one frame instead of the whole animation. */
const SETTLE = 48; /* px of visible travel; inside the owner's 40-60 band */
let settleFrame = 0;
let settleGuard = 0;
let settleCleanup = null;

/** Read a motion duration from the token layer so JS motion retunes with the
    CSS scales (tokens are the single source of truth — CLAUDE.md). */
function durationMs(token, fallback) {
  const raw = getComputedStyle(document.documentElement).getPropertyValue(token).trim();
  const ms = raw.endsWith("ms") ? parseFloat(raw) : raw.endsWith("s") ? parseFloat(raw) * 1000 : NaN;
  return Number.isFinite(ms) && ms > 0 ? ms : fallback;
}

/* The CSS reduced-motion guard collapses animations to 0.01ms, but it cannot
   shorten a JS teardown timer — without this check an exit-animated element
   would sit inert for the full duration doing nothing (P5, same reason
   revealSelected checks it for scrolling). */
function reducedMotion() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function endSettle() {
  cancelAnimationFrame(settleFrame);
  clearTimeout(settleGuard);
  if (settleCleanup) settleCleanup();
  settleCleanup = null;
}

const FLASH = "row-flash";
let flashTimer = 0;

/* One-shot arrival flash on any row-shaped element (the .row-flash::after
   overlay in app.css; the element needs position:relative). One flash lives at
   a time app-wide — a new one strips the old, which is also why the teardown
   timer is shared. Exported for the wizard's wish list (added/moved items get
   the same cue a deep-linked job row gets). */
export function flashRow(el) {
  if (!el) return;
  clearTimeout(flashTimer);
  // Re-adding a class an element already carries does not restart its
  // animation; strip any stale one and force a reflow so it retriggers.
  for (const prev of document.querySelectorAll(`.${FLASH}`)) prev.classList.remove(FLASH);
  void el.offsetWidth;
  el.classList.add(FLASH);
  // animationend is not a reliable teardown here — a hidden document never
  // runs the animation, so it would never fire and the next flash could not
  // retrigger. Time it out instead; the CSS fails safe either way.
  flashTimer = setTimeout(
    () => el.classList.remove(FLASH),
    durationMs("--t-dur-linger", 750) + 150
  );
}

/* Mark the row the settle just delivered (owner review, 2026-08-05: the scroll lands
   it, but nothing tells the eye where it landed). Deliberately NOT called from
   endSettle(): that also runs when a new reveal supersedes an old one and when
   a wheel/touch gesture takes the scroll over, and neither of those is an
   arrival. Only the two COMPLETION paths call this, and they are mutually
   exclusive — whichever finishes first cancels the other. */
function flashSelected(root) {
  flashRow(root?.querySelector(".list-pane .selected"));
}

export function revealSelected(root) {
  const pane = root?.querySelector(".list-pane");
  const row = pane?.querySelector(".selected");
  if (!pane || !row || !pane.clientHeight) return;
  const top = pane.scrollTop + row.getBoundingClientRect().top - pane.getBoundingClientRect().top;
  endSettle();
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    pane.scrollTop = top;
    return;
  }
  const from = Math.max(0, top - SETTLE);
  pane.scrollTop = from;

  // A real scroll gesture wins immediately — native smooth scrolling yields
  // to the user and a hand-driven one has to do the same.
  const stop = () => endSettle();
  window.addEventListener("wheel", stop, { passive: true });
  window.addEventListener("touchstart", stop, { passive: true });
  settleCleanup = () => {
    window.removeEventListener("wheel", stop);
    window.removeEventListener("touchstart", stop);
  };

  const dur = durationMs("--t-dur-base", 300);

  // Correctness backstop: rAF does not run in a hidden document, so without
  // this the row would sit stranded at `from` until the tab is looked at.
  // Position is the requirement; the motion is the nicety — never the
  // reverse. Cancels the tween so a resumed frame can't jump backwards.
  settleGuard = setTimeout(() => {
    const live = root.querySelector(".list-pane");
    endSettle();
    if (live) live.scrollTop = top;
    flashSelected(root); // arrival, just an unanimated one
  }, dur + 150);

  let t0 = 0;
  const step = (now) => {
    if (!t0) t0 = now; // rAF's own clock — no Date/performance (demo pins Date)
    const live = root.querySelector(".list-pane");
    if (!live) return endSettle();
    const p = Math.min(1, (now - t0) / dur);
    live.scrollTop = from + (top - from) * (p * p * (3 - 2 * p)); // smoothstep
    if (p < 1) {
      settleFrame = requestAnimationFrame(step);
    } else {
      endSettle();
      flashSelected(root);
    }
  };
  settleFrame = requestAnimationFrame(step);
}

/* Detail-pane scroll preservation: the same capture/restore as the list pane,
   for the detail side. A full paint() rebuilds .detail-pane via innerHTML, so an
   in-place repaint (expanding a section, changing a settings select) would jump
   the reader back to the top. Null when the pane is hidden (mobile list view) so
   saved state is never clobbered; restoring into a hidden pane is a no-op. */
export function getDetailScroll(root) {
  const pane = root?.querySelector(".detail-pane");
  return pane && pane.clientHeight ? pane.scrollTop : null;
}

export function setDetailScroll(root, top) {
  const pane = root?.querySelector(".detail-pane");
  if (pane) pane.scrollTop = top;
}

/** Search input + custom clear button (QA pass 1: the native webkit cancel
    x is off-style and a tiny tap target). Views handle data-action
    "search" (input) and "search-clear" (click) themselves. */
export function searchBoxHtml(placeholder, value) {
  return `
    <div class="search-wrap">
      <input class="search-box" type="search" aria-label="${esc(placeholder)}" placeholder="${esc(placeholder)}" value="${esc(value)}" data-action="search" />
      <button type="button" class="search-clear${value ? "" : " hide"}" data-action="search-clear" aria-label="Clear search">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>`;
}

/** Single "positive fit" cutoff (QA 2026-06-15): a score at or above this reads
    as a positive/green chip, suppresses the near-miss "maybe" tag (a strong score
    overrides a soft flag), and hides the "Elevate to positive fit" action — the
    one threshold the chip color, the maybe tag, the Today "Maybe" band, and the
    elevate button all agree on. UI-only: the 0–100 model score is unchanged. */
export const POSITIVE_FIT = 70;

/** Fit-score chip: the model's 0–100 score, the manual "elevated" override,
    or a dash when unscored. Shared by Jobs, Today, and the company Top-jobs
    list so the chip colors stay consistent (fit_score=0 is the Tier-1 hard-fail
    sentinel; NULL = not scored yet). Strong at POSITIVE_FIT+ (fit-high), a mixed
    60–69 band (fit-mid), muted below — the hues are cool by design (see
    tokens.css), so the manual describes bands by score, not colour name. */
export function fitChip(job) {
  if (job.manually_elevated) {
    const s = job.fit_score;
    return `<span class="fit-chip fit-elevated" title="Manually elevated${s != null ? ` — model fit score ${s}` : ""}">elevated</span>`;
  }
  if (job.fit_score === null || job.fit_score === undefined) {
    return `<span class="fit-chip fit-none" title="not scored yet">–</span>`;
  }
  const tier = job.fit_score >= POSITIVE_FIT ? "high" : job.fit_score >= 60 ? "mid" : "low";
  return `<span class="fit-chip fit-${tier}">${job.fit_score}</span>`;
}

/** A job whose linked application has resolved (rejected/withdrawn) is done. It
    drops out of the company Top-jobs section, but it STAYS in the Jobs list
    (owner review, 2026-08-13) reading "rejected"/"withdrawn" in the closed-band style —
    a dead application is history you want to see against the company, not a row
    that silently vanishes. job.status stays 'applied' after the application
    resolves (advancing an application never writes back to the job), so this
    keys off application_status, carried on the job payload via the
    JOB_LIST_COLUMNS join. Shared by Jobs + Companies (lives here to avoid a
    jobs↔companies import cycle, like fitChip). */
export function isResolvedApplication(job) {
  return job.application_status === "rejected" || job.application_status === "withdrawn";
}

/** Mirrors MISS_LIMIT in backend/app/ats/refresh.py — the number of consecutive
    refreshes a listing must be absent from its board before it counts as gone.
    Decay flips ACTIVE jobs to status 'closed' at this threshold; APPLIED jobs
    keep their user-owned status and carry the miss count instead, so the count
    is the only signal the UI gets that a req you applied to has been pulled. */
export const MISS_LIMIT = 2;

/** The listing is gone from the board — decay closed it, or it's an applied job
    that has missed MISS_LIMIT refreshes (decay can't flip those without
    destroying the 'applied' state). Note this is about the LISTING, not the
    application: an applied+delisted row still has a live application and must
    not be faded out like a closed one. */
export function isDelisted(job) {
  return job.status === "closed" || (job.status === "applied" && job.miss_count >= MISS_LIMIT);
}

/** A Tier-1 hard fail: the deterministic comp/location/sector gate scored it 0
    (no LLM cost). Hidden by default in Jobs/Today and excluded from every job
    count — UNLESS the user manually elevated it. Strict === 0: a NULL fit_score
    is "not scored yet" and stays visible/counted. Mirrors the backend
    active_job_count filter. */
export function isHardFailFit(job) {
  return job.fit_score === 0 && !job.manually_elevated;
}

/** Minimal markdown → HTML for trusted repo docs (the scoring rubric modal +
    the Help manual). Every line passes esc() before any transform — same XSS
    rule as the rest of the file. Scope matches DATA_DIR/fit_criteria.md: #-####
    headings, **bold**, \`code\`, -/* and 1. lists, > blockquotes, ``` fences.
    HTML comments (editorial markers like <!-- tier2:start -->, and the multi-line
    author-guidance blocks that sit above the machine fences) are dropped whole;
    consecutive > lines merge into one blockquote so multi-line quotes (and bold
    that spans them) render whole; a ```json <name>``` machine block collapses into
    a disclosure so the readable prose leads. Anything else → escaped paragraph. */
export function mdToHtml(markdown) {
  const inline = (s) =>
    esc(s)
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/`([^`]+)`/g, "<code>$1</code>");
  const out = [];
  let list = null; // "ul" | "ol" currently open
  let quote = null; // raw lines of the blockquote being accumulated
  let fence = null; // { info, lines } inside a ``` block
  let comment = false; // inside a multi-line <!-- ... --> block
  const closeList = () => {
    if (list) {
      out.push(`</${list}>`);
      list = null;
    }
  };
  const closeQuote = () => {
    if (quote) {
      // Join before inline() so **bold**/`code` spanning lines resolve.
      out.push(`<blockquote>${inline(quote.join(" "))}</blockquote>`);
      quote = null;
    }
  };
  const closeBlocks = () => {
    closeList();
    closeQuote();
  };
  for (const raw of String(markdown).split("\n")) {
    if (comment) {
      // Inside a multi-line HTML comment (the author guidance above a machine
      // block). Consume until it closes; never rendered.
      if (raw.includes("-->")) comment = false;
      continue;
    }
    if (fence !== null) {
      if (raw.startsWith("```")) {
        const body = fence.lines.join("\n");
        // Every ```json <name>``` machine block collapses into a disclosure, so
        // the prose leads and no config dump (persona, taxonomy, params, …)
        // renders as a bare block. tier1_params keeps its friendlier label
        // because a plain-English summary is spliced in just above it.
        const named = fence.info.match(/^json\s+(\S+)/);
        let html;
        if (named && named[1] === "tier1_params") {
          html = `<details class="md-rawparams"><summary>Raw parameters</summary><pre>${body}</pre></details>`;
        } else if (named) {
          const label = named[1].replace(/_/g, " ");
          html = `<details class="md-rawparams"><summary>Raw ${esc(label)}</summary><pre>${body}</pre></details>`;
        } else {
          html = `<pre>${body}</pre>`;
        }
        out.push(html);
        fence = null;
      } else {
        fence.lines.push(esc(raw));
      }
      continue;
    }
    if (raw.startsWith("```")) {
      closeBlocks();
      fence = { info: raw.slice(3).trim(), lines: [] };
      continue;
    }
    const line = raw.trimEnd();
    if (!line.trim()) {
      closeBlocks();
      continue;
    }
    if (line.trim().startsWith("<!--")) {
      // Editorial HTML comment: a one-liner (the tier2:start/end markers) or the
      // open of a multi-line author-guidance block above a machine fence. Never
      // rendered, and doesn't interrupt an open list/quote.
      if (!line.includes("-->")) comment = true;
      continue;
    }
    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    if (heading) {
      closeBlocks();
      const level = Math.min(heading[1].length + 1, 5); // doc h1 → h2: modal title owns the top level
      out.push(`<h${level}>${inline(heading[2])}</h${level}>`);
      continue;
    }
    const bullet = line.match(/^\s*[-*]\s+(.*)$/);
    if (bullet) {
      closeQuote();
      if (list !== "ul") {
        closeList();
        out.push("<ul>");
        list = "ul";
      }
      out.push(`<li>${inline(bullet[1])}</li>`);
      continue;
    }
    const numbered = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (numbered) {
      closeQuote();
      if (list !== "ol") {
        closeList();
        out.push("<ol>");
        list = "ol";
      }
      out.push(`<li>${inline(numbered[1])}</li>`);
      continue;
    }
    const quoteLine = line.match(/^>\s?(.*)$/);
    if (quoteLine) {
      closeList();
      if (quote === null) quote = [];
      quote.push(quoteLine[1]);
      continue;
    }
    closeBlocks();
    out.push(`<p>${inline(line)}</p>`);
  }
  if (fence !== null) out.push(`<pre>${fence.lines.join("\n")}</pre>`);
  closeBlocks();
  return out.join("");
}

/** Count-aware label: singular at exactly 1, plural otherwise. For count-noun
    stat labels (Company/Companies) so the strip stops reading "1 COMPANIES" —
    the Today banners already singularize (today.js). Adjective labels (New,
    Open, Overdue) need no form and don't use this. */
export function pluralize(n, singular, plural) {
  return n === 1 ? singular : plural;
}

/** Fill the topbar stats slots: [{value, label, title?}, …]. An optional title
    surfaces an exact-timestamp hover on a coarse stat (e.g. "Last refresh"). */
export function setStats(stats) {
  document.getElementById("topbar-stats").innerHTML = stats
    .map(
      (s) => `
      <div class="stat"${s.title ? ` title="${esc(s.title)}"` : ""}>
        <div class="stat-num">${esc(s.value)}</div>
        <div class="stat-label">${esc(s.label)}</div>
      </div>`
    )
    .join("");
}

/** "never" / "3h ago" / "2d ago" — for last-refresh / last-checked chrome.
    Unparseable input reads "unknown", never "NaNd ago": these land in the
    header stat strip, where a malformed stamp would otherwise be the most
    prominent thing on the page. Parsing goes through parseStamp (defined
    below, hoisted) so a zone-less server value is read as UTC here too. */
export function fmtAgo(iso) {
  if (!iso) return "never";
  const at = parseStamp(iso);
  if (!at) return "unknown";
  const hours = Math.round((Date.now() - at.getTime()) / 3600000);
  if (hours < 1) return "under an hour ago";
  if (hours < 24) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

/* Date-hover helpers (shared so every view formats a timestamp the same way and
   the UTC handling lives in one place). The backend writes UTC two ways: Python
   isoformat ("…T..:..:..+00:00", which Date parses correctly) and SQLite
   datetime('now') ("YYYY-MM-DD HH:MM:SS", zone-less — which Date would wrongly
   read as LOCAL, off by the UTC offset). parseStamp treats a zone-less value as
   UTC so both forms render the same local clock time. */
function parseStamp(iso) {
  if (!iso) return null;
  let s = String(iso).trim();
  if (s.includes(" ") && !s.includes("T")) s = s.replace(" ", "T");
  if (!/(Z|[+-]\d\d:?\d\d)$/.test(s)) s += "Z"; // zone-less server value = UTC
  const d = new Date(s);
  return isNaN(d) ? null : d;
}

/** Full local date + time to the minute, for a hover on a real system timestamp
    (first/last seen, applied_at, refresh times, activity logged-at). UTC-aware
    via parseStamp. Returns "" on falsy/unparseable input so callers can omit the
    title=. e.g. "Tue, Jun 10, 2026, 2:30 PM". */
export function fmtStamp(iso) {
  const d = parseStamp(iso);
  return d
    ? d.toLocaleString(undefined, {
        weekday: "short", year: "numeric", month: "short", day: "numeric",
        hour: "2-digit", minute: "2-digit",
      })
    : "";
}

/** Full local date (weekday + year, NO time) for a hover on a date-only value
    with no clock time stored (applied_date, next-step, suggestion dates).
    Parsed as local midnight (the ${iso}T00:00:00 idiom, like fmtDue/fmtDate) to
    avoid a UTC off-by-one. Returns "" on falsy/unparseable input.
    e.g. "Tuesday, June 10, 2026". */
export function fmtFullDate(iso) {
  if (!iso) return "";
  const d = new Date(`${iso}T00:00:00`);
  return isNaN(d)
    ? ""
    : d.toLocaleDateString(undefined, {
        weekday: "long", year: "numeric", month: "long", day: "numeric",
      });
}

/** Local YYYY-MM-DD (en-CA gives ISO order without UTC shifting). Lives here
    (P3, moved from reminderModal) so datepicker.js can use it without a
    module cycle; reminderModal re-exports it for its existing importers.
    Uses a bare new Date(), read at CALL time rather than module scope: a tab
    left open across midnight would otherwise keep reporting yesterday. */
export function localToday(offsetDays = 0) {
  const d = new Date();
  d.setDate(d.getDate() + offsetDays);
  return d.toLocaleDateString("en-CA");
}

/** Standard empty-state box. {html: true} only for pre-escaped markup
    (e.g. a message containing a link); {pad: true} adds the list margin. */
export function emptyState(message, { html = false, pad = false } = {}) {
  return `<div class="empty-state${pad ? " empty-state-pad" : ""}">${html ? message : esc(message)}</div>`;
}

/* The "HQ" wordmark used as the empty-pane watermark — the H plus the Q drawn
   as a magnifier. Geometry is the same as favicon.svg (generated by
   scripts/make_favicon.py); it is duplicated here rather than fetched because
   this is an inline decoration that must take its colour and opacity from
   .detail-empty-mark, and an <img>/<use> of the .svg file could do neither.
   currentColor throughout, so the one CSS rule still owns the treatment. If the
   mark's geometry ever changes, change it in make_favicon.py and mirror it here.

   The viewBox is cropped to the mark's own ink (the favicon's 512 square
   includes a background plate and padding this does not want): x 33->496 is the
   H's left bar to the magnifier handle's outer stroke edge, y 127->477 the
   circle's top to that handle's end. */
export const HQ_MARK = `
  <svg class="hq-mark" viewBox="33 127 463 350" fill="none" role="img" aria-label="Job Search HQ">
    <g fill="currentColor">
      <rect x="33" y="131" width="48" height="229"/>
      <rect x="177" y="131" width="48" height="229"/>
      <rect x="33" y="222" width="192" height="48"/>
    </g>
    <circle cx="357" cy="247" r="91" stroke="currentColor" stroke-width="58"/>
    <line x1="398" y1="334" x2="468" y2="449" stroke="currentColor" stroke-width="56" stroke-linecap="butt"/>
  </svg>`;

/* Every view renders into the same #view node and swaps its event handlers by
   assignment (container.onclick = …). That idiom is NOT safe for focusout: the
   onfocusout PROPERTY is not a GlobalEventHandler in every engine — older
   WebKit ships the focusout EVENT but no property slot, so a property-wired
   blur-save silently never fires there (live-found: careers-URL edits never
   saved in an embedded WebKit pane). One shared slot, replace-on-render:
   each render() installs its handler (or null) via addEventListener. */
let focusOutHandler = null;
export function setFocusOut(container, handler) {
  if (focusOutHandler) container.removeEventListener("focusout", focusOutHandler);
  focusOutHandler = handler;
  if (handler) container.addEventListener("focusout", handler);
}

/* List/selection rows are role="button" divs, not native buttons, so they do
   not activate on Enter/Space on their own (A11Y-01, WCAG 2.1.1 / 4.1.2). This
   is the same replace-on-render slot as setFocusOut: each render() installs its
   dispatcher (or null). The guard fires only when focus is ON a row itself —
   the [role='button'] test skips inner native controls (a reminder row's
   snooze/Done buttons already activate natively), so click and key stay ONE
   code path (dispatch is the view's own onClick, which reads
   event.target.closest("[data-action]")). */
let rowKeyHandler = null;
export function setRowKeys(container, dispatch) {
  if (rowKeyHandler) container.removeEventListener("keydown", rowKeyHandler);
  rowKeyHandler = dispatch
    ? (e) => {
        if (e.key !== "Enter" && e.key !== " ") return;
        if (!e.target.matches?.("[role='button'][data-action]")) return;
        e.preventDefault(); // Space: stop page scroll; Enter: stop any default
        dispatch(e);
      }
    : null;
  if (rowKeyHandler) container.addEventListener("keydown", rowKeyHandler);
}

/** Full-pane loading placeholder, shown while a view's first fetch runs. */
export function renderLoading(container) {
  container.innerHTML = `<div class="detail-empty"><p>Loading…</p></div>`;
}

/** Full-pane load failure with a Retry button. retry() re-renders the view. */
export function renderLoadError(container, error, retry) {
  container.innerHTML = `
    <div class="detail-empty">
      <p>${esc(error.detail || error.message)}</p>
      <button class="btn" data-action="load-retry">Retry</button>
    </div>`;
  container.querySelector("[data-action='load-retry']").onclick = retry;
}

let toastTimer = null;

/* Exit (P5, owner review): fade out on the linger tier rather than blinking out of
   existence — a toast dismissal is attention decaying, exactly what that tier
   is for. Only natural expiry fades; a superseding toast still removes its
   predecessor instantly (both occupy the same fixed position, and a 750ms
   cross-dissolve under the incoming toast reads as a glitch — its own
   toast-in provides the continuity). */
function dismissToast(node, { instant = false } = {}) {
  if (!node.isConnected) return;
  if (instant || reducedMotion()) {
    node.remove();
    return;
  }
  node.classList.add("toast-exit");
  setTimeout(() => node.remove(), durationMs("--t-dur-linger", 750));
}

export function toast(message, { error = false } = {}) {
  document.querySelectorAll(".toast").forEach((t) => dismissToast(t, { instant: true }));
  const node = document.createElement("div");
  node.className = error ? "toast toast-error" : "toast";
  node.setAttribute("role", "status");
  node.setAttribute("aria-live", "polite");
  node.textContent = message;
  document.body.appendChild(node);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => dismissToast(node), error ? 6000 : 3000);
}

/* Popover show/hide with an exit beat (P5). Enter has always been free —
   removing [hidden] re-runs the CSS pop-in keyframe — but [hidden] can't
   animate OUT, so hidePop holds the element visible under .pop-exit for one
   fast-tier beat before actually hiding it. A WeakMap of per-element timers
   makes reopen-during-exit safe: showPop cancels the pending hide, or the
   timer would hide a popover the user just reopened (the help pop is one
   shared node hopping between anchors — that race is real). Callers that
   close because the page is scrolling away pass instant: a fixed-position
   pop fading while its anchor moves reads as detached. */
const popExitTimers = new WeakMap();

export function showPop(el) {
  if (!el) return;
  clearTimeout(popExitTimers.get(el));
  el.classList.remove("pop-exit");
  el.hidden = false;
}

export function hidePop(el, { instant = false } = {}) {
  if (!el || el.hidden) return;
  clearTimeout(popExitTimers.get(el));
  if (instant || reducedMotion()) {
    el.classList.remove("pop-exit");
    el.hidden = true;
    return;
  }
  el.classList.add("pop-exit");
  popExitTimers.set(
    el,
    setTimeout(() => {
      el.classList.remove("pop-exit");
      el.hidden = true;
    }, durationMs("--t-dur-fast", 150))
  );
}

/** An exiting popover is already closed as far as toggle logic is concerned. */
export function isPopOpen(el) {
  return !!el && !el.hidden && !el.classList.contains("pop-exit");
}

/* Detail selection as a history state (7b2): push on list→detail so the phone
   back gesture closes the detail instead of leaving the view; replace on
   detail→detail (desktop row browsing) and on detail→list (e.g. after delete)
   so Back never walks through stale selections. Uses the History API directly
   — pushState/replaceState don't fire hashchange, so the caller's in-place
   paint() stands and only real back/forward traversal re-renders via app.js.
   The hqDetail marker tells close-detail whether we own the previous entry. */
export function setDetailHash(route, id) {
  // Preserve any ?query (the Jobs view's ?company=<id> scope, the Companies
  // ?ats filter) so opening/closing a detail or dismissing a row doesn't silently
  // strip the list scope from the URL and desync it from the in-memory filters.
  const query = location.hash.match(/\?.*$/)?.[0] || "";
  const target = id ? `#/${route}/${id}${query}` : `#/${route}${query}`;
  if (location.hash === target) return;
  const inDetail = /^#\/\w+\/\d+/.test(location.hash);
  const method = id && !inDetail ? "pushState" : "replaceState";
  history[method]({ hqDetail: !!id }, "", target);
}

/* Shared placement for body-anchored fixed popovers (help hints, the date
   picker): viewport coords from getBoundingClientRect — shows below the
   anchor, flips above when it would overflow the bottom, clamps to an 8px
   inset on every edge. Callers close on scroll, so the fixed position never
   goes stale (extracted from helpHint in P3). */
export function placeFixed(pop, anchorEl, gap = 6) {
  const r = anchorEl.getBoundingClientRect();
  pop.style.left = "0px";
  pop.style.top = "0px";
  const pw = pop.offsetWidth;
  const ph = pop.offsetHeight;
  const vw = document.documentElement.clientWidth;
  const vh = document.documentElement.clientHeight;
  let left = r.left;
  if (left + pw > vw - 8) left = vw - 8 - pw;
  if (left < 8) left = 8;
  let top = r.bottom + gap;
  if (top + ph > vh - 8) top = r.top - ph - gap;
  if (top < 8) top = 8;
  pop.style.left = `${Math.round(left)}px`;
  pop.style.top = `${Math.round(top)}px`;
}
