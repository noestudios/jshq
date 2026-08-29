/* Shared Phase 5 UI: reminder create/edit modal, activity-log modal, and the
   activity timeline used by the jobs/contacts detail panes. */

import { api } from "../api.js";
import { closeModal, confirmModal, emptyState, esc, fmtFullDate, fmtStamp, localToday, openModal, toast } from "./ui.js";
import { dateFieldHtml } from "./datepicker.js";
import { timeFieldHtml } from "./timepicker.js";

/* localToday moved to ui.js in P3 (datepicker.js needs it without a module
   cycle); re-exported here so existing importers keep working unchanged. */
export { localToday };

export const REMINDER_TYPES = [
  { value: "custom", label: "custom" },
  { value: "followup_application", label: "follow up — application" },
  { value: "followup_contact", label: "follow up — contact" },
  { value: "thank_you", label: "thank-you" },
  { value: "interview", label: "interview" },
  { value: "meeting", label: "meeting" },
  { value: "linkedin_post", label: "LinkedIn post" },
];

const ACTIVITY_TYPES = ["meeting", "interview", "call", "note"];

/** A reminder/event's full local due date, with the HH:MM wall-clock time when
    set — for a hover that turns a coarse date badge into the exact moment.
    date is YYYY-MM-DD (naive local), time is "HH:MM" or falsy. Returns "" when
    the date is missing/unparseable. e.g. "Tuesday, June 10, 2026, 2:30 PM". */
export function fmtReminderDue(date, time) {
  const full = fmtFullDate(date);
  if (!full) return "";
  if (!time) return full;
  const dt = new Date(`${date}T${time}`);
  return isNaN(dt)
    ? `${full}, ${time}`
    : `${full}, ${dt.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}`;
}

/** Create (no reminder) or edit (reminder given). prefill seeds the form on
    create: {title, type, due_date, due_time, entity_type, entity_id,
    entity_label}. onSaved(reminderOrNull) fires after save or delete. */
export function openReminderModal({ reminder = null, prefill = {}, onSaved } = {}) {
  const isEdit = Boolean(reminder?.id);
  const data = { type: "custom", due_date: localToday(), ...prefill, ...(reminder || {}) };
  openModal({
    title: isEdit ? "Edit reminder" : "Add reminder",
    body: `
      <p class="form-req-note"><span class="req-mark" aria-hidden="true">*</span> required</p>
      <div class="form-field">
        <label>Title <span class="req-mark" aria-hidden="true">*</span></label>
        <input name="title" required value="${esc(data.title || "")}" />
      </div>
      <div class="form-field">
        <label>Type</label>
        <select name="type">
          ${REMINDER_TYPES.map(
            (t) => `<option value="${t.value}"${t.value === data.type ? " selected" : ""}>${t.label}</option>`
          ).join("")}
        </select>
      </div>
      <div class="form-field-row">
        <div class="form-field">
          <label>Due date <span class="req-mark" aria-hidden="true">*</span></label>
          ${dateFieldHtml(data.due_date || "", { name: "due_date", required: true })}
        </div>
        <div class="form-field">
          <label>Time</label>
          ${timeFieldHtml(data.due_time || "", { name: "due_time" })}
        </div>
      </div>
      ${
        data.entity_type
          ? `<div class="form-field"><label>Linked to</label><div class="linked-entity">${esc(data.entity_label || `${data.entity_type} #${data.entity_id}`)}</div></div>`
          : ""
      }
      <div class="form-field">
        <label>Notes</label>
        <textarea name="notes" rows="2">${esc(data.notes || "")}</textarea>
      </div>`,
    footer: `
      ${isEdit ? `<button type="button" class="btn btn-ghost btn-danger" data-action="reminder-delete" style="margin-right: auto;">Delete</button>` : ""}
      <button type="button" class="btn" data-action="modal-close">Cancel</button>
      <button type="submit" class="btn btn-accent">${isEdit ? "Save" : "Add reminder"}</button>`,
    onSubmit: async (form) => {
      const body = {
        title: form.title.value.trim(),
        type: form.type.value,
        due_date: form.due_date.value,
        due_time: form.due_time.value || null,
        notes: form.notes.value.trim() || null,
        entity_type: data.entity_type || null,
        entity_id: data.entity_type ? data.entity_id : null,
      };
      try {
        const saved = isEdit
          ? await api.updateReminder(reminder.id, body)
          : await api.createReminder(body);
        closeModal();
        toast(isEdit ? "Reminder updated" : "Reminder added");
        if (onSaved) onSaved(saved);
      } catch (error) {
        toast(error.detail || error.message, { error: true });
      }
    },
  }).addEventListener("click", async (event) => {
    if (!event.target.closest("[data-action='reminder-delete']")) return;
    closeModal();
    const ok = await confirmModal({
      title: "Delete reminder?",
      message: `“${reminder.title}” is removed from the app and from the calendar feed.`,
    });
    if (!ok) return;
    try {
      await api.deleteReminder(reminder.id);
      toast("Reminder deleted");
      if (onSaved) onSaved(null);
    } catch (error) {
      toast(error.detail || error.message, { error: true });
    }
  });
}

