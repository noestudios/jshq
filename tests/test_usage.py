"""usage.py — token pricing and the usage_totals settings accumulator."""

from datetime import date
from types import SimpleNamespace

from jshq import usage


def test_sonnet5_intro_rate_auto_reverts_on_cutoff():
    # Intro $2/$10 through 2026-08-31, standard $3/$15 from 2026-09-01, keyed on
    # the call's own date — so the ledger flips on the cutoff with no edit.
    assert usage.rate_for("claude-sonnet-5", on=date(2026, 6, 30)) == (2.00, 10.00)
    assert usage.rate_for("claude-sonnet-5", on=date(2026, 8, 31)) == (2.00, 10.00)
    assert usage.rate_for("claude-sonnet-5", on=date(2026, 9, 1)) == (3.00, 15.00)
    assert usage.rate_for("claude-sonnet-5", on=date(2027, 1, 1)) == (3.00, 15.00)
    # cost_of threads the date through to the rate (1M in + 1M out).
    u = SimpleNamespace(
        input_tokens=1_000_000, output_tokens=1_000_000,
        cache_read_input_tokens=0, cache_creation_input_tokens=0,
    )
    assert round(usage.cost_of("claude-sonnet-5", u, on=date(2026, 8, 31)), 2) == 12.00  # 2 + 10
    assert round(usage.cost_of("claude-sonnet-5", u, on=date(2026, 9, 1)), 2) == 18.00   # 3 + 15


def test_cost_of_prices_input_output_and_cache():
    u = SimpleNamespace(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_input_tokens=1_000_000,
        cache_creation_input_tokens=1_000_000,
    )
    # haiku: $1 in + $5 out + $0.10 cache-read (0.1x) + $1.25 cache-write (1.25x) = $7.35
    assert round(usage.cost_of("claude-haiku-4-5", u), 2) == 7.35


def test_cost_of_unknown_model_or_none_is_zero():
    assert usage.cost_of("nope", SimpleNamespace(input_tokens=5)) == 0.0
    assert usage.cost_of("claude-haiku-4-5", None) == 0.0


def test_record_usage_accumulates(db):
    u = {
        "input_tokens": 1000, "output_tokens": 200,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
    }
    usage.record_usage(db, "claude-haiku-4-5", u, calls=3)
    usage.record_usage(db, "claude-haiku-4-5", u, calls=2)
    totals = usage.read_usage_totals(db)
    hk = totals["by_model"]["claude-haiku-4-5"]
    assert hk["calls"] == 5
    assert hk["input"] == 2000
    assert hk["cost"] > 0
    assert totals["started_at"]  # stamped on first record


def test_record_usage_none_is_noop(db):
    usage.record_usage(db, "claude-haiku-4-5", None)
    assert usage.read_usage_totals(db) is None


def test_read_usage_totals_none_when_absent(db):
    assert usage.read_usage_totals(db) is None


def test_append_harness_ledger_writes_jsonl(tmp_path):
    import json

    target = tmp_path / "spend.jsonl"
    acc = {
        "input_tokens": 1000, "output_tokens": 200,
        "cache_read_input_tokens": 50, "cache_creation_input_tokens": 10,
    }
    out = usage.append_harness_ledger(
        "score_distribution", "claude-haiku-4-5", acc,
        calls=3, cost=0.0123456, args="--limit 3", path=target,
    )
    usage.append_harness_ledger(
        "calibrate_scoring", "claude-haiku-4-5", acc,
        calls=10, cost=0.08, path=target,
    )
    assert out == target
    lines = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2  # append, never overwrite
    first = lines[0]
    assert first["script"] == "score_distribution" and first["calls"] == 3
    assert first["input"] == 1000 and first["cache_write"] == 10
    assert first["cost"] == 0.012346  # rounded like every stored cost
    assert first["args"] == "--limit 3"
    assert first["ts"]  # stamped; format is the writer's business
    assert lines[1]["script"] == "calibrate_scoring" and lines[1]["args"] == ""
