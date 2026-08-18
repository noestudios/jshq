/* Contacts view: list pane + detail pane with edit-in-place and add/delete. */

import { api } from "../api.js";
import {
  activityTimelineHtml,
  localToday,
  openActivityModal,
  openReminderModal,
} from "../lib/reminderModal.js";
import { openComposeModal } from "../lib/composeModal.js";
import { companyOptionsHtml, contactSources, openContactModal } from "../lib/contactModal.js";
import { dateFieldHtml } from "../lib/datepicker.js";
import { companyLogoHtml } from "../lib/logo.js";
import {
  confirmModal,
  emptyState,
  esc,
  escUrl,
  fmtFullDate,
  getDetailScroll,
  getListScroll,
  HQ_MARK,
  renderLoadError,
  renderLoading,
  searchBoxHtml,
  setDetailHash,
  revealSelected,
  setDetailScroll,
  setListScroll,
  pluralize,
  setStats,
  setFocusOut,
  setRowKeys,
  toast,
} from "../lib/ui.js";

const state = {
  contacts: [],
  companies: [],
  sources: [], // contact_sources setting, via contactSources() (fetch-once)
  selectedId: null,
  activityCache: new Map(),
  filters: { q: "" },
  mobileDetail: false,
  listScroll: 0,
  detailScroll: 0,
};

let root = null;

/* Mirrors ContactIn on the backend. */
function payload(contact, overrides = {}) {
  const fields = [
    "name", "company_id", "role", "linkedin_url", "email", "source",
    "relationship_notes", "last_contact_date",
  ];
  const body = {};
  for (const field of fields) body[field] = contact[field] ?? null;
  return { ...body, ...overrides };
}

async function load() {
  [state.contacts, state.companies, state.sources] = await Promise.all([
    api.listContacts(),
    api.listCompanies(),
    contactSources(),
  ]);
}

function selected() {
  return state.contacts.find((c) => c.id === state.selectedId) || null;
}

function filtered() {
  const needle = state.filters.q.trim().toLowerCase();
  if (!needle) return state.contacts;
  return state.contacts.filter((c) => {
    const haystack = `${c.name} ${c.role || ""} ${c.company_name || ""} ${c.relationship_notes || ""}`.toLowerCase();
    return haystack.includes(needle);
  });
}

function listRow(contact) {
  const isSelected = contact.id === state.selectedId;
  return `
    <div class="company-row${isSelected ? " selected" : ""}" data-action="select" data-id="${contact.id}" role="button" tabindex="0">
      <div class="co-row-flex">
        ${companyLogoHtml({ name: contact.company_name || contact.name, logo: contact.company_logo }, { size: "sm" })}
        <div class="co-row-rest">
          <div class="company-row-head">
            <span class="company-name">${esc(contact.name)}</span>
            <span class="contact-employer">${esc(contact.company_name || "")}</span>
          </div>
          <div class="company-meta">
            <span>${esc(contact.role || "no role")}</span>
            ${contact.source ? `<span class="source-tag">${esc(contact.source)}</span>` : ""}
          </div>
        </div>
      </div>
    </div>`;
}

