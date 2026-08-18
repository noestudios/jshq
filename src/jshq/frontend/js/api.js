/* Fetch wrapper. Paths are relative so the app works identically through
   Apache :8081 (ProxyPass /api) and uvicorn :8000 directly. */

export class ApiError extends Error {
  constructor(status, detail, info = null) {
    super(detail);
    this.status = status;
    this.detail = detail;
    // The raw structured detail when the API returns one (e.g. the 409 from
    // POST /api/jobs carries {message, job_id, status} so the Add-job UI can
    // offer to reactivate the existing job). null for plain string details.
    this.info = info;
  }
}

// Abort a fetch that never answers (e.g. a connection accepted during an API restart
// that never responds) so the caller's await always settles. A hung fetch would
// otherwise leave in-flight UI state (state.tailorBusy) stuck forever. Matched to
// Apache's 300s /api ProxyTimeout so the client never cuts off a legitimately-slow
// tailor/compose call before the proxy does — both are safety nets, not the norm.
const REQUEST_TIMEOUT_MS = 300_000;

async function request(method, path, body) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  let response;
  try {
    response = await fetch(path, {
      method,
      headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: controller.signal,
    });
  } catch (error) {
    throw error.name === "AbortError"
      ? new ApiError(0, "Request timed out — the server may have restarted or stalled. Try again.")
      : new ApiError(0, "API unreachable — is the backend running?");
  } finally {
    clearTimeout(timer);
  }
  if (!response.ok) {
    let detail = null;
    let info = null;
    try {
      const payload = await response.json();
      if (payload.detail) {
        if (typeof payload.detail === "string") {
          detail = payload.detail;
        } else if (Array.isArray(payload.detail)) {
          // FastAPI validation errors
          detail = payload.detail.map((e) => `${e.loc.join(".")}: ${e.msg}`).join("; ");
        } else {
          // a structured detail object (e.g. the already-tracked 409)
          info = payload.detail;
          detail = payload.detail.message ?? JSON.stringify(payload.detail);
        }
      }
    } catch {
      /* non-JSON error body (e.g. an Apache error page) */
    }
    if (detail === null) {
      // 502/503/504 with no API-shaped body = Apache answered but the proxied
      // request didn't complete — the backend is slow/busy (a long generation),
      // restarting, or down. Don't assert "down"; a slow tailor/compose is the
      // common case.
      detail = [502, 503, 504].includes(response.status)
        ? "The server didn't finish in time — it may be busy or restarting. Try again in a moment."
        : `${response.status} ${response.statusText}`;
    }
    throw new ApiError(response.status, detail, info);
  }
  return response.json();
}

