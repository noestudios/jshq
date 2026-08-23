"""jshq.schedule: generators, verbs, and the settings-row round-trip.

Side-effect-free by construction: every shell-out goes through
schedule._run, which these tests monkeypatch to a recorder; the launchd
plist dir is pointed at tmp_path. Nothing here ever registers with the
real host scheduler.
"""

import plistlib
import sqlite3
import subprocess
from pathlib import PurePosixPath, PureWindowsPath

import pytest

from jshq import schedule

ARGV = ["/opt/venv/bin/jshq"]
# Pure flavored paths, not Path: these fixtures are cron/schtasks paths for a
# SPECIFIC platform, and plain Path("/data dir") stringifies to "\data dir"
# on a Windows runner (CI caught it). Pure paths render identically everywhere.
DATA_DIR = PurePosixPath("/home/user/.local/share/jshq")
TIMES = {"refresh": ["10:00", "16:00"], "backup": ["02:00"]}


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
    yield c
    c.close()


@pytest.fixture
def runner(monkeypatch):
    """Replace schedule._run with a recorder returning canned results."""

    class Runner:
        def __init__(self):
            self.calls = []
            self.results = {}  # cmd[0:2] tuple prefix -> CompletedProcess

        def __call__(self, cmd, input_text=None):
            self.calls.append((list(cmd), input_text))
            for prefix, proc in self.results.items():
                if tuple(cmd[: len(prefix)]) == prefix:
                    return proc
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    r = Runner()
    monkeypatch.setattr(schedule, "_run", r)
    return r


# --- parse_times ---


def test_parse_times_normalizes_dedupes_and_sorts():
    assert schedule.parse_times(["16:00", "9:05", "16:00"]) == ["09:05", "16:00"]


@pytest.mark.parametrize("bad", [[], None, "10:00", ["25:00"], ["10:60"], ["noon"], [10]])
def test_parse_times_rejects_garbage(bad):
    with pytest.raises(schedule.ScheduleError):
        schedule.parse_times(bad)


# --- settings row ---


def test_times_round_trip_through_the_settings_row(conn):
    schedule.write_times(conn, TIMES)
    assert schedule.read_times(conn) == TIMES


def test_read_times_defaults_on_missing_or_garbled_row(conn):
    assert schedule.read_times(conn) == schedule.DEFAULT_TIMES
    conn.execute("INSERT INTO settings VALUES (?, ?)", (schedule.SETTING_KEY, "not json"))
    assert schedule.read_times(conn) == schedule.DEFAULT_TIMES
    conn.execute(
        "UPDATE settings SET value = ? WHERE key = ?",
        ('{"refresh": ["8:00"], "backup": "oops"}', schedule.SETTING_KEY),
    )
    # Per-job tolerance: the readable job survives, the garbled one defaults.
    assert schedule.read_times(conn) == {"refresh": ["08:00"], "backup": ["02:00"]}


# --- generators ---


def test_launchd_plist_content():
    text = schedule.launchd_plist("refresh", TIMES["refresh"], ARGV, DATA_DIR)
    assert plistlib.loads(text.encode()) == {
        "Label": "com.jshq.refresh",
        "ProgramArguments": ["/opt/venv/bin/jshq", "refresh"],
        "EnvironmentVariables": {"JSHQ_DATA_DIR": str(DATA_DIR)},
        "StartCalendarInterval": [
            {"Hour": 10, "Minute": 0},
            {"Hour": 16, "Minute": 0},
        ],
    }


def test_launchd_plist_one_interval_per_time():
    text = schedule.launchd_plist("backup", ["02:00", "14:30", "23:45"], ARGV, DATA_DIR)
    loaded = plistlib.loads(text.encode())
    assert loaded["Label"] == "com.jshq.backup"
    assert loaded["StartCalendarInterval"] == [
        {"Hour": 2, "Minute": 0},
        {"Hour": 14, "Minute": 30},
        {"Hour": 23, "Minute": 45},
    ]


def test_cron_block_exact_text():
    block = schedule.cron_block(TIMES, ARGV, DATA_DIR)
    assert block == (
        f"{schedule.CRON_BEGIN}\n"
        "0 10 * * * JSHQ_DATA_DIR=/home/user/.local/share/jshq /opt/venv/bin/jshq refresh\n"
        "0 16 * * * JSHQ_DATA_DIR=/home/user/.local/share/jshq /opt/venv/bin/jshq refresh\n"
        "0 2 * * * JSHQ_DATA_DIR=/home/user/.local/share/jshq /opt/venv/bin/jshq backup\n"
        f"{schedule.CRON_END}\n"
    )


