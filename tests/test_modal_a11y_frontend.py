"""The shared modal helper must be accessible (A11Y panel review, 2026-08-17):
a focus trap so Tab can't escape behind the overlay, focus restoration to the
opener on close, and an accessible name on the dialog. No JS runtime here (see
test_settings_frontend), so the behavior is pinned against the shipped source.

Covers WCAG 2.2 SC 2.4.3 (Focus Order) and SC 4.1.2 (Name, Role, Value) for
every modal, since Add/Edit reminder, Compose, the contact editor, and the
wizard Add-company modal all route through openModal/closeModal.
"""

from jshq import paths

FRONTEND = paths.FRONTEND_DIR


def _read(rel):
    return (FRONTEND / rel).read_text(encoding="utf-8")


def test_dialog_has_an_accessible_name():
    ui = _read("js/lib/ui.js")
    # a unique id per dialog, wired to the title node via aria-labelledby
    assert "const titleId = `modal-title-${++modalSeq}`;" in ui
    assert 'aria-labelledby="${titleId}"' in ui
    assert '<div class="modal-title" id="${titleId}">' in ui


def test_focus_is_restored_to_the_opener_on_close():
    ui = _read("js/lib/ui.js")
    # opener captured on open (skipping <body> = nothing was focused)
    assert "restoreFocusTo = opener && opener !== document.body ? opener : null;" in ui
    # restored on close, guarded against a detached opener
    assert "function restoreFocus()" in ui
    assert "if (el && document.contains(el) && typeof el.focus === \"function\") el.focus();" in ui
    assert "if (!keepFocus) restoreFocus();" in ui


def test_replacing_modal_keeps_the_original_opener():
    ui = _read("js/lib/ui.js")
    # the instant swap a replacing open performs must not restore focus, and
    # must not recapture the opener (which would be the outgoing field)
    assert "closeModal({ instant: true, keepFocus: true });" in ui
    assert "const replacing = !!activeModal;" in ui
    assert "if (!replacing) {" in ui


def test_modal_field_labels_are_associated_with_their_controls():
    """Scoped-out sibling of A11Y-04 (found during #11): every modal form is
    authored as <div class="form-field"><label>Name</label><input …></div>, where
    the label is a SIBLING of the control, so it names nothing to a screen reader
    and clicking it doesn't focus the field. openModal wires the for/id link once
    for every modal (add-company/add-job/edit-details/contact/compose/reminder all
    route through it). WCAG 2.2 SC 1.3.1 / 3.3.2 / 4.1.2."""
    ui = _read("js/lib/ui.js")
    # the association pass runs over each .form-field in the built form
    assert 'form.querySelectorAll(".form-field").forEach((field) => {' in ui
    # names the first labelable control, minting an id when it lacks one
    assert 'field.querySelector("input, select, textarea")' in ui
    assert "control.id = `mf-${++fieldSeq}`;" in ui
    assert 'label.setAttribute("for", control.id);' in ui
    # never clobbers a label the author already wired, and no-ops a control-less
    # field (the read-only "Linked to" rows)
    assert 'if (!label || label.hasAttribute("for")) return;' in ui
    assert "if (!control) return;" in ui


def test_tab_is_trapped_inside_the_open_dialog():
    ui = _read("js/lib/ui.js")
    assert "function trapFocus(event)" in ui
    # registered on open, torn down on close
    assert 'document.addEventListener("keydown", trapFocus);' in ui
    assert 'document.removeEventListener("keydown", trapFocus);' in ui
    # scoped to focus actually inside the overlay so it never fights a picker
    # popover's own capture-phase Tab cycle
    assert "if (!activeModal.contains(active)) return;" in ui
    # wraps at both ends
    assert "if (event.shiftKey && active === first) {" in ui
    assert "} else if (!event.shiftKey && active === last) {" in ui
