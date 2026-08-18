"""Wizard inline-validation errors are announced and associated with their
fields (UX panel Pass A, #30 / A11Y-01 — the blocker).

Before: fieldError() emitted a bare <p class="wizard-err"> with no role, no id,
and the inputs carried no aria-invalid/aria-describedby, so a screen reader read
the label but never the error. The filters step also moved no focus on a
validation failure, so the cause was silent.

WCAG 2.2: SC 3.3.1 (error identification, A), SC 4.1.3 (status messages, AA),
SC 1.3.1 (info and relationships, A). No JS runtime here (see
test_settings_frontend); the wiring is pinned against the shipped source.
"""

from jshq import paths

FRONTEND = paths.FRONTEND_DIR


def _welcome():
    return (FRONTEND / "js/views/welcome.js").read_text(encoding="utf-8")


def test_field_error_is_a_named_alert_region():
    js = _welcome()
    # role="alert" so the message is spoken when it appears, plus a stable id the
    # input's aria-describedby can point at.
    assert 'id="wiz-err-${name}" role="alert"' in js


def test_validated_inputs_carry_invalid_and_describedby_while_erroring():
    js = _welcome()
    # The helper that emits the paired attributes only while s.errors[name] is set.
    assert 'aria-invalid="true" aria-describedby="wiz-err-${name}"' in js
    # Applied to each of the three validated fields.
    for field in ("compFloor", "homeTown", "companyName"):
        assert f'errAttrs("{field}")' in js


def test_focus_moves_to_the_first_invalid_field_on_both_steps():
    js = _welcome()
    assert "function focusFirstError()" in js
    assert 'container.querySelector(\'[aria-invalid="true"]\')?.focus()' in js
    # Wired into next() for the filters step (previously silent) and the company
    # step (previously an unassociated sibling).
    assert js.count("return focusFirstError();") >= 2
