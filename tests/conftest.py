"""Test fixtures: each test gets a fresh temp-file DB; real user data is never touched."""

import itertools
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from pathlib import Path

# Positive guard, BEFORE any jshq import: jshq.paths freezes DATA_DIR at first
# import, so pointing it at a throwaway dir here makes it impossible for any
# test (or the app lifespan's init_db) to touch the real user data dir.
# Deliberate overwrite, not setdefault: a dev shell with JSHQ_DATA_DIR exported
# must not leak real data into the suite either.
os.environ["JSHQ_DATA_DIR"] = tempfile.mkdtemp(prefix="jshq-tests-")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from jshq import paths  # noqa: E402
from jshq.db import connect, get_db, init_db  # noqa: E402
from jshq.main import app  # noqa: E402
from jshq.scoring import criteria as criteria_mod  # noqa: E402

assert paths.DATA_DIR == Path(os.environ["JSHQ_DATA_DIR"]).resolve(), (
    "jshq.paths was imported before the test data-dir guard took effect"
)
# The live-doc anchor tests read criteria_mod.CRITERIA_PATH and expect the
# shipped Alex EXAMPLE values. Phase 4 seeds NEUTRAL *.starter.md templates
# instead, so after the mechanical seed overwrite the two editable docs with the
# reference example the anchor tests assert against. (test_paths_seed.py covers
# the real starter seeding into a throwaway dir.)
paths.seed_data_dir()
for _seed_name in ("fit_criteria.md", "voice_guide.md"):
    shutil.copyfile(paths.DEFAULTS_DIR / _seed_name, paths.DATA_DIR / _seed_name)


@pytest.fixture
def criteria_doc(tmp_path, monkeypatch):
    """A temp copy of the criteria doc, with CRITERIA_PATH redirected and the
    mtime cache reset — for tests that edit the doc through the API."""
    copy = tmp_path / "fit_criteria.md"
    shutil.copy(criteria_mod.CRITERIA_PATH, copy)
    monkeypatch.setattr(criteria_mod, "CRITERIA_PATH", copy)
    monkeypatch.setattr(criteria_mod, "_cache", None)
    return copy


def _insert(db: sqlite3.Connection, table: str, fields: dict) -> int:
    cols = ", ".join(fields)
    marks = ", ".join("?" * len(fields))
    cur = db.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(fields.values()))
    db.commit()
    return cur.lastrowid


