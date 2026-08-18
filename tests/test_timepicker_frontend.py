"""The time-picker's outside-click dismissal must PERSIST a typed value.

No JS runtime in this repo (see test_settings_frontend), so the behavior is
pinned against the shipped source. The entry box is the primary path (focused
and text-selected on open), so "type a time, then click Save / another field"
is the expected flow; the capture click handler's outside-click branch used to
call bare close(), dropping the typed value so the reminder saved with no time.
"""

from jshq import paths

FRONTEND = paths.FRONTEND_DIR


def _read(rel):
    return (FRONTEND / rel).read_text(encoding="utf-8")


def test_outside_click_commits_typed_time_not_bare_close():
    src = _read("js/lib/timepicker.js")
    # typing is the primary path, so a typed-but-uncommitted value is expected
    assert "TYPING IS THE PRIMARY PATH" in src
    # a dedicated dismissal helper persists a valid/clearable value on the way out
    assert "function commitOrClose()" in src
    # the outside-click branch of the document capture click handler uses it
    assert 'closest(".time-pop"))) commitOrClose();' in src
    # and it commits without stealing focus (the user clicked elsewhere)
    assert "commitParts(parsed, { refocus: false })" in src
