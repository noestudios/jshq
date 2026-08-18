"""The Anthropic-key accessor: the .env is written safely, other lines survive,
the key is never leaked in status(), and a shadowing environment key is reported
honestly. No test reaches the network — key management is pure file + env work."""

import os
import stat
import sys

import pytest
from dotenv import dotenv_values

from jshq import apikey, paths


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Each test gets its own DATA_DIR and a clean process env for the key. The
    process-env restore matters because write_key/clear_key mutate os.environ
    directly (not through monkeypatch), so a leak would poison later tests."""
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    monkeypatch.delenv(apikey.ENV_KEY, raising=False)
    yield
    os.environ.pop(apikey.ENV_KEY, None)


def _env_file(tmp_path):
    return tmp_path / ".env"


def test_write_then_status_reports_data_dir(tmp_path):
    apikey.write_key("sk-ant-test-ABCD1234")
    st = apikey.status()
    assert st == {
        "configured": True,
        "masked": "····1234",
        "source": "data-dir",
        "editable": True,
    }


def test_status_none_when_unset():
    assert apikey.status() == {
        "configured": False,
        "masked": None,
        "source": None,
        "editable": True,
    }


def test_write_preserves_other_lines(tmp_path):
    env = _env_file(tmp_path)
    env.write_text(
        "# my config\nJSHQ_DATA_DIR=./data\nOTHER=keepme\n", encoding="utf-8"
    )
    apikey.write_key("sk-ant-XXXX9999")
    text = env.read_text(encoding="utf-8")
    assert "# my config" in text
    assert "JSHQ_DATA_DIR=./data" in text
    assert "OTHER=keepme" in text
    assert "ANTHROPIC_API_KEY=sk-ant-XXXX9999" in text


def test_write_replaces_existing_key_line_in_place(tmp_path):
    env = _env_file(tmp_path)
    env.write_text("A=1\nANTHROPIC_API_KEY=old\nB=2\n", encoding="utf-8")
    apikey.write_key("new-key-VAL0")
    lines = env.read_text(encoding="utf-8").splitlines()
    assert lines == ["A=1", "ANTHROPIC_API_KEY=new-key-VAL0", "B=2"]


def test_write_collapses_duplicate_key_lines(tmp_path):
    env = _env_file(tmp_path)
    env.write_text(
        "ANTHROPIC_API_KEY=one\nANTHROPIC_API_KEY=two\nKEEP=1\n", encoding="utf-8"
    )
    apikey.write_key("final-KEY9")
    lines = env.read_text(encoding="utf-8").splitlines()
    assert lines.count("ANTHROPIC_API_KEY=final-KEY9") == 1
    assert [ln for ln in lines if ln.startswith("ANTHROPIC_API_KEY=")] == [
        "ANTHROPIC_API_KEY=final-KEY9"
    ]
    assert "KEEP=1" in lines


def test_written_env_is_readable_by_dotenv(tmp_path):
    """The writer's output must parse back the same through the real loader —
    covers quoting/escaping concerns end to end, not just our own reader."""
    apikey.write_key("sk-ant-api03-Ab_9-xYz")
    values = dotenv_values(_env_file(tmp_path))
    assert values["ANTHROPIC_API_KEY"] == "sk-ant-api03-Ab_9-xYz"


def test_no_trailing_newline_corruption(tmp_path):
    env = _env_file(tmp_path)
    env.write_text("JSHQ_DATA_DIR=./data", encoding="utf-8")  # no trailing \n
    apikey.write_key("key-ABCD")
    text = env.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert "\n\n" not in text.rstrip("\n") + "\n"  # no blank line splicing
    assert "JSHQ_DATA_DIR=./data" in text


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits only")
def test_env_written_owner_only(tmp_path):
    apikey.write_key("secret-KEY1")
    mode = stat.S_IMODE(os.stat(_env_file(tmp_path)).st_mode)
    assert mode == 0o600


def test_write_makes_key_live_in_process(tmp_path):
    assert not apikey.is_configured()
    apikey.write_key("live-KEY2")
    assert apikey.is_configured()
    assert os.environ[apikey.ENV_KEY] == "live-KEY2"


@pytest.mark.parametrize("bad", ["", "   ", "has space", "tab\tkey", "line\nbreak"])
def test_write_rejects_whitespace_and_empty(bad):
    with pytest.raises(ValueError):
        apikey.write_key(bad)


def test_write_strips_surrounding_whitespace(tmp_path):
    apikey.write_key("  sk-ant-trimME  ")
    assert os.environ[apikey.ENV_KEY] == "sk-ant-trimME"
    assert dotenv_values(_env_file(tmp_path))["ANTHROPIC_API_KEY"] == "sk-ant-trimME"


def test_clear_removes_key_keeps_rest(tmp_path):
    env = _env_file(tmp_path)
    env.write_text("JSHQ_DATA_DIR=./data\nANTHROPIC_API_KEY=gone\n", encoding="utf-8")
    apikey.set_process_key("gone")
    st = apikey.clear_key()
    assert st["configured"] is False
    assert apikey.ENV_KEY not in os.environ
    text = env.read_text(encoding="utf-8")
    assert "ANTHROPIC_API_KEY" not in text
    assert "JSHQ_DATA_DIR=./data" in text


def test_shadowing_env_reported_not_editable(tmp_path, monkeypatch):
    """A key exported into the process (or a cwd .env dotenv loaded first) beats
    our DATA_DIR/.env on the next start. status() must say so, not pretend the
    data-dir copy is in force."""
    _env_file(tmp_path).write_text("ANTHROPIC_API_KEY=ondisk-KEY\n", encoding="utf-8")
    monkeypatch.setenv(apikey.ENV_KEY, "shadow-EXPORTED")
    st = apikey.status()
    assert st == {
        "configured": True,
        "masked": "····RTED",
        "source": "environment",
        "editable": False,
    }


def test_reader_strips_quotes(tmp_path, monkeypatch):
    _env_file(tmp_path).write_text('ANTHROPIC_API_KEY="quoted-KEY"\n', encoding="utf-8")
    monkeypatch.setenv(apikey.ENV_KEY, "quoted-KEY")
    # On-disk quoted value equals the effective (dotenv-stripped) env → data-dir.
    assert apikey.status()["source"] == "data-dir"


def test_export_prefixed_key_line_recognized(tmp_path, monkeypatch):
    _env_file(tmp_path).write_text("export ANTHROPIC_API_KEY=exp-KEY\n", encoding="utf-8")
    monkeypatch.setenv(apikey.ENV_KEY, "exp-KEY")
    assert apikey.status()["source"] == "data-dir"
    # And a rewrite collapses the export line to a plain assignment.
    apikey.write_key("exp-KEY2")
    lines = _env_file(tmp_path).read_text(encoding="utf-8").splitlines()
    assert lines == ["ANTHROPIC_API_KEY=exp-KEY2"]


def test_mask_short_value():
    assert apikey.mask("ab") == "····ab"
    assert apikey.mask("abcdef") == "····cdef"
