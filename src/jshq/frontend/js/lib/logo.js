/* Company logo / monogram primitive — shared by every view that shows a company.

   Renders a deterministic initials monogram as the always-present baseline; when
   a cached logo URL is present, an <img> overlays it and falls back to the
   monogram on error (onerror removes the img, revealing the initials beneath).
   Offline-safe: with no URL (or a broken one) the monogram always shows.

   Monograms are neutral since P2 (all .co-mono-N share --t-tag-*); monoIndex
   still hashes the name to a stable class so a decorative palette can be
   restored by re-fanning the one app.css rule — no JS change. */

import { esc } from "./ui.js";

// Hash bucket count — matches the .co-mono-0..8 classes in app.css.
const MONO_COUNT = 9;

// Dropped when deriving initials so "The Acme Group" → "AC", not "TA".
const SKIP_WORDS = new Set([
  "the", "a", "an", "and", "of", "for", "inc", "llc", "ltd", "co", "corp",
  "group", "company", "foundation", "labs", "studio", "technologies",
]);

/* 1–2 uppercase initials from the significant words of a company name. Falls
   back to the first one or two characters when there are no usable words. */
export function initials(name) {
  const words = String(name || "")
    .split(/[\s/]+/)
    .map((w) => w.replace(/[^A-Za-z0-9]/g, ""))
    .filter((w) => w && !SKIP_WORDS.has(w.toLowerCase()));
  if (words.length >= 2) return (words[0][0] + words[1][0]).toUpperCase();
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  const bare = String(name || "").replace(/[^A-Za-z0-9]/g, "");
  return (bare.slice(0, 2) || "?").toUpperCase();
}

/* Stable small hash → palette index, so a company always gets the same hue. */
export function monoIndex(name) {
  const s = String(name || "");
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
  return ((h % MONO_COUNT) + MONO_COUNT) % MONO_COUNT;
}

/* HTML for a company avatar. `company` = { name, logo } where `logo` is the
   server-generated endpoint URL (/api/companies/{id}/logo) or null/undefined.
   size: "sm" (list rows) | "lg" (detail headers). Decorative — the company name
   is always rendered as text alongside, so the avatar is aria-hidden.

   `logo` is escaped with esc() (not escUrl): it is our own backend path, never
   user input, and escUrl rejects relative URLs. */
export function companyLogoHtml(company = {}, { size = "sm" } = {}) {
  const name = company.name;
  const cls = `co-logo co-logo--${size} co-mono-${monoIndex(name)}`;
  const img = company.logo
    ? `<img class="co-logo-img" src="${esc(company.logo)}" alt="" loading="lazy" onerror="this.remove()">`
    : "";
  return `<span class="${cls}" title="${esc(name || "")}" aria-hidden="true">${esc(initials(name))}${img}</span>`;
}
