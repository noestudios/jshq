/* Shared add-contact modal (extracted from contacts.js in 7b2 so the company
   detail can add a contact too). The companies list is passed in — the caller
   may be a view that loaded before the contacts view ever did — and the
   caller decides what happens after creation via onCreated. */

import { api } from "../api.js";
import { closeModal, esc, openModal, toast } from "./ui.js";

/* How-you-met vocabulary: the contact_sources setting (editable in Settings),
   fetched once per session like jobs.js's dismiss reasons. The fallback only
   covers an unreachable API; an emptied setting stays empty ([] is truthy). */
const FALLBACK_SOURCES = ["linkedin", "referral", "event", "other"];
let sources = null;

export async function contactSources() {
  if (!sources) {
    try {
      sources = (await api.getSetting("contact_sources")).value || FALLBACK_SOURCES;
    } catch {
      sources = FALLBACK_SOURCES;
    }
  }
  return sources;
}

export function companyOptionsHtml(companies, currentId) {
  return companies
    .slice()
    .sort((a, b) => a.name.localeCompare(b.name))
    .map((c) => `<option value="${c.id}"${c.id === currentId ? " selected" : ""}>${esc(c.name)}</option>`)
    .join("");
}

export async function openContactModal({ companies, companyId = null, onCreated }) {
  const sourceOptions = await contactSources();
  openModal({
    title: "Add contact",
    body: `
      <p class="form-req-note"><span class="req-mark" aria-hidden="true">*</span> required</p>
      <div class="form-field"><label>Name <span class="req-mark" aria-hidden="true">*</span></label><input name="name" required /></div>
      <div class="form-field"><label>Company</label><select name="company_id"><option value="">—</option>${companyOptionsHtml(companies, companyId)}</select></div>
      <div class="form-field"><label>Role</label><input name="role" /></div>
      <div class="form-field"><label>LinkedIn URL</label><input name="linkedin_url" type="url" placeholder="https://www.linkedin.com/in/…" /></div>
      <div class="form-field"><label>Email</label><input name="email" type="email" /></div>
      <div class="form-field"><label>Source</label><select name="source"><option value="">—</option>${sourceOptions.map((s) => `<option value="${esc(s)}">${esc(s)}</option>`).join("")}</select></div>
      <div class="form-field"><label>Relationship notes</label><textarea name="relationship_notes"></textarea></div>`,
    footer: `
      <button type="button" class="btn" data-action="modal-close">Cancel</button>
      <button type="submit" class="btn btn-accent">Add contact</button>`,
    onSubmit: async (form) => {
      const data = Object.fromEntries(new FormData(form));
      try {
        const created = await api.createContact({
          name: data.name,
          company_id: data.company_id ? Number(data.company_id) : null,
          role: data.role || null,
          linkedin_url: data.linkedin_url || null,
          email: data.email || null,
          source: data.source || null,
          relationship_notes: data.relationship_notes || null,
        });
        closeModal();
        toast(`Added ${created.name}`);
        await onCreated?.(created);
      } catch (error) {
        toast(error.detail || error.message, { error: true });
      }
    },
  });
}
