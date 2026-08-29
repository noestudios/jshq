"""Next-step rows on the calendar surfaces. Since v10 next steps are first-class
rows (next_steps table); the .ics feed carries pending dated rows only —
resolved (done/dismissed) rows stay in the JSON list for the in-app calendar
but vanish from the feed, like done reminders. ICS output is parsed back via
icalendar, never string-compared."""

from datetime import date, datetime, timezone

from icalendar import Calendar


def _events(raw: bytes):
    return Calendar.from_ical(raw).walk("VEVENT")


def _uids(raw: bytes):
    return {str(ev["UID"]) for ev in _events(raw)}


def test_next_steps_endpoint_shape(client, seed_application, seed_next_step):
    app_id = seed_application(status="applied")
    ns_id = seed_next_step(application_id=app_id, title="Send work samples",
                           due_date="2026-06-20")
    [ev] = client.get("/api/next-steps").json()
    assert ev["id"] == ns_id
    assert ev["application_id"] == app_id
    assert ev["due_date"] == "2026-06-20"
    assert ev["title"] == "Send work samples"
    assert ev["status"] == "pending"
    assert ev["resolved_at"] is None
    assert ev["entity_label"] == "Product Designer @ TestCo"  # role @ company
    assert ev["ics_uid"].endswith("@jobsearchhq")


def test_list_includes_resolved_rows(client, seed_application, seed_next_step):
    """The in-app calendar keeps resolved steps visible with status styling, so
    the list returns everything; pending sorts first."""
    app_id = seed_application(status="applied")
    seed_next_step(application_id=app_id, title="done one", status="done",
                   due_date="2026-06-01")
    seed_next_step(application_id=app_id, title="pending one", due_date="2026-06-20")
    seed_next_step(application_id=app_id, title="dismissed one", status="dismissed",
                   due_date="2026-06-02")
    titles = [e["title"] for e in client.get("/api/next-steps").json()]
    assert titles == ["pending one", "done one", "dismissed one"]


def test_pending_ordered_by_date_dateless_last(client, seed_application, seed_next_step):
    app_id = seed_application(status="applied")
    seed_next_step(application_id=app_id, title="later", due_date="2026-07-01")
    seed_next_step(application_id=app_id, title="undated", due_date=None)
    seed_next_step(application_id=app_id, title="sooner", due_date="2026-06-15")
    titles = [e["title"] for e in client.get("/api/next-steps").json()]
    assert titles == ["sooner", "later", "undated"]


def test_feed_unions_reminders_and_next_steps(client, seed_application,
                                              seed_next_step, seed_reminder):
    seed_reminder(title="a real reminder")
    app_id = seed_application(status="applied")
    seed_next_step(application_id=app_id, title="Prep interview loop",
                   due_date="2026-06-20", ics_uid="ns-loop@jobsearchhq")
    events = _events(client.get("/api/calendar.ics").content)
    summaries = {str(ev["SUMMARY"]) for ev in events}
    assert summaries == {"a real reminder", "Prep interview loop"}
    # the next-step is an all-day VALUE=DATE event carrying the row's own uid
    [ns] = [ev for ev in events if str(ev["SUMMARY"]) == "Prep interview loop"]
    assert str(ns["UID"]) == "ns-loop@jobsearchhq"
    assert ns["DTSTART"].params.get("VALUE") == "DATE"
    assert ns.decoded("dtstart") == date(2026, 6, 20)
    assert ns.decoded("dtend") == date(2026, 6, 21)
    assert "Product Designer @ TestCo" in str(ns["DESCRIPTION"])


def test_feed_sequence_tracks_row_updated_at(client, seed_next_step):
    seed_next_step(ics_uid="ns-seq@jobsearchhq", updated_at="2026-06-11 10:00:00")
    [ev] = [e for e in _events(client.get("/api/calendar.ics").content)
            if str(e["UID"]) == "ns-seq@jobsearchhq"]
    updated = datetime(2026, 6, 11, 10, 0, tzinfo=timezone.utc)
    assert ev.decoded("dtstamp") == updated
    assert int(ev["SEQUENCE"]) == int(updated.timestamp())


def test_resolved_rows_drop_from_feed_but_not_list(client, seed_next_step):
    """Done/dismissed follow the done-reminder convention: gone from the feed
    (subscribed clients drop the UID), kept in the JSON list."""
    ns_id = seed_next_step(title="X", ics_uid="ns-x@jobsearchhq")
    assert "ns-x@jobsearchhq" in _uids(client.get("/api/calendar.ics").content)
    client.patch(f"/api/next-steps/{ns_id}", json={"status": "done"})
    assert "ns-x@jobsearchhq" not in _uids(client.get("/api/calendar.ics").content)
    [row] = client.get("/api/next-steps").json()
    assert row["status"] == "done"


def test_dateless_rows_stay_off_the_feed(client, seed_next_step):
    seed_next_step(title="undated", due_date=None, ics_uid="ns-undated@jobsearchhq")
    assert "ns-undated@jobsearchhq" not in _uids(client.get("/api/calendar.ics").content)
    [row] = client.get("/api/next-steps").json()
    assert row["due_date"] is None


def test_closing_the_application_drops_it_from_the_feed(client, seed_application,
                                                        seed_next_step):
    """Rejecting the app auto-dismisses its pending steps — the feed drop is a
    consequence of the status flip, not of any read-time gate."""
    app_id = seed_application(status="interview")
    seed_next_step(application_id=app_id, title="X", ics_uid="ns-close@jobsearchhq")
    assert "ns-close@jobsearchhq" in _uids(client.get("/api/calendar.ics").content)
    client.put(f"/api/applications/{app_id}", json={"status": "rejected"})
    assert "ns-close@jobsearchhq" not in _uids(client.get("/api/calendar.ics").content)
    [row] = client.get("/api/next-steps").json()
    assert row["status"] == "dismissed"