/** Log a meeting/interview/call/note against an entity. */
export function openActivityModal({ entity_type, entity_id, entity_label, onSaved } = {}) {
  openModal({
    title: "Log activity",
    body: `
      <div class="form-field">
        <label>Type</label>
        <select name="type">
          ${ACTIVITY_TYPES.map((t) => `<option value="${t}">${t}</option>`).join("")}
        </select>
      </div>
      <div class="form-field">
        <label>Date</label>
        ${dateFieldHtml(localToday(), { name: "date", required: true })}
      </div>
      ${entity_label ? `<div class="form-field"><label>Linked to</label><div class="linked-entity">${esc(entity_label)}</div></div>` : ""}
      <div class="form-field">
        <label>What happened</label>
        <textarea name="content" rows="3" placeholder="Notes feed future AI context — a sentence is plenty."></textarea>
      </div>`,
    footer: `
      <button type="button" class="btn" data-action="modal-close">Cancel</button>
      <button type="submit" class="btn btn-accent">Log it</button>`,
    onSubmit: async (form) => {
      try {
        const created = await api.createActivity({
          entity_type,
          entity_id,
          type: form.type.value,
          date: form.date.value,
          content: form.content.value.trim() || null,
        });
        closeModal();
        toast("Activity logged");
        if (onSaved) onSaved(created);
      } catch (error) {
        toast(error.detail || error.message, { error: true });
      }
    },
  });
}

/* Side-effect rows (dismissal/applied/unapplied/compose/status/next_step)
   store JSON content; render it readably. A type missing from this list falls
   through to the raw content, which for a JSON payload means showing the JSON
   — so any new side-effect type has to be added here as well as in main.py. */
function activityText(activity) {
  const sideEffect = ["dismissal", "applied", "unapplied", "compose", "status", "next_step"];
  if (sideEffect.includes(activity.type)) {
    let payload = {};
    try {
      payload = JSON.parse(activity.content || "{}");
    } catch {
      /* fall through to plain text */
    }
    if (activity.type === "dismissal") {
      return `dismissed${payload.reason ? `: ${payload.reason}` : ""}${payload.note ? ` — ${payload.note}` : ""}`;
    }
    if (activity.type === "compose") {
      const intent = (payload.intent || "draft").replaceAll("_", " ");
      const draft = (payload.draft || "").replace(/\s+/g, " ");
      return `drafted ${intent}${draft ? ` — “${draft.slice(0, 120)}${draft.length > 120 ? "…" : ""}”` : ""}`;
    }
    if (activity.type === "unapplied") {
      // Deliberately not "withdrawn": that is a real application status, and
      // this row means the apply was undone, not that the user withdrew.
      return "application reverted";
    }
    if (activity.type === "next_step") {
      // {action: done|dismissed, title, due_date, auto?} — auto marks the
      // dismissals that ride an application closing out.
      if (!payload.action) return activity.content || "";
      return `next step ${payload.action}${payload.auto ? " (auto)" : ""}: ${payload.title || "?"}${payload.due_date ? ` — due ${payload.due_date}` : ""}`;
    }
    if (activity.type === "status") {
      // from is null for legacy NULL-status rows; a payload without `to`
      // (malformed) falls back to the raw content like any unknown row.
      if (!payload.to) return activity.content || "";
      return payload.from ? `${payload.from} → ${payload.to}` : `→ ${payload.to}`;
    }
    return "application submitted";
  }
  return activity.content || "";
}

export function activityTimelineHtml(activities) {
  if (!activities.length) {
    return emptyState("Nothing logged yet.");
  }
  return activities
    .map((a) => {
      // The visible a.date is the user-picked day (date only); created_at is when
      // the row was actually logged, the only sub-day timestamp we have — so the
      // hover is labelled "Logged …" to keep the two distinct.
      const logged = fmtStamp(a.created_at);
      return `
      <div class="activity-row">
        <span class="activity-date"${logged ? ` title="Logged ${esc(logged)}"` : ""}>${esc(a.date || "")}</span>
        <span class="activity-type activity-type-${esc(a.type)}">${esc(a.type)}</span>
        <span class="activity-text">${esc(activityText(a))}</span>
      </div>`;
    })
    .join("");
}
