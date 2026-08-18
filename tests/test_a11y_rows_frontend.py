"""Keyboard-operable list/selection rows (UX panel review 2026-08-17, #1 /
A11Y-01). Every selectable or navigable row was a non-focusable
`<div data-action=…>` on a delegated click handler, so keyboard and
screen-reader users could not reach the detail pane at all. Each row is now a
`role="button" tabindex="0"` control activated by Enter/Space through the
SAME dispatch as click (the shared setRowKeys slot in ui.js).

No JS runtime here (see test_settings_frontend), so the behavior is pinned
against the shipped source.

WCAG 2.1: SC 2.1.1 (Keyboard, A), SC 4.1.2 (Name, Role, Value).
"""

from jshq import paths

FRONTEND = paths.FRONTEND_DIR

# Views whose list rows become keyboard-operable, and the render() call that
# installs the shared keydown slot.
ROW_VIEWS = (
    "js/views/jobs.js",
    "js/views/companies.js",
    "js/views/contacts.js",
    "js/views/applications.js",
    "js/views/today.js",
    "js/views/calendar.js",
)


def _read(rel):
    return (FRONTEND / rel).read_text(encoding="utf-8")


# ---- the shared activation slot -------------------------------------------

def test_ui_exports_setrowkeys_slot():
    ui = _read("js/lib/ui.js")
    assert "export function setRowKeys(container, dispatch)" in ui
    # replace-on-render slot (mirrors setFocusOut) so a remount never stacks
    # listeners
    assert 'container.removeEventListener("keydown", rowKeyHandler)' in ui
    assert 'container.addEventListener("keydown", rowKeyHandler)' in ui


def test_setrowkeys_only_activates_rows_on_enter_or_space():
    ui = _read("js/lib/ui.js")
    # Enter + Space, and Space's page-scroll default is prevented
    assert 'if (e.key !== "Enter" && e.key !== " ") return;' in ui
    assert "e.preventDefault();" in ui
    # fires ONLY when focus is on a row itself — the guard skips inner native
    # controls (a reminder row's snooze/Done buttons activate natively) so
    # click and key stay one code path
    assert "[role='button'][data-action]" in ui


# ---- every list view registers the slot -----------------------------------

def test_every_row_view_imports_and_registers_setrowkeys():
    for rel in ROW_VIEWS:
        src = _read(rel)
        assert "setRowKeys" in src, f"{rel} does not import setRowKeys"
        assert "setRowKeys(container, onClick)" in src, rel


# ---- rows are role=button, focusable --------------------------------------

def test_selectable_and_nav_rows_are_role_button_tabbable():
    # jobs / companies / contacts / applications selection rows + companies
    # topJobRow + today jobRow/upcomingRow + calendar day cells
    for rel in ROW_VIEWS:
        src = _read(rel)
        assert 'role="button" tabindex="0"' in src, rel


def test_jobs_select_row_is_a_button():
    jobs = _read("js/views/jobs.js")
    assert 'data-action="select" data-id="${job.id}" role="button" tabindex="0"' in jobs


def test_companies_select_and_topjob_rows_are_buttons():
    companies = _read("js/views/companies.js")
    assert 'data-action="select" data-id="${company.id}" role="button" tabindex="0"' in companies
    assert 'data-action="open-job" data-id="${job.id}" role="button" tabindex="0"' in companies


# ---- reminder row: the role goes on .reminder-main, not the wrapper -------

def test_reminder_row_puts_the_button_on_reminder_main_not_the_wrapper():
    today = _read("js/views/today.js")
    # ARIA forbids focusable descendants inside role="button"; the edit-reminder
    # row wraps real snooze/Done <button>s, so the role lives on the inner
    # non-interactive .reminder-main instead.
    assert '<div class="reminder-main" data-action="edit-reminder"' in today
    assert 'role="button" tabindex="0" aria-label="Edit reminder:' in today
    # the wrapper is a plain container (the action is no longer on it)
    assert 'class="reminder-row" data-action="edit-reminder"' not in today
    # and the inner action buttons are untouched
    assert 'data-action="snooze"' in today
    assert 'data-action="done"' in today


# ---- calendar: blank cells stay unfocusable -------------------------------

def test_calendar_day_cells_are_buttons_but_blanks_are_not():
    calendar = _read("js/views/calendar.js")
    # blank filler cells carry no data-action and must not become buttons
    assert '<div class="cal-cell cal-cell-blank"></div>' in calendar
    # real day cells are named buttons (a bare day number is an unclear control)
    assert 'data-action="select-day" data-date="${dateStr}"' in calendar
    assert 'role="button" tabindex="0" aria-label="${esc(label)}"' in calendar


# ---- focus ring -----------------------------------------------------------

def test_rows_have_a_focus_visible_ring():
    css = _read("css/app.css")
    assert ".company-row:focus-visible," in css
    assert ".reminder-row:focus-visible," in css
    assert ".reminder-main:focus-visible," in css
    assert ".cal-cell:focus-visible {" in css
