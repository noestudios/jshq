"""v10 backfill: legacy applications.next_step/next_step_date values become
next_steps rows on init_db. Gated by the next_steps_backfilled settings flag
(deleting the flag simulates a pre-v10 database — on a real upgrade the flag
simply doesn't exist yet)."""

from jshq.db import connect, init_db


def _legacy_db(tmp_path, apps):
    """Build a DB that looks pre-v10: current schema, legacy field values
    present, backfill flag absent."""
    path = tmp_path / "legacy.sqlite"
    init_db(path)
    conn = connect(path)
    conn.execute("INSERT INTO companies (id, name) VALUES (1, 'TestCo')")
    for i, fields in enumerate(apps, start=1):
        # one job per application (the 1:1 unique index on job_id)
        conn.execute(
            """INSERT INTO jobs (id, company_id, title, url, dedupe_key)
               VALUES (?, 1, 'Designer', ?, ?)""",
            (i, f"https://jobs.example/{i}", f"k{i}"),
        )
        conn.execute(
            """INSERT INTO applications
               (id, job_id, status, next_step, next_step_date, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, '2026-01-01 00:00:00', '2026-02-03 04:05:06')""",
            (i, i, fields.get("status"), fields.get("next_step"),
             fields.get("next_step_date")),
        )
    conn.execute("DELETE FROM settings WHERE key = 'next_steps_backfilled'")
    conn.commit()
    conn.close()
    return path


def _rows(path):
    conn = connect(path)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM next_steps ORDER BY id")]
    finally:
        conn.close()


def _app(path, app_id):
    conn = connect(path)
    try:
        return dict(conn.execute(
            "SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone())
    finally:
        conn.close()


def test_backfill_migrates_field_pair(tmp_path):
    path = _legacy_db(tmp_path, [
        {"status": "screen", "next_step": "Recruiter screen",
         "next_step_date": "2026-08-28"},
    ])
    init_db(path)
    [row] = _rows(path)
    assert row["application_id"] == 1
    assert row["title"] == "Recruiter screen"
    assert row["due_date"] == "2026-08-28"
    assert row["status"] == "pending"
    # legacy per-application uid preserved so subscribed calendars update in place
    assert row["ics_uid"] == "app-nextstep-1@jobsearchhq"
    # provenance timestamps carried from the application (SEQUENCE continuity)
    assert row["updated_at"] == "2026-02-03 04:05:06"
    # legacy fields nulled
    app = _app(path, 1)
    assert app["next_step"] is None and app["next_step_date"] is None


def test_backfill_variants(tmp_path):
    path = _legacy_db(tmp_path, [
        {"status": "applied", "next_step": "   ", "next_step_date": "2026-06-20"},  # blank title
        {"status": "applied", "next_step": "Title only", "next_step_date": None},   # dateless
        {"status": "rejected", "next_step": "Leftover", "next_step_date": "2026-06-01"},  # closed
        {"status": None, "next_step": "Null status", "next_step_date": "2026-06-02"},     # legacy NULL
        {"status": "applied", "next_step": None, "next_step_date": None},           # nothing → no row
    ])
    init_db(path)
    rows = {r["application_id"]: r for r in _rows(path)}
    assert set(rows) == {1, 2, 3, 4}
    assert rows[1]["title"] == "Next step"           # blank title falls back
    assert rows[2]["due_date"] is None               # dateless migrates
    assert rows[3]["status"] == "dismissed"          # closed app → history, not live work
    assert rows[4]["status"] == "pending"            # NULL status counts as open


def test_backfill_is_one_shot(tmp_path):
    path = _legacy_db(tmp_path, [
        {"status": "applied", "next_step": "X", "next_step_date": "2026-06-20"},
    ])
    init_db(path)
    assert len(_rows(path)) == 1
    # a re-run adds nothing (flag set) — even after the user deletes the row
    conn = connect(path)
    conn.execute("DELETE FROM next_steps")
    conn.commit()
    conn.close()
    init_db(path)
    assert _rows(path) == []


def test_fresh_db_sets_flag_without_rows(tmp_path):
    path = tmp_path / "fresh.sqlite"
    init_db(path)
    conn = connect(path)
    flag = conn.execute(
        "SELECT value FROM settings WHERE key = 'next_steps_backfilled'").fetchone()
    conn.close()
    assert flag["value"] == "1"
    assert _rows(path) == []
