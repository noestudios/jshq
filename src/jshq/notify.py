"""Desktop pop-up notifications for background work (refresh / rescore).

macOS-only for now: osascript `display notification` works from scheduled
jobs and the API service alike, whether or not a browser is open, and macOS
attributes the banner to "Script Editor" (one-time permission prompt on
first use). Message text is passed as osascript ARGV — never interpolated
into AppleScript source — so quotes/backslashes in company names can't
inject script. Never raises: a notification is decoration, not work.

The darwin gate lives at the subprocess boundary (_osascript), not in
send(): send()'s truncation and settings logic runs on every platform, so
the suite exercises it everywhere and elsewhere it's a clean no-op.
"""

import json
import sqlite3
import subprocess
import sys

_SCRIPT = (
    "on run argv\n"
    "  display notification (item 1 of argv) "
    "with title (item 2 of argv) sound name (item 3 of argv)\n"
    "end run"
)


def popups_enabled(conn: sqlite3.Connection) -> bool:
    """settings.notify_popups gates pop-ups; absent or unparseable = ON.
    Only a stored JSON `false` disables (the Settings System-tab toggle)."""
    row = conn.execute(
        "SELECT value FROM settings WHERE key = 'notify_popups'"
    ).fetchone()
    if row is None or not row["value"]:
        return True
    try:
        return json.loads(row["value"]) is not False
    except json.JSONDecodeError:
        return True


def send(message: str, *, sound: str = "Glass", title: str = "Job Search HQ") -> None:
    """Fire-and-forget desktop banner. Safe to call from anywhere; never raises."""
    try:
        _osascript(message[:200], title, sound)
    except Exception:
        pass  # notification failure must never break the pipeline


def _osascript(message: str, title: str, sound: str) -> None:
    """The subprocess boundary — tests monkeypatch exactly this (conftest.py)."""
    if sys.platform != "darwin":
        return
    subprocess.run(
        ["osascript", "-e", _SCRIPT, message, title, sound],
        capture_output=True,
        timeout=5,
    )
