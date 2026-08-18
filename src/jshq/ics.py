"""ICS generation. Pure builders over reminder dicts.

Timed events are deliberately floating local time (no TZID, no UTC suffix):
this is a local-first single-user app and wall-clock times must not shift.
DTSTAMP/LAST-MODIFIED come from updated_at (sqlite datetime('now') is UTC,
which RFC 5545 requires there), and SEQUENCE is its epoch so every edit
bumps it — same-UID re-imports replace instead of duplicate.
"""

from datetime import date, datetime, timedelta, timezone

from icalendar import Calendar, Event

PRODID = "-//Job Search HQ//EN"


def _updated_utc(value: str | None) -> datetime:
    if not value:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def _timed_start(day: date, due_time: str | None) -> datetime | None:
    """The floating-local start for a timed reminder, or None for all-day.

    The API tightened due_time validation to a real HH:MM range, but a row
    written before that (or by a direct DB edit) could still carry a malformed
    or out-of-range value. build_calendar runs over every pending reminder, so
    one such row raising here would 500 the whole subscribed feed — degrade it
    to an all-day event instead.
    """
    if not due_time:
        return None
    try:
        hour, minute = (int(p) for p in due_time.split(":"))
        return datetime(day.year, day.month, day.day, hour, minute)  # naive = floating
    except ValueError:
        return None


def build_event(reminder: dict) -> Event:
    event = Event()
    event.add("uid", reminder["ics_uid"])
    day = date.fromisoformat(reminder["due_date"])
    start = _timed_start(day, reminder.get("due_time"))
    if start is not None:
        event.add("dtstart", start)
        event.add("dtend", start + timedelta(hours=1))
    else:
        event.add("dtstart", day)  # date object → VALUE=DATE all-day
        event.add("dtend", day + timedelta(days=1))
    event.add("summary", reminder["title"])
    description = "\n".join(
        part for part in (reminder.get("entity_label"), reminder.get("notes")) if part
    )
    if description:
        event.add("description", description)
    if reminder.get("type"):
        event.add("categories", reminder["type"])
    event.add("status", "CONFIRMED")
    updated = _updated_utc(reminder.get("updated_at"))
    event.add("dtstamp", updated)
    event.add("last-modified", updated)
    event.add("sequence", int(updated.timestamp()))
    return event


def build_calendar(reminders: list[dict], name: str = "Job Search HQ") -> bytes:
    calendar = Calendar()
    calendar.add("prodid", PRODID)
    calendar.add("version", "2.0")
    calendar.add("method", "PUBLISH")
    calendar.add("x-wr-calname", name)
    for reminder in reminders:
        calendar.add_component(build_event(reminder))
    return calendar.to_ical()