function detailPane(contact) {
  if (!contact) {
    return `
      <div class="detail-empty">
        <div class="detail-empty-mark">${HQ_MARK}</div>
        <p>Select a contact to see their details and relationship notes — or add a new one.</p>
      </div>`;
  }

  return `
    <div class="detail-content" data-id="${contact.id}">
      <div class="detail-head">
        <button class="detail-back" data-action="close-detail" title="Back to list">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="15 18 9 12 15 6"/></svg>
          <span>Back</span>
        </button>
        <div class="detail-head-id">
          ${contact.company_id
            ? `<a class="co-logo-link" href="#/companies/${contact.company_id}" aria-label="View ${esc(contact.company_name)} in Companies">${companyLogoHtml({ name: contact.company_name, logo: contact.company_logo }, { size: "lg" })}</a>`
            : companyLogoHtml({ name: contact.company_name || contact.name, logo: contact.company_logo }, { size: "lg" })}
          <div class="detail-head-id-main">
            <div class="detail-head-row">
              <div class="detail-eyebrow">${contact.company_id ? `<a class="detail-eyebrow-link" href="#/companies/${contact.company_id}">${esc(contact.company_name)}</a>` : "no company"} · ${esc(contact.role || "no role")}</div>
            </div>
            <h2 class="detail-title">
              <input data-field="name" value="${esc(contact.name)}" aria-label="Contact name" />
            </h2>
          </div>
        </div>
        <div class="detail-subhead">
          ${contact.linkedin_url ? `<a href="${escUrl(contact.linkedin_url)}" target="_blank" rel="noopener">LinkedIn ↗</a>` : ""}
          ${contact.email ? `<a href="${escUrl(`mailto:${contact.email}`)}">${esc(contact.email)}</a>` : ""}
          <button class="btn btn-ghost" data-action="log-activity">Log activity</button>
          <button class="btn btn-ghost" data-action="schedule-followup">Schedule follow-up</button>
          <button class="btn btn-ghost" data-action="compose">Compose</button>
          <button class="btn btn-ghost btn-danger detail-delete" data-action="delete">Delete</button>
        </div>
      </div>

      <div class="control-row">
        <div class="field">
          <span class="field-label">Company</span>
          <select data-field="company_id" aria-label="Company">
            <option value="">—</option>
            ${companyOptionsHtml(state.companies, contact.company_id)}
          </select>
        </div>
        <div class="field">
          <span class="field-label">Source</span>
          <select data-field="source" aria-label="Source">
            <option value="">—</option>
            ${[...new Set([...(state.sources || []), ...(contact.source ? [contact.source] : [])])]
              .map((s) => `<option value="${esc(s)}"${s === contact.source ? " selected" : ""}>${esc(s)}</option>`)
              .join("")}
          </select>
        </div>
        <div class="field">
          <span class="field-label">Role</span>
          <input data-field="role" aria-label="Role" value="${esc(contact.role || "")}" />
        </div>
        <div class="field">
          <span class="field-label">Email</span>
          <input data-field="email" aria-label="Email" value="${esc(contact.email || "")}" />
        </div>
        <div class="field">
          <span class="field-label">LinkedIn URL</span>
          <input data-field="linkedin_url" aria-label="LinkedIn URL" value="${esc(contact.linkedin_url || "")}" />
        </div>
        <div class="field">
          <span class="field-label">Last contact</span>
          ${dateFieldHtml(contact.last_contact_date, { field: "last_contact_date", ariaLabel: "Last contact", title: fmtFullDate(contact.last_contact_date) })}
        </div>
      </div>

      <div class="section">
        <div class="section-head">
          <h2 class="section-title">Relationship notes</h2>
        </div>
        <textarea class="notes-area" data-field="relationship_notes" placeholder="How you met, what you talked about, what's next…">${esc(contact.relationship_notes || "")}</textarea>
      </div>

      <div class="section">
        <div class="section-head">
          <h2 class="section-title">Activity</h2>
        </div>
        ${activityTimelineHtml(state.activityCache.get(contact.id) || [])}
      </div>
    </div>`;
}

async function loadActivities(id) {
  if (state.activityCache.has(id)) return;
  try {
    state.activityCache.set(
      id,
      await api.listActivities({ entity_type: "contact", entity_id: id })
    );
  } catch {
    return; // timeline just stays empty; the rest of the pane works
  }
  if (state.selectedId === id) paint();
}

function template() {
  const rows = filtered();
  return `
    <div class="filters">
      ${searchBoxHtml("Search contacts…", state.filters.q)}
      <div class="actions-right">
        <button class="btn btn-accent btn-collapse" data-action="add" aria-label="Add contact"><span aria-hidden="true">+</span><span class="btn-label"> Add contact</span></button>
      </div>
    </div>
    <div class="layout contacts-layout">
      <div class="list-pane${state.mobileDetail ? " mobile-hide" : ""}">
        ${
          rows.length
            ? rows.map(listRow).join("")
            : emptyState(
                state.contacts.length
                  ? "No contacts match the current search."
                  : "No contacts yet — add one from a company's page.",
                { pad: true }
              )
        }
      </div>
      <div class="detail-pane${state.mobileDetail ? " mobile-show" : ""}">
        ${detailPane(selected())}
      </div>
    </div>`;
}

