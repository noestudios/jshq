"""The editable voice guide (Phase 3): served, saved to DATA_DIR, size-capped,
and seeded on first run. The doc is prose, so there is no structural validation
— only a byte cap.
"""

import pytest

from jshq import compose, paths


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    """Point the voice-guide read/write at a throwaway DATA_DIR per test."""
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    return tmp_path


def test_voice_guide_is_seeded_on_first_run(tmp_path):
    """Phase 3 added voice_guide.md to SEED_FILES; a fresh data dir gets a copy
    of the shipped starter so it becomes user-editable. Phase 4 makes that
    starter a NEUTRAL template (voice_guide.starter.md), not the Alex example."""
    assert "voice_guide.md" in paths.SEED_FILES
    created = paths.seed_data_dir(tmp_path)
    seeded = tmp_path / "voice_guide.md"
    assert seeded in created
    assert seeded.read_bytes() == (paths.DEFAULTS_DIR / "voice_guide.starter.md").read_bytes()


def test_get_serves_the_default_when_no_live_copy(client):
    body = client.get("/api/docs/voice-guide").json()
    # Falls back to the shipped guide (no DATA_DIR copy yet).
    assert body["markdown"] == compose.VOICE_GUIDE_PATH.read_text(encoding="utf-8")


def test_put_then_get_round_trips_and_persists(client, tmp_path):
    text = "Write plainly.\nNo em dashes.\nShort sentences."
    r = client.put("/api/docs/voice-guide", json={"markdown": text})
    assert r.status_code == 200
    assert r.json()["markdown"] == text
    assert (tmp_path / "voice_guide.md").read_text(encoding="utf-8") == text
    assert client.get("/api/docs/voice-guide").json()["markdown"] == text


def test_empty_guide_is_legal(client):
    r = client.put("/api/docs/voice-guide", json={"markdown": ""})
    assert r.status_code == 200
    assert client.get("/api/docs/voice-guide").json()["markdown"] == ""


def test_oversize_guide_is_422(client, tmp_path):
    r = client.put("/api/docs/voice-guide", json={"markdown": "x" * 200_001})
    assert r.status_code == 422
    assert not (tmp_path / "voice_guide.md").exists()  # nothing written
