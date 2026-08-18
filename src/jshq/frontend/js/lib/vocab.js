/* The job-search vocabulary, served from the criteria doc (Phase 2).

   Level bands, quadrant/tension labels, disciplines and near-miss flag
   values are the user's taxonomy, not ours — the backend derives them from the
   criteria doc and serves them from GET /api/scoring/vocab. Views read the
   accessors below instead of keeping their own copies, which is how the jobs
   view came to be missing the "junior" band the backend was already emitting:
   junior-banded roles were unfilterable and their label fell through to the
   raw token.

   Fetched ONCE per page load and cached as a promise; app.js awaits it before
   any view paints, so nothing renders a raw token like "senior_director" and
   no view re-fetches on render. A failed fetch is not fatal — every accessor
   falls back to the literals below (the values the views hardcoded before this
   module existed), so the UI still renders offline or against a dead API.
   Same-origin only: this adds no new network destination. */

import { api } from "../api.js";

/* Last-resort vocabulary, STRUCTURALLY neutral (Phase 5b): level bands and
   fixed label keys only — no field-of-work vocabulary. The old fallback
   carried the shipped example's design taxonomy, so any install whose one
   vocab fetch failed silently rendered another person's career. It is a
   fallback, never a claim about this user's doc — anything real comes from
   the endpoint. level_bands is most-senior-first, matching the doc order the
   API preserves; "junior" is included because its absence was the bug this
   module fixes. */
const FALLBACK = {
  level_bands: [
    { value: "vp_plus", label: "VP+" },
    { value: "distinguished", label: "Distinguished" },
    { value: "principal", label: "Principal" },
    { value: "senior_staff", label: "Sr Staff" },
    { value: "staff", label: "Staff" },
    { value: "senior_director", label: "Sr Director" },
    { value: "director", label: "Director" },
    { value: "senior_manager", label: "Sr Manager" },
    { value: "manager", label: "Manager" },
    { value: "ic", label: "IC" },
    { value: "junior", label: "Junior" },
  ],
  quadrant_labels: {
    energizing_strength: "energizing · strength",
    energizing_growth: "energizing · growth",
    draining_growth: "draining · growth",
    draining_strength: "draining · strength",
  },
  // Fixed KEYS (stored values); the display strings stay field-neutral —
  // "the core work vs. selling it" is the axis in any profession.
  tension_labels: {
    teach_craft: "the work itself",
    convert_sell: "selling it",
    mixed: "mixed",
  },
  // The two reserved tokens every taxonomy must carry; a user's own fields
  // arrive only from the endpoint.
  disciplines: ["other", "unclear"],
  in_band_disciplines: [],
  flag_values: ["below_band", "scope_gap"],
  criteria_error: null,
};

let vocab = FALLBACK;
let pending = null; // the single in-flight/settled fetch — never re-issued

/* Fold the payload over the fallback field by field, so a partial or malformed
   response degrades one key at a time instead of all-or-nothing. */
function merge(payload) {
  const p = payload || {};
  const list = (value, fallback) => (Array.isArray(value) && value.length ? value : fallback);
  const map = (value, fallback) =>
    value && typeof value === "object" && Object.keys(value).length ? value : fallback;
  return {
    level_bands: list(p.level_bands, FALLBACK.level_bands).filter((b) => b && b.value),
    quadrant_labels: map(p.quadrant_labels, FALLBACK.quadrant_labels),
    tension_labels: map(p.tension_labels, FALLBACK.tension_labels),
    disciplines: list(p.disciplines, FALLBACK.disciplines),
    // Served by the backend (main.py) as part of the taxonomy; carried through
    // so the vocabulary module exposes the whole contract, not a subset.
    in_band_disciplines: list(p.in_band_disciplines, FALLBACK.in_band_disciplines),
    flag_values: list(p.flag_values, FALLBACK.flag_values),
    // Present only when the doc is broken; anything non-string is "no error".
    criteria_error: typeof p.criteria_error === "string" ? p.criteria_error : null,
  };
}

/* Resolves to the vocabulary in force. Never rejects: an unreachable API leaves
   the fallback in place, because half-labelled UI beats no UI. Idempotent —
   later calls reuse the same promise, so awaiting it per render costs a
   microtask and never a request. */
export function loadVocab() {
  if (pending) return pending;
  pending = api
    .getScoringVocab()
    .then((payload) => {
      vocab = merge(payload);
      return vocab;
    })
    .catch(() => vocab);
  return pending;
}

/* De-duplicated by value, first occurrence wins, doc order preserved (Map keeps
   insertion order). The ladder legitimately lists a band twice — program titles
   like "intern" outrank the seniority words, so "junior" sits both above and
   below them — but a filter must offer each band exactly once. */
export function levelBands() {
  const byValue = new Map();
  for (const band of vocab.level_bands) {
    if (!byValue.has(band.value)) byValue.set(band.value, band);
  }
  return [...byValue.values()];
}

/* Display label for a stored level_band token. Falls back to the raw token
   (better a legible token than a blank cell) and to an em dash when unset. */
export function levelLabel(value) {
  return vocab.level_bands.find((b) => b.value === value)?.label || value || "—";
}

export function quadrantLabel(key) {
  return vocab.quadrant_labels[key] || key || "";
}

export function tensionLabel(key) {
  return vocab.tension_labels[key] || key || "";
}

export function flagValues() {
  return [...vocab.flag_values];
}

export function disciplines() {
  return [...vocab.disciplines];
}

/* The disciplines that count as on-target for the level bands (the rest are
   scored as adjacent). Part of the served taxonomy; exposed for parity with
   disciplines() so a view never has to re-derive it. */
export function inBandDisciplines() {
  return [...vocab.in_band_disciplines];
}

/* The criteria doc failed to parse: this vocabulary is the shipped fallback
   while scoring is separately hard-failing on the same doc. Settings surfaces
   the message; null when the doc is fine. */
export function criteriaError() {
  return vocab.criteria_error;
}