@pytest.fixture(autouse=True)
def _no_live_anthropic(monkeypatch):
    """No test may ever reach a real AI endpoint (CLAUDE.md hard rule) — the
    suite runs keyless AND endpointless."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("JSHQ_OPENAI_COMPAT_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def notify_calls(monkeypatch):
    """No test may ever pop a real macOS notification (the suite would spam
    Notification Center on the dev Mac). Patch the subprocess boundary and
    record the would-be banners; tests assert on them by requesting this
    fixture by name. send()'s truncation and popups_enabled()'s gate still
    run for real."""
    calls = []
    monkeypatch.setattr(
        "jshq.notify._osascript",
        lambda message, title, sound: calls.append(
            {"message": message, "title": title, "sound": sound}
        ),
    )
    return calls


@pytest.fixture(autouse=True)
def _no_background_onboarding(monkeypatch):
    """create_company and the per-company refresh endpoint each spawn a real ATS
    fetch (network + its own DB connection to data/hq.sqlite); never let that run
    in tests. The detect_and_fetch_company / refresh_company_board unit tests call
    those functions directly instead."""
    monkeypatch.setattr("jshq.main._spawn_onboarding", lambda company_id: None)
    monkeypatch.setattr("jshq.main._spawn_company_refresh", lambda company_id: None)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.sqlite"
    init_db(path)
    return path


@pytest.fixture
def db(db_path) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    yield conn
    conn.close()


@pytest.fixture
def seed_company(db):
    """Factory: insert a company row, return its id."""

    def _seed(**overrides) -> int:
        fields = {"name": "TestCo", "ats_type": "greenhouse", "ats_slug": "testco"}
        fields.update(overrides)
        cols = ", ".join(fields)
        marks = ", ".join("?" * len(fields))
        cur = db.execute(f"INSERT INTO companies ({cols}) VALUES ({marks})", tuple(fields.values()))
        db.commit()
        return cur.lastrowid

    return _seed


@pytest.fixture
def seed_job(db, seed_company):
    """Factory: insert a job (and a company if none given), return its id."""
    counter = itertools.count(1)

    def _seed(company_id: int | None = None, **overrides) -> int:
        if company_id is None:
            company_id = seed_company()
        fields = {
            "company_id": company_id,
            "title": "Product Designer",
            "status": "active",
            "dedupe_key": f"{company_id}:JOB{next(counter)}",
        }
        fields.update(overrides)
        return _insert(db, "jobs", fields)

    return _seed


@pytest.fixture
def seed_contact(db):
    """Factory: insert a contact row, return its id."""

    def _seed(**overrides) -> int:
        fields = {"name": "Test Contact"}
        fields.update(overrides)
        return _insert(db, "contacts", fields)

    return _seed


@pytest.fixture
def seed_reminder(db):
    """Factory: insert a reminder row directly, return its id.

    updated_at defaults to a fixed past instant so tests can assert that
    edits bump it (API writes within the same second would otherwise tie).
    """
    counter = itertools.count(1)

    def _seed(**overrides) -> int:
        fields = {
            "title": "Test reminder",
            "type": "custom",
            "due_date": "2026-06-15",
            "done": 0,
            "ics_uid": f"seed-{next(counter)}@jobsearchhq",
            "created_at": "2026-01-01 00:00:00",
            "updated_at": "2026-01-01 00:00:00",
        }
        fields.update(overrides)
        return _insert(db, "reminders", fields)

    return _seed


@pytest.fixture
def seed_application(db, seed_job):
    """Factory: insert an application row directly, return its id.

    Timestamps default to a fixed past instant so tests can assert that
    edits bump updated_at (same-second API writes would otherwise tie).
    """

    def _seed(job_id: int | None = None, **overrides) -> int:
        if job_id is None:
            job_id = seed_job()
        fields = {
            "job_id": job_id,
            "status": "drafting",
            "created_at": "2026-01-01 00:00:00",
            "updated_at": "2026-01-01 00:00:00",
        }
        fields.update(overrides)
        return _insert(db, "applications", fields)

    return _seed


@pytest.fixture
def seed_next_step(db, seed_application):
    """Factory: insert a next_steps row directly, return its id.

    Timestamps default to a fixed past instant so tests can assert that
    edits bump updated_at (same-second API writes would otherwise tie).
    """
    counter = itertools.count(1)

    def _seed(application_id: int | None = None, **overrides) -> int:
        if application_id is None:
            application_id = seed_application()
        fields = {
            "application_id": application_id,
            "title": "Test next step",
            "due_date": "2026-06-20",
            "status": "pending",
            "ics_uid": f"seed-ns-{next(counter)}@jobsearchhq",
            "created_at": "2026-01-01 00:00:00",
            "updated_at": "2026-01-01 00:00:00",
        }
        fields.update(overrides)
        return _insert(db, "next_steps", fields)

    return _seed


@pytest.fixture
def seed_activity(db):
    """Factory: insert an activities row directly, return its id."""

    def _seed(**overrides) -> int:
        fields = {
            "entity_type": "general",
            "entity_id": None,
            "date": "2026-06-11",
            "type": "note",
            "content": None,
        }
        fields.update(overrides)
        return _insert(db, "activities", fields)

    return _seed


@pytest.fixture
def client(db_path) -> Iterator[TestClient]:
    def override_get_db() -> Iterator[sqlite3.Connection]:
        conn = connect(db_path)
        try:
            yield conn
        finally:
            conn.close()

    app.dependency_overrides[get_db] = override_get_db
    # No `with`: entering the context runs the app lifespan, whose init_db()
    # targets the real data/hq.sqlite. The fixture DB is already initialized.
    yield TestClient(app)
    app.dependency_overrides.clear()
