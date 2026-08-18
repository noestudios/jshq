"""jshq CLI: argument parsing and import discipline (Phase 1)."""

import pytest

from jshq import cli


def test_default_is_serve(monkeypatch):
    calls = {}
    monkeypatch.setattr(cli, "_load_env", lambda: calls.setdefault("env", True))

    import uvicorn

    def fake_run(target, host, port, reload):
        calls["run"] = (target, host, port, reload)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    assert cli.main([]) == 0
    assert calls["env"] is True
    assert calls["run"] == ("jshq.main:app", "127.0.0.1", cli.DEFAULT_PORT, False)


def test_serve_flags(monkeypatch):
    calls = {}
    monkeypatch.setattr(cli, "_load_env", lambda: None)

    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda target, host, port, reload: calls.update(
        target=target, host=host, port=port, reload=reload))
    assert cli.main(["serve", "--port", "6001", "--reload"]) == 0
    assert calls == {"target": "jshq.main:app", "host": "127.0.0.1", "port": 6001, "reload": True}


def test_no_host_flag():
    """The localhost invariant is enforced by refusing the knob entirely."""
    with pytest.raises(SystemExit):
        cli.main(["serve", "--host", "0.0.0.0"])


def test_refresh_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "_load_env", lambda: calls.append("env"))
    monkeypatch.setattr(cli, "refresh_job", lambda: calls.append("refresh"))
    assert cli.main(["refresh"]) == 0
    assert calls == ["env", "refresh"]


def test_backup_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "_load_env", lambda: calls.append("env"))
    monkeypatch.setattr(cli, "backup_job", lambda: calls.append("backup"))
    assert cli.main(["backup"]) == 0
    assert calls == ["env", "backup"]


def test_cli_module_has_no_import_time_jshq_imports():
    """jshq.paths freezes DATA_DIR at first import; cli must let the cwd .env
    load first. Guard the discipline, not just the current implementation."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(cli))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.col_offset == 0:
            assert not (node.module or "").startswith("jshq"), node.module
        if isinstance(node, ast.Import) and node.col_offset == 0:
            assert not any(a.name.startswith("jshq") for a in node.names), ast.dump(node)
