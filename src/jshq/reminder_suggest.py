"""Auto-suggested reminders on events.

Pure function, no I/O — mirrors scoring/suggest.py. The caller gathers rows
and passes `today` explicitly so tests are deterministic. Suggestions are
one-click accept, never auto-created; the stable `key` (built from row ids)
is what accept/ignore records so a decision is idempotent forever.
"""

from datetime import date, timedelta

# Days after the trigger date before a suggestion goes stale and stops showing.
_WINDOWS = {"followup_application": 21, "thank_you": 7, "followup_contact": 30}


def _parse(value) -> date | None:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def suggest_reminders(
    applications: list[dict],
    activities: list[dict],
    reminders: list[dict],
    ignored: list[str],
    today: date,
) -> list[dict]:
    """applications: {id, job_id, applied_date, label}; activities: {id,
    entity_type, entity_id, date, type, label} (interview/meeting only);
    reminders: {type, entity_type, entity_id} for suppression."""
    existing = {(r["type"], r["entity_type"], r["entity_id"]) for r in reminders}
    skip = set(ignored)
    out = []

    def add(key, type_, title, due, entity_type, entity_id, context):
        # An existing reminder of the same (type, entity) — done or pending,
        # manual or accepted — silences the suggestion.
        if key in skip or (type_, entity_type, entity_id) in existing:
            return
        out.append({
            "key": key, "type": type_, "title": title, "due_date": due.isoformat(),
            "entity_type": entity_type, "entity_id": entity_id, "context": context,
        })

    for application in applications:
        applied = _parse(application.get("applied_date"))
        if applied is None or today - applied > timedelta(days=_WINDOWS["followup_application"]):
            continue
        add(
            f"followup_application:application:{application['id']}",
            "followup_application",
            f"Follow up on application — {application['label']}",
            applied + timedelta(days=7),
            "job", application["job_id"],
            f"Applied {applied.isoformat()}",
        )

    for activity in activities:
        when = _parse(activity.get("date"))
        if when is None:
            continue
        label = activity.get("label") or "(deleted)"
        if activity["type"] == "interview":
            if today - when > timedelta(days=_WINDOWS["thank_you"]):
                continue
            add(
                f"thank_you:activity:{activity['id']}",
                "thank_you",
                f"Thank-you note — {label}",
                when,
                activity["entity_type"], activity["entity_id"],
                f"Interview on {when.isoformat()}",
            )
        elif activity["type"] == "meeting" and activity["entity_type"] == "contact":
            if today - when > timedelta(days=_WINDOWS["followup_contact"]):
                continue
            add(
                f"followup_contact:activity:{activity['id']}",
                "followup_contact",
                f"Ping {label}",
                when + timedelta(days=14),
                "contact", activity["entity_id"],
                f"Met {when.isoformat()}",
            )

    out.sort(key=lambda s: (s["due_date"], s["key"]))
    return out
