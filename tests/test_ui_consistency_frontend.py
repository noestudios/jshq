"""UI-consistency batch (2026-08-17 UX panel review): dead CSS removal (#24 /
UI-08), count-aware stat labels (#8 / UI-04), and a coherent required/optional
field convention (#22 / UI-06). Source-scan style (no JS runtime) — the
behavior is pinned against the shipped source, like test_settings_frontend.
"""

from jshq import paths

FRONTEND = paths.FRONTEND_DIR


def _read(rel):
    return (FRONTEND / rel).read_text(encoding="utf-8")


# ---- #24 UI-08: no dead .btn-primary --------------------------------------

def test_btn_primary_is_gone_everywhere_but_the_note_saying_so():
    css = _read("css/app.css")
    # The dead rule and its :hover are removed; the shared active idiom keeps
    # its real members. The only surviving mention is the comment documenting
    # that the primary button is .btn-accent.
    assert ".btn-primary {" not in css
    assert ".btn-primary:hover" not in css
    assert "there is no .btn-primary" in css  # the intentional signpost
    # cross-ref comments were repointed to the block's new first selector
    assert "(see .btn-primary)" not in css
    assert "(at .btn-primary)" not in css
    # no JS ever referenced it
    for rel in ("js/views/companies.js", "js/views/jobs.js", "js/lib/ui.js"):
        assert "btn-primary" not in _read(rel), rel


# ---- #8 UI-04: count-aware stat labels ------------------------------------

def test_ui_exports_pluralize_helper():
    ui = _read("js/lib/ui.js")
    assert "export function pluralize(n, singular, plural)" in ui
    assert "return n === 1 ? singular : plural;" in ui


def test_count_noun_stat_labels_pluralize():
    # each count-noun label is chosen by pluralize on its own value; adjective
    # labels (Active, New, Open, Overdue, Maybe) are left as static strings.
    assert 'pluralize(state.companies.length, "Company", "Companies")' in _read("js/views/companies.js")
    assert 'pluralize(active, "Active job", "Active jobs")' in _read("js/views/jobs.js")
    contacts = _read("js/views/contacts.js")
    assert 'pluralize(state.contacts.length, "Contact", "Contacts")' in contacts
    assert 'pluralize(state.companies.length, "Company", "Companies")' in contacts
    assert 'pluralize(due.length, "Step due", "Steps due")' in _read("js/views/applications.js")


# ---- #22 UI-06: required marking made real + explained ---------------------

import re

REQ_MARK = '<span class="req-mark" aria-hidden="true">*</span>'
REQ_NOTE = '<p class="form-req-note"><span class="req-mark" aria-hidden="true">*</span> required</p>'


def test_required_marker_and_legend_styles_exist():
    css = _read("css/app.css")
    assert ".req-mark {" in css
    assert ".form-req-note {" in css
    # the marker is not hue-coded (colour is reserved for state) — muted ink
    assert "color: var(--t-ink-mute)" in css


def test_no_legacy_literal_asterisk_required_labels_remain():
    # the old "Name *" literal convention is fully replaced by the aria-hidden
    # span across every add/compose form
    for rel in ("js/views/companies.js", "js/lib/contactModal.js",
                "js/lib/reminderModal.js", "js/lib/composeModal.js"):
        assert not re.search(r"[A-Za-z] ?\*</label>", _read(rel)), rel


def test_wizard_marks_required_and_labels_website_optional():
    w = _read("js/views/welcome.js")
    # the one required wizard field now carries the marker + aria-required
    assert f'Company name {REQ_MARK}' in w
    assert 'data-field="companyName"' in w and 'aria-required="true"' in w
    # the previously-bare optional field now matches its siblings
    assert "Website (optional)" in w
    # and the asterisk is explained
    assert REQ_NOTE in w


def test_every_always_required_form_explains_its_asterisk():
    # add-company + add-job (companies.js), add-contact, add-reminder, wizard
    assert _read("js/views/companies.js").count(REQ_NOTE) == 2
    assert REQ_NOTE in _read("js/lib/contactModal.js")
    assert REQ_NOTE in _read("js/lib/reminderModal.js")
    assert REQ_NOTE in _read("js/views/welcome.js")
    # compose's required field is conditionally hidden, so it carries the marker
    # for glyph consistency but no persistent top legend
    assert REQ_MARK in _read("js/lib/composeModal.js")
    assert REQ_NOTE not in _read("js/lib/composeModal.js")
