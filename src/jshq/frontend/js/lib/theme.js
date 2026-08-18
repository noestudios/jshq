/* Theme switching (P2). The inline boot script in index.html <head> does the
   first, pre-stylesheet application so a stored light preference never flashes
   dark; this module owns everything after that — the Settings control writes
   the preference, and a matchMedia listener follows live OS changes while the
   preference is "system".

   localStorage "hq_theme": "system" | "light" | "dark"; absent = DEFAULT_PREF.

   DEFAULT_PREF is "dark", not "system": dark is the app's primary design
   theme, and the theme control lives in Settings where a first-time user
   won't have found it yet — so first launch lands on the designed-first look
   rather than whichever OS theme happens to be set. Choosing System (or
   light) in Settings is sticky from then on.

   "system" is now STORED EXPLICITLY rather than represented by absence, and it
   has to be: absence means dark, so the old removeItem() convention would have
   made the System option unreachable — choosing it would have resolved straight
   back to dark. This is the one place this module diverges from the
   absent-equals-default convention hq_notify_sound uses, and that is why.

   The boot script in index.html mirrors this logic exactly. Change both. */

const THEME_KEY = "hq_theme";
const DEFAULT_PREF = "dark";
const PREFS = ["system", "light", "dark"];
const mq = matchMedia("(prefers-color-scheme: light)");

export function getThemePref() {
  const v = localStorage.getItem(THEME_KEY);
  return PREFS.includes(v) ? v : DEFAULT_PREF;
}

export function setThemePref(pref) {
  if (PREFS.includes(pref)) localStorage.setItem(THEME_KEY, pref);
  else localStorage.removeItem(THEME_KEY);
  applyTheme();
}

export function applyTheme() {
  const pref = getThemePref();
  const theme = pref === "system" ? (mq.matches ? "light" : "dark") : pref;
  document.documentElement.dataset.theme = theme;
  /* Keep mobile chrome in sync. Reading the real token value (rather than a
     second hardcoded pair) means the boot script's hexes are the only copy
     that could drift, and this corrects them after first paint. */
  const meta = document.querySelector('meta[name="theme-color"]');
  const bg = getComputedStyle(document.documentElement).getPropertyValue("--t-bg-elev").trim();
  if (meta && bg) meta.content = bg;
}

mq.addEventListener("change", () => {
  if (getThemePref() === "system") applyTheme();
});
