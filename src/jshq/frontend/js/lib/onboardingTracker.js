/* Persistent onboarding-completeness tracker (Phase 4): the "Setup N/total" pill
   in the topbar. Goal-gradient chrome — as long as setup steps are open it stays
   visible on every view, and clicking it lands on #/welcome, whose welcome-back
   hub jumps straight to any step. Dismissing the WIZARD does not hide it, but the
   pill now carries its own "I'm set — hide this" ✕ (FLOW-02): optional steps
   (hard filters / matrix / wish list) are content-derived, so leaving one blank
   on purpose keeps it done:false forever — the nudge would never clear. The ✕
   persists the acknowledgement (settings row onboarding_tracker_dismissed);
   readiness counts are untouched, only the nudge is suppressed.

   Owns the #onboarding-tracker node's innerHTML the way setStats() owns
   #topbar-stats — a SEPARATE node, because setStats() rewrites that one on
   every view paint. Counts the backend's readiness verbatim (complete_count /
   total from GET /api/onboarding); no client-side recount to drift.

   seed() paints from boot()'s already-fetched payload (no second request);
   refresh() is fire-and-forget on navigation and after mutations that bypass
   render() (companies.js's add uses pushState, which never fires hashchange),
   and repaints only when the visible signature actually changes, so unchanged
   navigations never flicker the pill. */

import { api } from "../api.js";
import { toast } from "./ui.js";

const NODE_ID = "onboarding-tracker";
let last = null; // last payload painted from
let primed = false; // boot seeded us; skip exactly one render()-driven refetch

export function seed(payload) {
  last = payload;
  primed = true;
  paint();
}

export async function refresh() {
  if (primed) {
    primed = false; // boot's payload is fresh enough for the first render()
    return;
  }
  let payload;
  try {
    payload = await api.getOnboarding();
  } catch {
    return; // transient: keep whatever is shown
  }
  const changed = sig(payload) !== sig(last);
  last = payload;
  if (changed) paint();
}

/* Show only when there is something to nudge about: never over a broken
   criteria doc (the counts would be an artificial undercount), never during
   first-run (the whole screen IS the wizard), never when complete (nothing
   left), and never once the user acknowledged with the pill's own ✕. */
function shouldShow(p) {
  return (
    !!p &&
    !p.criteria_error &&
    !p.first_run &&
    !p.tracker_dismissed &&
    p.complete_count < p.total
  );
}

function sig(p) {
  return p ? `${shouldShow(p)}:${p.complete_count}/${p.total}` : "none";
}

function paint() {
  const node = document.getElementById(NODE_ID);
  if (!node) return;
  if (!shouldShow(last)) {
    node.innerHTML = "";
    node.hidden = true; // display:none — no box, no synthesized baseline
    return;
  }
  const { complete_count: done, total } = last;
  const pct = Math.round((done / total) * 100);
  node.hidden = false;
  // The pill LINKS, so its ✕ can't nest inside the anchor (invalid, and the click
  // would also follow the link). They ride as siblings in an inline-flex wrapper;
  // the ✕ reuses .banner-dismiss (24px A11Y-06 tap target, tokens-only) with an
  // inline position:static so it flows beside the pill instead of absolute-anchoring.
  node.innerHTML = `<span class="ob-tracker-wrap" style="display:inline-flex;align-items:center;gap:0.25rem">
    <a class="ob-tracker" href="#/welcome"
        aria-label="Setup: ${done} of ${total} steps complete. Finish setting up.">
      <span class="ob-tracker-label" aria-hidden="true">Setup ${done}/${total}</span>
      <span class="ob-tracker-bar" aria-hidden="true"><span class="ob-tracker-fill" style="width:${pct}%"></span></span>
    </a>
    <button type="button" class="banner-dismiss" data-action="dismiss-tracker"
        style="position:static" aria-label="Hide setup tracker">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
    </button>
  </span>`;
  node.querySelector('[data-action="dismiss-tracker"]').addEventListener("click", dismiss);
}

/* "I'm set — hide this": drop the pill immediately (optimistic — repaint from a
   dismissed copy of the payload, which shouldShow() now hides) and persist the
   acknowledgement. A failed save used to be swallowed (F6): the pill vanished
   for the session and quietly came back on the next one, which reads as the ✕
   being broken. Now the failure reverts the pill and says why. */
function dismiss() {
  if (last) last = { ...last, tracker_dismissed: true };
  paint();
  api.putSetting("onboarding_tracker_dismissed", true).catch(() => {
    if (last) last = { ...last, tracker_dismissed: false };
    paint();
    toast("Couldn't save that — the setup pill stays for now. Try the ✕ again.", { error: true });
  });
}