def test_cron_block_quotes_spaces():
    block = schedule.cron_block(TIMES, ["/Applications/My Tools/jshq"], PurePosixPath("/data dir"))
    assert "'/data dir'" in block
    assert "'/Applications/My Tools/jshq' refresh" in block


def test_schtasks_commands_one_trigger_per_time():
    cmds = schedule.schtasks_commands("refresh", TIMES["refresh"], ["C:\\jshq\\jshq.exe"], PureWindowsPath("C:\\data"))
    assert cmds == [
        [
            "schtasks", "/Create", "/F", "/TN", "jshq-refresh-1000",
            "/SC", "DAILY", "/ST", "10:00",
            "/TR", 'cmd /c "set JSHQ_DATA_DIR=C:\\data && C:\\jshq\\jshq.exe refresh"',
        ],
        [
            "schtasks", "/Create", "/F", "/TN", "jshq-refresh-1600",
            "/SC", "DAILY", "/ST", "16:00",
            "/TR", 'cmd /c "set JSHQ_DATA_DIR=C:\\data && C:\\jshq\\jshq.exe refresh"',
        ],
    ]


# --- launchd verbs ---


@pytest.fixture
def launchd(monkeypatch, tmp_path):
    monkeypatch.setattr(schedule, "backend", lambda: "launchd")
    monkeypatch.setattr(schedule, "LAUNCH_AGENTS_DIR", tmp_path / "LaunchAgents")
    return tmp_path / "LaunchAgents"


def test_launchd_install_writes_and_loads_both_plists(launchd, runner):
    result = schedule.install(TIMES)
    assert result == {"ok": True, "supported": True}
    for label in ("com.jshq.refresh", "com.jshq.backup"):
        assert (launchd / f"{label}.plist").is_file()
    # Idempotency mechanism: unload precedes every load.
    ops = [(c[0][0], c[0][1]) for c in runner.calls]
    assert ops == [
        ("launchctl", "unload"), ("launchctl", "load"),
        ("launchctl", "unload"), ("launchctl", "load"),
    ]


def test_launchd_reinstall_replaces_never_duplicates(launchd, runner):
    schedule.install(TIMES)
    schedule.install({"refresh": ["08:00"], "backup": ["03:00"]})
    text = (launchd / "com.jshq.refresh.plist").read_text(encoding="utf-8")
    loaded = plistlib.loads(text.encode())
    assert loaded["StartCalendarInterval"] == [{"Hour": 8, "Minute": 0}]
    assert len(list(launchd.glob("*.plist"))) == 2


def test_launchd_load_failure_surfaces_stderr(launchd, runner):
    runner.results[("launchctl", "load")] = subprocess.CompletedProcess(
        [], 1, stdout="", stderr="Load failed: 5: Input/output error"
    )
    result = schedule.install(TIMES)
    assert result["ok"] is False
    assert "Input/output error" in result["error"]


def test_launchd_uninstall_unloads_and_removes(launchd, runner):
    schedule.install(TIMES)
    result = schedule.uninstall()
    assert result == {"ok": True, "supported": True}
    assert list(launchd.glob("*.plist")) == []


def test_launchd_status_reads_plist_presence(launchd, runner, conn):
    schedule.write_times(conn, TIMES)
    assert schedule.status(conn)["installed"] == {"refresh": False, "backup": False}
    schedule.install(TIMES)
    st = schedule.status(conn)
    assert st["installed"] == {"refresh": True, "backup": True}
    assert st["supported"] is True
    assert st["platform"] == "launchd"
    assert st["times"] == TIMES


# --- cron verbs ---


@pytest.fixture
def cron(monkeypatch, runner):
    monkeypatch.setattr(schedule, "backend", lambda: "cron")
    return runner


def _crontab_state(runner, text, returncode=0):
    runner.results[("crontab", "-l")] = subprocess.CompletedProcess(
        [], returncode, stdout=text, stderr="" if returncode == 0 else "no crontab for user"
    )


def _written_crontab(runner):
    writes = [inp for cmd, inp in runner.calls if cmd == ["crontab", "-"]]
    assert writes, "no crontab write happened"
    return writes[-1]


def test_cron_install_appends_managed_block_to_empty_crontab(cron):
    _crontab_state(cron, "", returncode=1)  # no crontab yet
    result = schedule.install(TIMES)
    assert result["ok"] is True
    written = _written_crontab(cron)
    assert written.startswith(schedule.CRON_BEGIN)
    assert written.count("refresh") == 2 and written.count("backup") == 1