export const api = {
  listJobs: ({ company_id } = {}) =>
    request("GET", `/api/jobs${company_id ? `?company_id=${company_id}` : ""}`),
  getJob: (id) => request("GET", `/api/jobs/${id}`),
  createJob: (body) => request("POST", "/api/jobs", body),
  parseJobUrl: (url) => request("POST", "/api/jobs/parse-url", { url }),
  setJobStatus: (id, status) => request("PATCH", `/api/jobs/${id}`, { status }),
  dismissJob: (id, reason, note) =>
    request("PATCH", `/api/jobs/${id}`, {
      status: "dismissed",
      reason: reason || undefined,
      note: note || undefined,
    }),
  elevateJob: (id, elevated) => request("POST", `/api/jobs/${id}/elevate`, { elevated }),
  updateJobDetails: (id, body) => request("PATCH", `/api/jobs/${id}/details`, body),
  getSetting: (key) => request("GET", `/api/settings/${key}`),
  putSetting: (key, value) => request("PUT", `/api/settings/${key}`, { value }),
  // Anthropic key: status in, key never out. See apikey.py — the value lives in
  // <data dir>/.env on this machine and is sent only to api.anthropic.com.
  getApiKeyStatus: () => request("GET", "/api/settings/api-key"),
  putApiKey: (key) => request("PUT", "/api/settings/api-key", { key }),
  deleteApiKey: () => request("DELETE", "/api/settings/api-key"),
  testApiKey: () => request("POST", "/api/settings/api-key/test"),
  getSuggestions: () => request("GET", "/api/suggestions"),
  actOnSuggestion: (keyword, action) =>
    request("POST", "/api/suggestions/title-exclude", { keyword, action }),
  refreshStatus: () => request("GET", "/api/refresh/status"),
  backupStatus: () => request("GET", "/api/backup/status"),
  // body is optional: {scope: "failed"} retries only the failing boards.
  triggerRefresh: (body) => request("POST", "/api/refresh", body),
  listCompanies: () => request("GET", "/api/companies"),
  getCompany: (id) => request("GET", `/api/companies/${id}`),
  refreshCompanyBoard: (id) => request("POST", `/api/companies/${id}/refresh`),
  detectCompanyBoard: (id) => request("POST", `/api/companies/${id}/detect`),
  previewCareers: (body) => request("POST", "/api/companies/careers-preview", body),
  refreshCompanyLogo: (id) => request("POST", `/api/companies/${id}/logo/refresh`),
  createCompany: (body) => request("POST", "/api/companies", body),
  updateCompany: (id, body) => request("PUT", `/api/companies/${id}`, body),
  deleteCompany: (id) => request("DELETE", `/api/companies/${id}`),
  listReminders: () => request("GET", "/api/reminders"),
  createReminder: (body) => request("POST", "/api/reminders", body),
  updateReminder: (id, body) => request("PUT", `/api/reminders/${id}`, body),
  patchReminder: (id, body) => request("PATCH", `/api/reminders/${id}`, body),
  deleteReminder: (id) => request("DELETE", `/api/reminders/${id}`),
  listActivities: (params = {}) => {
    const qs = new URLSearchParams(
      Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== "")
    ).toString();
    return request("GET", `/api/activities${qs ? `?${qs}` : ""}`);
  },
  createActivity: (body) => request("POST", "/api/activities", body),
  compose: (body) => request("POST", "/api/compose", body),
  refineTells: (body) => request("POST", "/api/refine-tells", body),
  actOnReminderSuggestion: (key, action) =>
    request("POST", "/api/suggestions/reminder", { key, action }),
  getCriteriaDoc: () => request("GET", "/api/scoring/criteria-doc"),
  getUserManual: () => request("GET", "/api/docs/user-manual"),
  // The editable voice guide (Phase 3): prose the AI writes with.
  getVoiceGuide: () => request("GET", "/api/docs/voice-guide"),
  putVoiceGuide: (markdown) => request("PUT", "/api/docs/voice-guide", { markdown }),
  getCriteria: () => request("GET", "/api/scoring/criteria"),
  // The display vocabulary derived from the criteria doc (lib/vocab.js owns the
  // caching — call that, not this).
  getScoringVocab: () => request("GET", "/api/scoring/vocab"),
  putCriteria: (body) => request("PUT", "/api/scoring/criteria", body),
  // Persona: who the AI prompts are written for (display_name + domain_label).
  getPersona: () => request("GET", "/api/scoring/persona"),
  putPersona: (body) => request("PUT", "/api/scoring/persona", body),
  // The onboarding wizard's field step: declares the user's field as the in-band
  // discipline so scoring targets it, not the design default.
  putDiscipline: (field) => request("PUT", "/api/scoring/discipline", { field }),
  getCriteriaExample: () => request("GET", "/api/scoring/criteria-example"),
  // Onboarding (Phase 4): first-run + readiness aggregate, the skip/finish state,
  // and the raw-exercise roadmap store (the user's wishlist + matrix, verbatim).
  getOnboarding: () => request("GET", "/api/onboarding"),
  putOnboarding: (body) => request("PUT", "/api/onboarding", body),
  getRoadmap: () => request("GET", "/api/onboarding/roadmap"),
  getSynthesis: () => request("GET", "/api/scoring/synthesis"),
  getSynthesisPrompt: () => request("GET", "/api/scoring/synthesis/prompt"),
  proposeSynthesis: () => request("POST", "/api/scoring/synthesis"),
  submitSynthesisReply: (reply) => request("POST", "/api/scoring/synthesis/reply", { reply }),
  applySynthesis: (body) => request("POST", "/api/scoring/synthesis/apply", body),
  discardSynthesis: () => request("DELETE", "/api/scoring/synthesis"),
  putRoadmap: (body) => request("PUT", "/api/onboarding/roadmap", body),
  geocode: (q) => request("GET", `/api/scoring/geocode?q=${encodeURIComponent(q)}`),
  getInclusionRules: () => request("GET", "/api/inclusion-rules"),
  putInclusionRules: (body) => request("PUT", "/api/inclusion-rules", body),
  // Semantic JD/role-mismatch learned rules (Phase 7i, scoring layer).
  proposeScoringRule: (jobId) => request("POST", `/api/jobs/${jobId}/scoring-rule-proposal`),
  actOnScoringProposal: (id, action) =>
    request("POST", "/api/suggestions/scoring-rule", { id, action }),
  getScoringRules: () => request("GET", "/api/scoring-rules"),
  removeScoringRule: (id) => request("DELETE", `/api/scoring-rules/${id}`),
  rescore: () => request("POST", "/api/scoring/rescore"),
  rescoreEstimate: () => request("GET", "/api/scoring/rescore-estimate"),
  tailorApplication: (id, body = {}) => request("POST", `/api/applications/${id}/tailor`, body),
  getTailoring: (id) => request("GET", `/api/applications/${id}/tailoring`),
  patchTailoring: (id, body) => request("PATCH", `/api/tailorings/${id}`, body),
  applyTailoring: (id) => request("POST", `/api/tailorings/${id}/apply`),
  rerenderCover: (id, body) => request("POST", `/api/tailorings/${id}/rerender`, body),
  discardTailoring: (id) => request("POST", `/api/tailorings/${id}/discard`),
  chatTailoring: (id, body) => request("POST", `/api/tailorings/${id}/chat`, body),
  getTailoringMessages: (id) => request("GET", `/api/tailorings/${id}/messages`),
  listApplications: () => request("GET", "/api/applications"),
  getApplication: (id) => request("GET", `/api/applications/${id}`),
  createApplication: (body) => request("POST", "/api/applications", body),
  updateApplication: (id, body) => request("PUT", `/api/applications/${id}`, body),
  deleteApplication: (id) => request("DELETE", `/api/applications/${id}`),
  listApplicationFiles: (id) => request("GET", `/api/applications/${id}/files`),
  deleteApplicationFile: (id, name) =>
    request("DELETE", `/api/applications/${id}/files/${encodeURIComponent(name)}`),
  // Raw-body PUT (a File/Blob) — request() would JSON-encode it, so this one
  // drives fetch directly; errors funnel through the same ApiError shape.
  uploadApplicationFile: async (id, file) => {
    let response;
    try {
      response = await fetch(`/api/applications/${id}/files/${encodeURIComponent(file.name)}`, {
        method: "PUT",
        body: file,
      });
    } catch {
      throw new ApiError(0, "API unreachable — is the backend running?");
    }
    if (!response.ok) {
      let detail = `${response.status} ${response.statusText}`;
      try {
        const payload = await response.json();
        if (typeof payload.detail === "string") detail = payload.detail;
      } catch {
        /* non-JSON error body */
      }
      throw new ApiError(response.status, detail);
    }
    return response.json();
  },
  listContacts: () => request("GET", "/api/contacts"),
  createContact: (body) => request("POST", "/api/contacts", body),
  updateContact: (id, body) => request("PUT", `/api/contacts/${id}`, body),
  deleteContact: (id) => request("DELETE", `/api/contacts/${id}`),
};
