"""Dismissal digest for the scoring prompt (AI tier).

Recent dismissals + reasons are demonstrated taste; the scorer reads them as
context so similar roles rank lower without editing fit_criteria.md.
"""

import json
import sqlite3


def build_dismissal_digest(conn: sqlite3.Connection, limit: int = 20) -> str:
    rows = conn.execute(
        """SELECT content FROM activities
           WHERE entity_type = 'job' AND type = 'dismissal'
           ORDER BY id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    lines = []
    for row in rows:
        try:
            data = json.loads(row["content"] or "{}")
        except json.JSONDecodeError:
            continue
        title, reason = data.get("title"), data.get("reason")
        if not title or not reason:
            continue
        note = f" (note: {data['note']})" if data.get("note") else ""
        lines.append(f'- "{title}" — {reason}{note}')
    if not lines:
        return ""
    return (
        "Recently dismissed jobs (the user's demonstrated taste — score similar "
        "roles lower):\n" + "\n".join(lines)
    )