function renderStats() {
  setStats([
    { value: state.contacts.length, label: pluralize(state.contacts.length, "Contact", "Contacts") },
    { value: state.companies.length, label: pluralize(state.companies.length, "Company", "Companies") },
  ]);
}

function paint(opts = {}) {
  const top = getListScroll(root);
  if (top !== null) state.listScroll = top;
  const dtop = getDetailScroll(root);
  if (dtop !== null) state.detailScroll = dtop;
  root.innerHTML = template();
  setListScroll(root, state.listScroll);
  // Selecting a different item opens its detail at the top; every other repaint
  // (in-detail edits, saves) keeps the reader's place in the detail pane.
  if (opts.detailToTop) state.detailScroll = 0;
  setDetailScroll(root, state.detailScroll);
  renderStats();
}

function repaintList() {
  const pane = root.querySelector(".list-pane");
  if (!pane) return;
  const rows = filtered();
  pane.innerHTML = rows.length
    ? rows.map(listRow).join("")
    : emptyState(
        state.contacts.length
          ? "No contacts match the current search."
          : "No contacts yet — add one from a company's page.",
        { pad: true }
      );
}

async function reload({ keepSelection = true } = {}) {
  const keep = keepSelection ? state.selectedId : null;
  await load();
  state.selectedId = state.contacts.some((c) => c.id === keep) ? keep : null;
  paint();
}

let saveTimer = null;

async function save(contact, overrides, { quiet = false } = {}) {
  try {
    const updated = await api.updateContact(contact.id, payload(contact, overrides));
    if (quiet) {
      // mid-typing autosave: sync state without repainting (a repaint would steal focus)
      const index = state.contacts.findIndex((c) => c.id === contact.id);
      if (index !== -1) state.contacts[index] = updated;
    } else {
      await reload();
    }
  } catch (error) {
    if (quiet) return; // the focusout save retries and surfaces the error
    toast(error.detail || error.message, { error: true });
    paint();
  }
}

function fieldValue(field, element) {
  const raw = element.value.trim();
  if (field === "company_id") return raw ? Number(raw) : null;
  if (field === "name") return raw;
  return raw || null;
}

async function deleteSelected() {
  const contact = selected();
  if (!contact) return;
  const ok = await confirmModal({
    title: `Delete ${contact.name}?`,
    message: "This permanently removes the contact and their activity notes.",
  });
  if (!ok) return;
  try {
    await api.deleteContact(contact.id);
    state.selectedId = null;
    state.mobileDetail = false;
    setDetailHash("contacts", null); // Back must not return to the deleted id
    await reload();
    toast(`Deleted ${contact.name}`);
  } catch (error) {
    toast(error.detail || error.message, { error: true });
  }
}