def test_cron_install_preserves_foreign_lines_and_replaces_own_block(cron):
    existing = (
        "MAILTO=me@example.com\n"
        "0 5 * * * /usr/local/bin/certbot renew\n"
        f"{schedule.CRON_BEGIN}\n"
        "0 9 * * * JSHQ_DATA_DIR=/old /old/jshq refresh\n"
        f"{schedule.CRON_END}\n"
    )
    _crontab_state(cron, existing)
    schedule.install(TIMES)
    written = _written_crontab(cron)
    assert "certbot renew" in written and "MAILTO" in written
    assert "/old/jshq" not in written  # replaced, not duplicated
    assert written.count(schedule.CRON_BEGIN) == 1


def test_cron_uninstall_leaves_foreign_lines_alone(cron):
    _crontab_state(cron, "0 5 * * * /usr/bin/other\n" + schedule.cron_block(TIMES, ARGV, DATA_DIR))
    result = schedule.uninstall()
    assert result["ok"] is True
    written = _written_crontab(cron)
    assert written == "0 5 * * * /usr/bin/other\n"


def test_cron_uninstall_without_block_writes_nothing(cron):
    _crontab_state(cron, "0 5 * * * /usr/bin/other\n")
    result = schedule.uninstall()
    assert result["ok"] is True
    assert [c for c, _ in cron.calls if c == ["crontab", "-"]] == []


def test_cron_status_detects_jobs_in_managed_block(cron, conn):
    _crontab_state(cron, schedule.cron_block(TIMES, ARGV, DATA_DIR))
    assert schedule.status(conn)["installed"] == {"refresh": True, "backup": True}
    _crontab_state(cron, "", returncode=1)
    assert schedule.status(conn)["installed"] == {"refresh": False, "backup": False}


def test_cron_write_failure_surfaces(cron):
    _crontab_state(cron, "", returncode=1)
    cron.results[("crontab", "-")] = subprocess.CompletedProcess([], 1, "", "crontab: not allowed")
    result = schedule.install(TIMES)
    assert result["ok"] is False and "not allowed" in result["error"]


# --- schtasks verbs ---


@pytest.fixture
def schtasks(monkeypatch, runner):
    monkeypatch.setattr(schedule, "backend", lambda: "schtasks")
    return runner


def _query_state(runner, names):
    rows = "\n".join(f'"\\{n}","Ready"' for n in names)
    runner.results[("schtasks", "/Query")] = subprocess.CompletedProcess([], 0, rows, "")


def test_schtasks_install_sweeps_stale_tasks_then_creates(schtasks):
    _query_state(schtasks, ["jshq-refresh-0900", "jshq-backup-0200"])
    result = schedule.install(TIMES)
    assert result["ok"] is True
    deletes = [c for c, _ in schtasks.calls if c[:2] == ["schtasks", "/Delete"]]
    creates = [c for c, _ in schtasks.calls if c[:2] == ["schtasks", "/Create"]]
    assert [d[4] for d in deletes] == ["jshq-refresh-0900", "jshq-backup-0200"]
    assert [c[4] for c in creates] == ["jshq-refresh-1000", "jshq-refresh-1600", "jshq-backup-0200"]


def test_schtasks_uninstall_deletes_every_jshq_task(schtasks):
    _query_state(schtasks, ["jshq-refresh-1000", "jshq-backup-0200", "other-task"])
    result = schedule.uninstall()
    assert result["ok"] is True
    deletes = [c for c, _ in schtasks.calls if c[:2] == ["schtasks", "/Delete"]]
    assert [d[4] for d in deletes] == ["jshq-refresh-1000", "jshq-backup-0200"]


def test_schtasks_status_reads_task_names(schtasks, conn):
    _query_state(schtasks, ["jshq-refresh-1000"])
    assert schedule.status(conn)["installed"] == {"refresh": True, "backup": False}


# --- unsupported platform ---


def test_unsupported_platform_returns_manual_instructions(monkeypatch):
    monkeypatch.setattr(schedule, "backend", lambda: None)
    for result in (schedule.install(TIMES), schedule.uninstall()):
        assert result["ok"] is False
        assert result["supported"] is False
        assert "jshq" in result["manual"] and "refresh" in result["manual"]
    st = schedule.status()
    assert st["supported"] is False and "manual" in st


def test_run_survives_a_missing_binary():
    proc = schedule._run(["definitely-not-a-real-binary-xyz"])
    assert proc.returncode == 127 and proc.stderr
