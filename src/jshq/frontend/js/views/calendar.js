/* Calendar view (Phase 5): month grid of reminders + logged
   meetings/interviews. Click a day to work its items in the detail pane.
   ICS: the feed URL is subscribable (macOS Calendar ▸ File ▸ New Calendar
   Subscription); downloads are one-shot imports. */

import { api } from "../api.js";
import { fmtReminderDue, localToday, openReminderModal } from "../lib/reminderModal.js";
import { emptyState, esc, HQ_MARK, renderLoadError, renderLoading, setFocusOut, setRowKeys, setStats, toast } from "../lib/ui.js";

const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MAX_CHIPS = 3;

const state = {
  reminders: [],
  events: [], // logged meetings/interviews
  nextSteps: [], // applications' next-step rows (v10; pending + resolved history)
  year: null,
  month: null, // 0-based
  selectedDate: null, // YYYY-MM-DD
  mobileDetail: false,
};

let root = null;

async function load() {
  [state.reminders, state.events, state.nextSteps] = await Promise.all([
    api.listReminders(),
    api.listActivities({ types: "meeting,interview" }),
    api.listNextSteps(),
  ]);
}

function ymd(year, month, day) {
  return `${year}-${String(month + 1).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
}

function monthLabel() {
  return new Date(state.year, state.month, 1).toLocaleDateString(undefined, {
    month: "long",
    year: "numeric",
  });
}

function itemsOn(dateStr) {
  const reminders = state.reminders
    .filter((r) => r.due_date === dateStr)
    .map((r) => ({ kind: "reminder", time: r.due_time, reminder: r }));
  const events = state.events
    .filter((a) => a.date === dateStr)
    .map((a) => ({ kind: "event", time: null, event: a }));
  const nextSteps = state.nextSteps
    .filter((n) => n.due_date === dateStr)
    .map((n) => ({ kind: "nextstep", time: null, nextStep: n }));
  return [...reminders, ...events, ...nextSteps].sort((a, b) =>
    (a.time || "").localeCompare(b.time || "")
  );
}

function chip(item, today) {
  if (item.kind === "event") {
    return `<div class="cal-chip cal-chip-event" title="${esc(item.event.content || item.event.type)}">${esc(item.event.type)}</div>`;
  }
  if (item.kind === "nextstep") {
    // Reuse the reminder overdue/pending lightness ramp so overdue still reads;
    // the leading arrow (a non-colour cue) marks it as a next-step, not a
    // reminder — colour-blind-safe without a new hue. Resolved rows stay on
    // the grid with the done treatment (muted + line-through); done vs
    // dismissed is spelled out in the tooltip and on the detail row.
    const n = item.nextStep;
    const cls =
      n.status !== "pending" ? "cal-chip-done"
      : n.due_date < today ? "cal-chip-overdue"
      : "cal-chip-pending";
    const label = n.status === "pending" ? "Next step" : `Next step (${n.status})`;
    return `<div class="cal-chip cal-chip-nextstep ${cls}" title="${label} — ${esc(n.entity_label)}">→ ${esc(n.title)}</div>`;
  }
  const r = item.reminder;
  const cls = r.done ? "cal-chip-done" : r.due_date < today ? "cal-chip-overdue" : "cal-chip-pending";
  return `<div class="cal-chip ${cls}" title="${esc(r.title)}">${esc(r.title)}</div>`;
}

function dayCell(day, today) {
  if (day === null) return `<div class="cal-cell cal-cell-blank"></div>`;
  const dateStr = ymd(state.year, state.month, day);
  const items = itemsOn(dateStr);
  const classes = [
    "cal-cell",
    dateStr === today ? "cal-cell-today" : "",
    dateStr === state.selectedDate ? "cal-cell-selected" : "",
  ].join(" ");
  // Full readable date as the button's name — a bare day number ("15") is an
  // unclear control for a screen reader. Mirrors the detail-title label format.
  const label = new Date(`${dateStr}T00:00:00`).toLocaleDateString(undefined, {
    weekday: "long",
    month: "long",
    day: "numeric",
  });
  return `
    <div class="${classes}" data-action="select-day" data-date="${dateStr}"
         role="button" tabindex="0" aria-label="${esc(label)}">
      <div class="cal-daynum">${day}</div>
      ${items.slice(0, MAX_CHIPS).map((i) => chip(i, today)).join("")}
      ${items.length > MAX_CHIPS ? `<div class="cal-more">+${items.length - MAX_CHIPS} more</div>` : ""}
    </div>`;
}

function grid(today) {
  const first = new Date(state.year, state.month, 1);
  const daysInMonth = new Date(state.year, state.month + 1, 0).getDate();
  const cells = [
    ...Array(first.getDay()).fill(null),
    ...Array.from({ length: daysInMonth }, (_, i) => i + 1),
  ];
  return `
    <div class="cal-grid">
      ${WEEKDAYS.map((d) => `<div class="cal-weekday">${d}</div>`).join("")}
      ${cells.map((d) => dayCell(d, today)).join("")}
    </div>`;
}

/* `today` must be passed explicitly — the call site maps over items, and
   Array.map hands the callback (item, index, array), so a defaulted second
   parameter would silently receive an integer and every row would read as
   overdue. */
/* A logged event has a TYPE and a note — no title. Its content was rendering in
   .reminder-title, which was survivable while that class was unstyled body text
   but not once it became the row-title face (Fraunces 500): a three-line
   paragraph set in display type reads as a heading that ran on. The badge
   already carries the type, here and on the grid chip, so the content goes
   where it belongs, in the notes slot. */
function detailItem(item, today) {
  if (item.kind === "event") {
    const a = item.event;
    return `
      <div class="reminder-row">
        <div class="reminder-main">
          <span class="rem-badge rem-event">${esc(a.type)}</span>
          <p class="rem-notes">${esc(a.content || "(no notes)")}</p>
        </div>
      </div>`;
  }
  if (item.kind === "nextstep") {
    // First-class row (v10): the title deep-links to the application (where the
    // step is edited); pending rows carry Done/Dismiss right here. The inner
    // <a> has its own data-action so closest() stops on it and native hash nav
    // runs (the download-ics precedent); resolved rows show the status word —
    // done also strikes the title, dismissed only mutes, so the pair reads
    // without colour.
    const n = item.nextStep;
    const resolved = n.status !== "pending";
    const overdue = !resolved && today && n.due_date < today;
    const badge = resolved
      ? `<span class="rem-badge rem-resolved">${n.status}</span>`
      : `<span class="rem-badge ${overdue ? "rem-overdue" : "rem-nextstep"}">${overdue ? "overdue" : "next step"}</span>`;
    return `
      <div class="reminder-row reminder-nextstep${n.status === "done" ? " reminder-done" : ""}${n.status === "dismissed" ? " reminder-dismissed" : ""}">
        <div class="reminder-main">
          ${badge}
          <a class="reminder-title" href="#/applications/${n.application_id}" data-action="open-application">→ ${esc(n.title)}</a>
          <span class="rem-entity">${esc(n.entity_label)}</span>
        </div>
        ${
          resolved
            ? ""
            : `<div class="reminder-actions">
                <button class="btn btn-ghost" data-action="nextstep-dismiss" data-id="${n.id}">Dismiss</button>
                <button class="btn" data-action="nextstep-done" data-id="${n.id}">Done</button>
              </div>`
        }
      </div>`;
  }
  const r = item.reminder;
  // The grid marks overdue reminders; the detail pane had no state signal at
  // all — done-ness got a strikethrough, overdue got nothing. Same .rem-badge
  // Today and Applications already use for this.
  const overdue = !r.done && today && r.due_date < today;
  return `
    <div class="reminder-row${r.done ? " reminder-done" : ""}" data-action="edit-reminder" data-id="${r.id}">
      <div class="reminder-main">
        ${overdue ? `<span class="rem-badge rem-overdue">overdue</span>` : ""}
        ${r.due_time ? `<span class="reminder-time">${esc(r.due_time)}</span>` : ""}
        <span class="reminder-title" title="${esc(fmtReminderDue(r.due_date, r.due_time))}">${esc(r.title)}</span>
        ${r.entity_label ? `<span class="rem-entity">${esc(r.entity_label)}</span>` : ""}
        ${r.notes ? `<p class="rem-notes">${esc(r.notes)}</p>` : ""}
      </div>
      <div class="reminder-actions">
        <a class="btn btn-ghost" href="/api/reminders/${r.id}/ics" title="Download .ics" data-action="download-ics">⤓</a>
        <button class="btn${r.done ? " btn-ghost" : ""}" data-action="toggle-done" data-id="${r.id}">${r.done ? "Undo" : "Done"}</button>
      </div>
    </div>`;
}

function detailPane(today) {
  if (!state.selectedDate) {
    return `
      <div class="detail-empty">
        <div class="detail-empty-mark">${HQ_MARK}</div>
        <p>Select a day to see and add reminders.</p>
      </div>`;
  }
  const items = itemsOn(state.selectedDate);
  const label = new Date(`${state.selectedDate}T00:00:00`).toLocaleDateString(undefined, {
    weekday: "long",
    month: "long",
    day: "numeric",
  });
  return `
    <div class="detail-content">
      <div class="detail-head">
        <button class="detail-back" data-action="close-detail" title="Back to grid">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="15 18 9 12 15 6"/></svg>
          <span>Back</span>
        </button>
        <div class="detail-head-row">
          <div class="detail-eyebrow">${state.selectedDate === today ? "today" : ""}</div>
        </div>
        <h2 class="detail-title">${esc(label)}</h2>
      </div>
      <div class="section">
        <div class="section-head">
          <h2 class="section-title">Scheduled</h2>
          <button class="btn btn-accent" data-action="add-reminder">+ Add reminder</button>
        </div>
        ${items.length ? items.map((i) => detailItem(i, today)).join("") : emptyState("Nothing on this day.")}
      </div>
    </div>`;
}

function template() {
  const today = localToday();
  return `
    <div class="filters">
      <div class="filter-group cal-nav">
        <button class="btn" data-action="prev-month" title="Previous month">‹</button>
        <span class="cal-month-label">${esc(monthLabel())}</span>
        <button class="btn" data-action="next-month" title="Next month">›</button>
        <button class="btn btn-ghost" data-action="goto-today">Today</button>
      </div>
      <div class="actions-right">
        <a class="btn btn-collapse" href="/api/reminders/ics" title="All pending reminders — import into Google Calendar or any calendar app">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3v12"/><path d="m7 11 5 5 5-5"/><path d="M4 20h16"/></svg>
          <span class="btn-label">⤓ Download .ics</span>
        </a>
        <button class="btn btn-ghost btn-collapse" data-action="copy-feed" title="Copy the reminders feed URL — subscribe in a calendar app on this computer (Apple Calendar: File ▸ New Calendar Subscription). Served from this machine, so web calendars like Google can't reach it.">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>
          <span class="btn-label">Copy feed URL</span>
        </button>
        <button class="btn btn-accent btn-collapse" data-action="add-reminder" title="Add reminder">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>
          <span class="btn-label">+ Add reminder</span>
        </button>
      </div>
    </div>
    <div class="layout">
      <div class="list-pane cal-pane${state.mobileDetail ? " mobile-hide" : ""}">
        ${grid(today)}
      </div>
      <div class="detail-pane${state.mobileDetail ? " mobile-show" : ""}">
        ${detailPane(today)}
      </div>
    </div>`;
}

function renderStats() {
  const today = localToday();
  const monthPrefix = `${state.year}-${String(state.month + 1).padStart(2, "0")}-`;
  const pending = state.reminders.filter(
    (r) => !r.done && r.due_date.startsWith(monthPrefix)
  ).length;
  const overdue = state.reminders.filter((r) => !r.done && r.due_date < today).length;
  setStats([
    { value: pending, label: "Pending" },
    { value: overdue, label: "Overdue" },
  ]);
}

function paint() {
  root.innerHTML = template();
  renderStats();
}

function upsertReminder(saved, removedId = null) {
  if (saved) {
    const i = state.reminders.findIndex((r) => r.id === saved.id);
    if (i >= 0) state.reminders[i] = saved;
    else state.reminders.push(saved);
  } else if (removedId) {
    state.reminders = state.reminders.filter((r) => r.id !== removedId);
  }
  paint();
}

async function toggleDone(id) {
  const reminder = state.reminders.find((r) => r.id === id);
  if (!reminder) return;
  try {
    upsertReminder(await api.patchReminder(id, { done: !reminder.done }));
  } catch (error) {
    toast(error.detail || error.message, { error: true });
  }
}

async function resolveNextStep(id, status) {
  try {
    const saved = await api.patchNextStep(id, { status });
    state.nextSteps = state.nextSteps.map((n) => (n.id === saved.id ? saved : n));
    paint();
    toast(status === "done" ? "Done" : "Dismissed");
  } catch (error) {
    toast(error.detail || error.message, { error: true });
  }
}

function addReminder() {
  openReminderModal({
    prefill: { due_date: state.selectedDate || localToday() },
    onSaved: (saved) => upsertReminder(saved),
  });
}

async function copyFeedUrl() {
  const url = `${location.origin}/api/calendar.ics`;
  try {
    await navigator.clipboard.writeText(url);
    toast("Feed URL copied — add it as a calendar subscription in your calendar app");
  } catch {
    toast(url); // clipboard unavailable — at least show it
  }
}

function onClick(event) {
  const target = event.target.closest("[data-action]");
  if (!target || !root.contains(target)) return;
  switch (target.dataset.action) {
    case "prev-month":
    case "next-month": {
      const delta = target.dataset.action === "prev-month" ? -1 : 1;
      const d = new Date(state.year, state.month + delta, 1);
      state.year = d.getFullYear();
      state.month = d.getMonth();
      paint();
      break;
    }
    case "goto-today": {
      const now = new Date();
      state.year = now.getFullYear();
      state.month = now.getMonth();
      state.selectedDate = localToday();
      paint();
      break;
    }
    case "select-day":
      state.selectedDate = target.dataset.date;
      state.mobileDetail = true;
      paint();
      break;
    case "close-detail":
      state.mobileDetail = false;
      paint();
      break;
    case "add-reminder":
      addReminder();
      break;
    case "toggle-done":
      toggleDone(Number(target.dataset.id));
      break;
    case "nextstep-done":
      resolveNextStep(Number(target.dataset.id), "done");
      break;
    case "nextstep-dismiss":
      resolveNextStep(Number(target.dataset.id), "dismissed");
      break;
    case "open-application":
      // Same shape as download-ics: the anchor's own data-action stops
      // closest() here and the native href hash-navigates.
      break;
    case "download-ics":
      // The link carries its own data-action so closest() stops here instead of
      // walking up to the row's edit-reminder; the native href downloads the
      // .ics (no preventDefault below). Fixes the modal-pops-on-download bug.
      break;
    case "edit-reminder": {
      const reminder = state.reminders.find((r) => r.id === Number(target.dataset.id));
      if (reminder) {
        openReminderModal({
          reminder,
          onSaved: (saved) => upsertReminder(saved, reminder.id),
        });
      }
      break;
    }
    case "copy-feed":
      copyFeedUrl();
      break;
  }
}

export async function render(container, _id, params = {}) {
  root = container;
  renderLoading(container);
  container.onclick = onClick;
  container.onchange = null;
  container.oninput = null;
  setFocusOut(container, null);
  setRowKeys(container, onClick);
  // #/calendar?date=YYYY-MM-DD focuses that day (Today's "Coming up" rows link
  // this way). It wins on every mount — arriving from a specific event should
  // always land on that event, not on wherever the view was left.
  const focus = /^\d{4}-\d{2}-\d{2}$/.test(params.date || "") ? params.date : null;
  if (focus) {
    const [year, month] = focus.split("-").map(Number);
    state.year = year;
    state.month = month - 1;
    state.selectedDate = focus;
    state.mobileDetail = true; // mirrors select-day, so phones show the day
  } else if (state.year === null) {
    const now = new Date();
    state.year = now.getFullYear();
    state.month = now.getMonth();
    state.selectedDate = localToday();
  }
  try {
    await load();
  } catch (error) {
    renderLoadError(container, error, () => render(container));
    setStats([]);
    return;
  }
  paint();
}