function onClick(event) {
  const target = event.target.closest("[data-action]");
  if (!target || !root.contains(target)) return;
  switch (target.dataset.action) {
    case "select":
      state.selectedId = Number(target.dataset.id);
      state.mobileDetail = true;
      paint({ detailToTop: true });
      loadActivities(state.selectedId);
      setDetailHash("contacts", state.selectedId);
      break;
    case "search-clear": {
      state.filters.q = "";
      const input = root.querySelector(".search-box");
      input.value = "";
      target.classList.add("hide");
      repaintList();
      input.focus();
      break;
    }
    case "close-detail":
      // Our own history entry → back() pops it (popstate → hashchange → render);
      // cold deep-link entry → rewrite the hash in place and close locally.
      if (history.state?.hqDetail) {
        history.back();
      } else {
        state.mobileDetail = false;
        setDetailHash("contacts", null);
        paint();
      }
      break;
    case "add":
      openContactModal({
        companies: state.companies,
        onCreated: async (created) => {
          state.selectedId = created.id;
          await reload();
          setDetailHash("contacts", created.id);
        },
      });
      break;
    case "delete":
      deleteSelected();
      break;
    case "log-activity": {
      const contact = selected();
      if (!contact) break;
      openActivityModal({
        entity_type: "contact",
        entity_id: contact.id,
        entity_label: contact.name,
        onSaved: () => {
          state.activityCache.delete(contact.id);
          loadActivities(contact.id);
        },
      });
      break;
    }
    case "compose": {
      const contact = selected();
      if (!contact) break;
      openComposeModal({
        entity_type: "contact",
        entity_id: contact.id,
        entity_label: contact.name,
        onLogged: () => {
          state.activityCache.delete(contact.id);
          loadActivities(contact.id);
        },
      });
      break;
    }
    case "schedule-followup": {
      const contact = selected();
      if (!contact) break;
      openReminderModal({
        prefill: {
          title: `Follow up with ${contact.name}`,
          type: "followup_contact",
          due_date: localToday(7),
          entity_type: "contact",
          entity_id: contact.id,
          entity_label: contact.name,
        },
      });
      break;
    }
  }
}

function onChange(event) {
  const element = event.target;
  const field = element.dataset.field;
  const contact = selected();
  // selects commit on change; date fields too (the picker writes the value
  // and dispatches a synthetic change — mirrors applications.js, so the
  // debounce/focusout paths below can skip picker fields entirely)
  if (!field || !contact || (element.tagName !== "SELECT" && !("datepicker" in element.dataset))) return;
  save(contact, { [field]: fieldValue(field, element) });
}

function onFocusOut(event) {
  const element = event.target;
  const field = element.dataset.field;
  const contact = selected();
  // datepicker fields are change-committed above — a focusout save here
  // would fire mid-interaction when the popup takes focus (review catch)
  if (!field || !contact || element.tagName === "SELECT" || "datepicker" in element.dataset) return;
  clearTimeout(saveTimer);
  const value = fieldValue(field, element);
  if (field === "name" && !value) {
    toast("Name can't be empty", { error: true });
    paint();
    return;
  }
  if (value === (contact[field] ?? null)) return;
  save(contact, { [field]: value });
}

function onInput(event) {
  const element = event.target;
  if (element.dataset.action === "search") {
    state.filters.q = element.value;
    root.querySelector(".search-clear")?.classList.toggle("hide", !state.filters.q);
    repaintList();
    return;
  }
  const field = element.dataset.field;
  if (!field || element.tagName === "SELECT" || "datepicker" in element.dataset) return;
  const contactId = selected()?.id;
  if (!contactId) return;
  clearTimeout(saveTimer);
  // iOS Safari often never blurs an input (tapping outside isn't a blur), so a
  // focusout-only save loses edits; autosave while typing as the safety net.
  saveTimer = setTimeout(() => {
    const contact = state.contacts.find((c) => c.id === contactId);
    if (!contact) return;
    const value = fieldValue(field, element);
    if (field === "name" && !value) return; // focusout owns the empty-name error
    if (value === (contact[field] ?? null)) return;
    save(contact, { [field]: value }, { quiet: true });
  }, 700);
}

export async function render(container, preselectId = null) {
  root = container;
  renderLoading(container);
  container.onclick = onClick;
  container.onchange = onChange;
  container.oninput = onInput;
  setFocusOut(container, onFocusOut);
  setRowKeys(container, onClick);
  try {
    await load();
  } catch (error) {
    renderLoadError(container, error, () => render(container, preselectId));
    setStats([]);
    return;
  }
  if (preselectId && state.contacts.some((c) => c.id === preselectId)) {
    state.selectedId = preselectId;
    state.mobileDetail = true;
  } else if (!preselectId) {
    // back/forward to the bare list: keep the desktop pane's selection, but
    // the phone must land on the list
    state.mobileDetail = false;
  }
  paint();
  // Route-driven selection only: an in-list click never yanks the pane.
  if (preselectId && state.selectedId === preselectId) revealSelected(root);
  if (state.selectedId) loadActivities(state.selectedId);
}
