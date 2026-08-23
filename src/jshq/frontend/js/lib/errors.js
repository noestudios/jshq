/* Error presentation (error-audit Wave 1): one place that turns a caught
   error into a sentence fit for a toast or inline line.

   The backend is progressively rewriting its details as human sentences that
   end in a stable [JSHQ-###] code (src/jshq/errors.py) — those pass through
   untouched, code and all. Everything else (missing details, non-ApiError
   throws like a JSON SyntaxError) gets the caller's context sentence plus a
   status-shaped diagnosis instead of engine text or "[object Object]". */

const STATUS_DEFAULTS = {
  0: "Can't reach the app — is the backend still running?",
  401: "The server refused that request.",
  403: "The server refused that request.",
  404: "That wasn't found — it may have been removed. Refresh and try again.",
  409: "That conflicts with something already saved.",
  413: "That's too large.",
  422: "That input couldn't be saved — check the form and try again.",
  429: "Rate limited — wait a moment and try again.",
};

/* The numeric [JSHQ-###] code on a server detail, or null. Lets inline
   mappers match on the code instead of the prose (which is free to change). */
export function errorCode(error) {
  const detail = typeof error?.detail === "string" ? error.detail : "";
  const match = /\[JSHQ-(\d{3})\]\s*$/.exec(detail);
  return match ? Number(match[1]) : null;
}

/* Every code in the detail, in order. Validation 422s can carry a coded
   validator sentence inside the joined message (e.g. the location-exclude
   rule), so mappers check membership here rather than only the trailing
   code. */
export function errorCodes(error) {
  const detail = typeof error?.detail === "string" ? error.detail : "";
  return [...detail.matchAll(/\[JSHQ-(\d{3})\]/g)].map((m) => Number(m[1]));
}

export function humanizeApiError(error, fallback = "") {
  const detail = typeof error?.detail === "string" ? error.detail.trim() : "";
  if (detail) return detail;
  const status = typeof error?.status === "number" ? error.status : null;
  const diagnosis =
    status === null
      ? ""
      : STATUS_DEFAULTS[status] ||
        (status >= 500 ? "The server hit a problem — try again in a moment." : "");
  return [fallback, diagnosis].filter(Boolean).join(" ") || "Something went wrong. Try again.";
}
