"""ICS generation + endpoints. Output is always parsed back via icalendar —
never string-compared (line folding and CRLF make that brittle)."""

from datetime import date, datetime, timezone

from icalendar import Calendar

from jshq.ics import build_calendar

REMINDER = {
    "id": 1,
    "title": "Follow up",
    "type": "followup_application",
    "entity_label": "PD @ TestCo",
    "due_date": "2026-06-15",
    "due_time": None,
    "notes": "ask about timeline",
    "ics_uid": "abc123@jobsearchhq",
    "updated_at": "2026-06-11 10:00:00",
}


def _events(raw: bytes):
    return Calendar.from_ical(raw).walk("VEVENT")


def test_calendar_props():
    cal = Calendar.from_ical(build_calendar([REMINDER]))
    assert str(cal["PRODID"]) == "-//Job Search HQ//EN"
    assert str(cal["VERSION"]) == "2.0"
    assert str(cal["X-WR-CALNAME"]) == "Job Search HQ"


def test_all_day_event():
    [ev] = _events(build_calendar([REMINDER]))
    assert str(ev["UID"]) == "abc123@jobsearchhq"
    assert ev["DTSTART"].params.get("VALUE") == "DATE"
    assert ev.decoded("dtstart") == date(2026, 6, 15)
    assert ev.decoded("dtend") == date(2026, 6, 16)
    assert "PD @ TestCo" in str(ev["DESCRIPTION"])
    assert "ask about timeline" in str(ev["DESCRIPTION"])


def test_timed_event_is_floating_local():
    [ev] = _events(build_calendar([REMINDER | {"due_time": "09:30"}]))
    start = ev.decoded("dtstart")
    assert start == datetime(2026, 6, 15, 9, 30)
    assert start.tzinfo is None  # floating: no TZID, no Z
    assert "TZID" not in ev["DTSTART"].params
    assert ev.decoded("dtend") == datetime(2026, 6, 15, 10, 30)


def test_dtstamp_and_sequence_from_updated_at():
    [ev] = _events(build_calendar([REMINDER]))
    updated = datetime(2026, 6, 11, 10, 0, tzinfo=timezone.utc)
    assert ev.decoded("dtstamp") == updated
    assert int(ev["SEQUENCE"]) == int(updated.timestamp())


def test_build_is_deterministic():
    assert build_calendar([REMINDER]) == build_calendar([REMINDER])


def test_malformed_due_time_degrades_to_all_day_not_a_crash():
    # A row carrying an out-of-range or unparseable due_time (written before the
    # API range-check, or by a direct DB edit) must not raise: build_calendar
    # runs over every pending reminder, so one bad row would 500 the whole
    # subscribed feed. It degrades to an all-day event instead.
    for bad in ("24:00", "00:60", "99:99", "notatime", "9", "1:2:3"):
        raw = build_calendar([REMINDER | {"due_time": bad}])
        [ev] = _events(raw)
        assert ev["DTSTART"].params.get("VALUE") == "DATE"
        assert ev.decoded("dtstart") == date(2026, 6, 15)


def test_feed_pending_only_and_subscribable(client, seed_reminder):
    seed_reminder(title="pending one")
    seed_reminder(title="done one", done=1)
    r = client.get("/api/calendar.ics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/calendar")
    assert "content-disposition" not in r.headers  # feed must be subscribable
    events = _events(r.content)
    assert [str(ev["SUMMARY"]) for ev in events] == ["pending one"]


def test_single_download_includes_done(client, seed_reminder):
    rid = seed_reminder(title="done one", done=1)
    r = client.get(f"/api/reminders/{rid}/ics")
    assert r.status_code == 200
    assert f'filename="reminder-{rid}.ics"' in r.headers["content-disposition"]
    assert len(_events(r.content)) == 1
    assert client.get("/api/reminders/999/ics").status_code == 404


def test_batch_download(client, seed_reminder):
    a = seed_reminder(title="a")
    seed_reminder(title="b")
    done = seed_reminder(title="c", done=1)

    r = client.get("/api/reminders/ics")  # no ids → all pending
    assert 'filename="reminders.ics"' in r.headers["content-disposition"]
    assert {str(ev["SUMMARY"]) for ev in _events(r.content)} == {"a", "b"}

    r = client.get(f"/api/reminders/ics?ids={a},{done}")  # explicit ids include done
    assert {str(ev["SUMMARY"]) for ev in _events(r.content)} == {"a", "c"}

    assert client.get("/api/reminders/ics?ids=junk").status_code == 400


def test_sequence_increases_after_edit(client, seed_reminder):
    rid = seed_reminder()  # seeded updated_at is in the past
    [before] = _events(client.get(f"/api/reminders/{rid}/ics").content)
    client.patch(f"/api/reminders/{rid}", json={"due_date": "2026-06-20"})
    [after] = _events(client.get(f"/api/reminders/{rid}/ics").content)
    assert int(after["SEQUENCE"]) > int(before["SEQUENCE"])
    assert str(after["UID"]) == str(before["UID"])
