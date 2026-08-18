"""Pure reminder-suggestion engine."""

from datetime import date

from jshq.reminder_suggest import suggest_reminders

TODAY = date(2026, 6, 11)


def suggest(applications=(), activities=(), reminders=(), ignored=(), today=TODAY):
    return suggest_reminders(
        list(applications), list(activities), list(reminders), list(ignored), today
    )


APP = {"id": 5, "job_id": 9, "applied_date": "2026-06-08", "label": "PD @ TestCo"}
INTERVIEW = {"id": 3, "entity_type": "job", "entity_id": 9, "date": "2026-06-10",
             "type": "interview", "label": "PD @ TestCo"}
MEETING = {"id": 4, "entity_type": "contact", "entity_id": 2, "date": "2026-06-01",
           "type": "meeting", "label": "Dana"}


def test_application_followup():
    [s] = suggest(applications=[APP])
    assert s["key"] == "followup_application:application:5"
    assert s["type"] == "followup_application"
    assert s["due_date"] == "2026-06-15"  # applied + 7d
    assert s["entity_type"] == "job" and s["entity_id"] == 9
    assert "PD @ TestCo" in s["title"]


def test_application_stale_after_21d():
    old = APP | {"applied_date": "2026-05-20"}
    assert suggest(applications=[old]) == []
    edge = APP | {"applied_date": "2026-05-21"}  # exactly 21d → still shown
    assert len(suggest(applications=[edge])) == 1


def test_interview_thank_you_same_day():
    [s] = suggest(activities=[INTERVIEW])
    assert s["key"] == "thank_you:activity:3"
    assert s["due_date"] == "2026-06-10"
    assert s["entity_type"] == "job" and s["entity_id"] == 9


def test_interview_stale_after_7d():
    assert suggest(activities=[INTERVIEW | {"date": "2026-06-04"}]) != []  # exactly 7d
    assert suggest(activities=[INTERVIEW | {"date": "2026-06-03"}]) == []


def test_contact_meeting_ping():
    [s] = suggest(activities=[MEETING])
    assert s["key"] == "followup_contact:activity:4"
    assert s["due_date"] == "2026-06-15"  # met + 14d
    assert s["title"] == "Ping Dana"


def test_meeting_on_non_contact_ignored():
    assert suggest(activities=[MEETING | {"entity_type": "company"}]) == []


def test_meeting_stale_after_30d():
    assert suggest(activities=[MEETING | {"date": "2026-05-01"}]) == []


def test_ignored_key_suppresses():
    assert suggest(applications=[APP], ignored=["followup_application:application:5"]) == []


def test_existing_reminder_suppresses():
    matching = {"type": "followup_application", "entity_type": "job", "entity_id": 9}
    assert suggest(applications=[APP], reminders=[matching]) == []
    other_entity = matching | {"entity_id": 8}
    assert len(suggest(applications=[APP], reminders=[other_entity])) == 1


def test_bad_dates_skipped():
    assert suggest(applications=[APP | {"applied_date": None}],
                   activities=[INTERVIEW | {"date": "junk"}]) == []


def test_sorted_by_due_date():
    out = suggest(applications=[APP], activities=[INTERVIEW, MEETING])
    assert [s["due_date"] for s in out] == sorted(s["due_date"] for s in out)
