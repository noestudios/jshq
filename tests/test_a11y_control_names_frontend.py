"""Accessible names for form controls and nav (UX panel Pass B):
#60 Contacts/Applications inline-edit controls, #61 date-picker fields, #65
mobile primary-nav tabs.

Contacts/Applications used the same floating <span class="field-label"> markup as
Companies but omitted the aria-label Companies/Settings carry, so a screen reader
announced "combo box" / "edit text, <value>" with no field name (Level A). The
date fields were named by their formatted value (title), not their purpose. The
six main nav tabs carried title-only naming while Help/Settings also had
aria-label.

WCAG 2.2: SC 1.3.1, 3.3.2, 4.1.2 (Level A); SC 2.4.4.
"""

from jshq import paths

FRONTEND = paths.FRONTEND_DIR


def _read(rel):
    return (FRONTEND / rel).read_text(encoding="utf-8")


# ---- #60 Contacts / Applications control names ---------------------------

def test_contacts_controls_are_named():
    js = _read("js/views/contacts.js")
    for name in ("Company", "Source", "Role", "Email", "LinkedIn URL"):
        assert f'aria-label="{name}"' in js, name


def test_applications_controls_are_named():
    js = _read("js/views/applications.js")
    for name in ("Status", "Next step", "Resume version", "Cover note"):
        assert f'aria-label="{name}"' in js, name


# ---- #61 date-picker named by purpose ------------------------------------

def test_datepicker_accepts_and_emits_an_aria_label():
    js = _read("js/lib/datepicker.js")
    assert "ariaLabel }" in js  # destructured option
    assert '`aria-label="${esc(ariaLabel)}"`' in js


def test_date_fields_pass_a_purpose_label():
    contacts = _read("js/views/contacts.js")
    apps = _read("js/views/applications.js")
    assert 'ariaLabel: "Last contact"' in contacts
    assert 'ariaLabel: "Applied"' in apps
    assert 'ariaLabel: "Next step date"' in apps


# ---- #65 mobile nav tabs -------------------------------------------------

def test_all_six_main_nav_tabs_carry_aria_label():
    html = _read("index.html")
    for label in ("Today", "Jobs", "Applications", "Companies", "Contacts", "Calendar"):
        assert f'data-route="{label.lower()}" title="{label}" aria-label="{label}"' in html, label
