/* Shared ATS helpers used by the Today banners and the Companies view. */

// Bulk "retry failed boards" affordances (Companies toolbar button, Today
// banner action) appear only when MORE than this many boards are failing —
// below that, the per-company ↻ is quicker than a bulk run.
export const BULK_RETRY_MIN = 3;

/* Humanize an adapter failure reason (the recorded error text) for the refresh
   banners / completion bar / Companies view — the raw text is techy (URLs, HTTP
   codes, Python exception classes). Pure; matches substrings so the leading
   "error: " prefix on a live ats_last_status is harmless. Specific HTTP codes
   before the generic 5xx; "unexpected:" is fixed text so an exception class name
   never leaks. */
export function failReason(raw) {
  const r = String(raw || "");
  if (/\b401\b/.test(r)) return "needs a login (401)";
  if (/\b403\b/.test(r)) return "blocked (403)";
  if (/\b404\b/.test(r)) return "not found (404)";
  if (/\b410\b/.test(r)) return "gone (410)";
  if (/\b429\b/.test(r)) return "rate-limited (429)";
  if (/\b5\d\d\b/.test(r)) return "server error";
  if (/bad JSON/i.test(r)) return "unexpected response";
  if (/timeout/i.test(r)) return "timed out";
  if (/Connect|nodename|name resolution|unreachable/i.test(r)) return "couldn't reach it";
  if (/unexpected:/i.test(r)) return "unexpected error";
  return r.length > 120 ? r.slice(0, 117) + "…" : r;
}
