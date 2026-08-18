"""The compose modal must confirm before an accidental close drops an edited
draft. No JS runtime here (see test_settings_frontend), so the behavior is
pinned against the shipped source: a beforeClose guard on the backdrop/Escape
vectors, an inline discard confirm (a confirm-modal would replace this modal),
and the explicit Cancel button left unguarded.
"""

from jshq import paths

FRONTEND = paths.FRONTEND_DIR


def _read(rel):
    return (FRONTEND / rel).read_text(encoding="utf-8")


def test_openmodal_guards_only_the_accidental_close_vectors():
    ui = _read("js/lib/ui.js")
    # a beforeClose guard, consulted by both accidental vectors
    assert "activeBeforeClose" in ui
    assert "if (activeBeforeClose && activeBeforeClose() === false) return;" in ui
    # the explicit Cancel button is a deliberate dismissal — never guarded
    assert "closeModal(); // explicit Cancel" in ui
    # cleared on close so it never leaks to the next modal
    assert "activeBeforeClose = null;" in ui


def test_compose_modal_confirms_discard_of_a_dirty_draft():
    js = _read("js/lib/composeModal.js")
    assert "beforeClose:" in js
    assert "function draftIsDirty()" in js
    assert "showDiscardConfirm()" in js
    # inline confirm in the footer, not confirmModal (which would replace this modal)
    assert "Discard your edited draft?" in js
    assert 'data-role="discard-draft"' in js
    assert 'data-role="keep-editing"' in js
