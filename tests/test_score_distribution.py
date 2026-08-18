"""score_distribution.run — empty-population handling."""

import asyncio
import importlib.util
from pathlib import Path

# Not a package module; load by path (it inserts backend/ on sys.path itself
# and touches no network or DB at import time).
_SPEC = importlib.util.spec_from_file_location(
    "score_distribution",
    Path(__file__).resolve().parents[1] / "scripts" / "score_distribution.py",
)
score_distribution = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(score_distribution)


def test_empty_ids_population_exits_clean(db, db_path, seed_job, monkeypatch, capsys):
    """--ids naming only never-AI-scored rows (e.g. Tier-1 hard-fail sentinels)
    must exit 2 with a message, not crash in histogram() on an empty Counter."""
    job_id = seed_job(fit_score=0, score_detail=None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    real_connect = score_distribution.db.connect
    monkeypatch.setattr(score_distribution.db, "connect", lambda path=None: real_connect(db_path))

    code = asyncio.run(score_distribution.run(None, [job_id], None, "write"))

    assert code == 2
    out = capsys.readouterr().out
    assert f"ids not found or not AI-scored: [{job_id}]" in out
    assert "nothing to score" in out
