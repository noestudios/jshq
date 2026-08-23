"""One-command scheduler setup: `jshq schedule --install/--uninstall/--status`.

Writes the platform-native scheduler entries that run `jshq refresh`
(default 10:00 and 16:00) and `jshq backup` (default 02:00) — launchd
plists on macOS, a managed crontab block on Linux, `schtasks` tasks on
Windows — pointing at the resolved absolute jshq command with
JSHQ_DATA_DIR pinned to this install's data dir. The times live in the
`schedule` settings row, which the Settings → System control edits and
this module reads, so the CLI and the in-app control share one source
of truth.

Contracts:
- Install is idempotent: replace, never duplicate (unload-then-load on
  launchd, rewrite the managed block on cron, sweep-then-create on
  schtasks).
- Never silent: an unsupported scheduler returns the manual
  instructions instead of pretending; every failed shell-out surfaces
  its stderr in the result.
- The generators are pure functions of (job, times, argv, data_dir) —
  the unit-test surface. All shell-outs go through _run(), the one
  monkeypatch point; tests never touch the real host scheduler.
"""

import json
import plistlib
import re
import shlex
import sqlite3
import subprocess
import sys
from pathlib import Path, PurePath

from jshq import paths


class ScheduleError(Exception):
    """Invalid schedule configuration, message is user-facing."""


SETTING_KEY = "schedule"

JOBS = ("refresh", "backup")

DEFAULT_TIMES: dict[str, list[str]] = {
    "refresh": ["10:00", "16:00"],
    "backup": ["02:00"],
}

# Module-level constants (the paths.py convention): tests monkeypatch these
# attribute-by-attribute so nothing ever touches the real LaunchAgents dir.
LAUNCH_AGENTS_DIR: Path = Path.home() / "Library" / "LaunchAgents"

LABELS = {"refresh": "com.jshq.refresh", "backup": "com.jshq.backup"}

# The jshq-owned crontab region. Everything between the markers is replaced
# wholesale on install and removed on uninstall; lines outside are never
# touched.
CRON_BEGIN = "# BEGIN jshq schedule (written by `jshq schedule`; do not edit)"
CRON_END = "# END jshq schedule"

# Windows task names: one task per job per time (a schtasks daily task takes
# a single /ST). The prefix is what uninstall sweeps, so renamed times never
# strand a stale task.
TASK_PREFIX = "jshq-"

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def parse_times(raw) -> list[str]:
    """Normalized, deduplicated, sorted HH:MM list, or ScheduleError.
    Accepts 24h H:MM/HH:MM strings; an empty list is an error (a job with
    no times is "uninstall", not "install with nothing")."""
    if not isinstance(raw, list) or not raw:
        raise ScheduleError("Provide at least one time as HH:MM, like 10:00")
    parsed: set[tuple[int, int]] = set()
    for item in raw:
        m = _TIME_RE.match(item.strip()) if isinstance(item, str) else None
        if not m:
            raise ScheduleError(f"Not a valid time: {item!r} (use 24-hour HH:MM, like 16:30)")
        hour, minute = int(m.group(1)), int(m.group(2))
        if hour > 23 or minute > 59:
            raise ScheduleError(f"Not a valid time: {item!r} (use 24-hour HH:MM, like 16:30)")
        parsed.add((hour, minute))
    return [f"{h:02d}:{m:02d}" for h, m in sorted(parsed)]


def backend() -> str | None:
    if sys.platform == "darwin":
        return "launchd"
    if sys.platform.startswith("linux"):
        return "cron"
    if sys.platform == "win32":
        return "schtasks"
    return None


def read_times(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Per-job times from the settings row, defaults where absent. Tolerant
    of a missing/garbled row — anything unreadable reads as the default
    (mirrors providers.read_config)."""
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (SETTING_KEY,)
    ).fetchone()
    data = {}
    if row and row["value"]:
        try:
            loaded = json.loads(row["value"])
            if isinstance(loaded, dict):
                data = loaded
        except ValueError:
            pass
    times: dict[str, list[str]] = {}
    for job in JOBS:
        try:
            times[job] = parse_times(data.get(job))
        except ScheduleError:
            times[job] = list(DEFAULT_TIMES[job])
    return times


def write_times(conn: sqlite3.Connection, times: dict[str, list[str]]) -> None:
    """Persist validated per-job times to the settings row (the one source
    of truth the CLI and the API both read)."""
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (SETTING_KEY, json.dumps(times)),
    )
    conn.commit()


# --- generators (pure; no I/O) ---


def launchd_plist(job: str, times: list[str], argv: list[str], data_dir: PurePath) -> str:
    intervals = []
    for t in times:
        hour, minute = t.split(":")
        intervals.append({"Hour": int(hour), "Minute": int(minute)})
    return plistlib.dumps(
        {
            "Label": LABELS[job],
            "ProgramArguments": [*argv, job],
            "EnvironmentVariables": {paths.ENV_DATA_DIR: str(data_dir)},
            "StartCalendarInterval": intervals,
        },
        sort_keys=False,
    ).decode("utf-8")


def cron_block(times: dict[str, list[str]], argv: list[str], data_dir: PurePath) -> str:
    """The managed crontab region: one line per job per time."""
    cmd = " ".join(shlex.quote(a) for a in argv)
    env = f"{paths.ENV_DATA_DIR}={shlex.quote(str(data_dir))}"
    lines = [CRON_BEGIN]
    for job in JOBS:
        for t in times[job]:
            hour, minute = t.split(":")
            lines.append(f"{int(minute)} {int(hour)} * * * {env} {cmd} {job}")
    lines.append(CRON_END)
    return "\n".join(lines) + "\n"


def _task_name(job: str, time: str) -> str:
    return f"{TASK_PREFIX}{job}-{time.replace(':', '')}"


def schtasks_commands(job: str, times: list[str], argv: list[str], data_dir: PurePath) -> list[list[str]]:
    """One `schtasks /Create` per time. /F replaces a same-named task; the
    install sweep handles renamed ones. The command rides `cmd /c` so the
    data dir env var reaches the job without a wrapper script."""
    cmd = " ".join(f'"{a}"' if " " in a else a for a in [*argv, job])
    tr = f'cmd /c "set {paths.ENV_DATA_DIR}={data_dir} && {cmd}"'
    return [
        ["schtasks", "/Create", "/F", "/TN", _task_name(job, t), "/SC", "DAILY", "/ST", t, "/TR", tr]
        for t in times
    ]


def manual_text(argv: list[str] | None = None, data_dir: PurePath | None = None) -> str:
    """The fallback instructions for a system without a supported scheduler
    (and the README's manual path) — printed, never silently skipped."""
    cmd = " ".join(argv if argv is not None else paths.jshq_argv())
    dd = str(data_dir if data_dir is not None else paths.DATA_DIR)
    return (
        "Automatic scheduling is not supported on this system. Point your own\n"
        "scheduler (cron, launchd, Task Scheduler) at the installed commands:\n"
        f"  {cmd} refresh   twice a day (for example 10:00 and 16:00)\n"
        f"  {cmd} backup    nightly (for example 02:00)\n"
        f"with the environment variable {paths.ENV_DATA_DIR}={dd}\n"
        "cron example:\n"
        f"  0 10,16 * * * {paths.ENV_DATA_DIR}={shlex.quote(dd)} {cmd} refresh\n"
        f"  0 2 * * * {paths.ENV_DATA_DIR}={shlex.quote(dd)} {cmd} backup\n"
    )


# --- shell-outs and the three verbs ---


def _run(cmd: list[str], input_text: str | None = None) -> subprocess.CompletedProcess:
    """The one subprocess seam (tests monkeypatch this). Never raises: a
    missing binary comes back as returncode 127 with the error in stderr."""
    try:
        return subprocess.run(cmd, input=input_text, capture_output=True, text=True)
    except OSError as exc:
        return subprocess.CompletedProcess(cmd, 127, stdout="", stderr=str(exc))


def _fail(step: str, proc: subprocess.CompletedProcess) -> dict:
    detail = (proc.stderr or proc.stdout or "").strip()
    return {
        "ok": False,
        "supported": True,
        "error": f"{step} failed (exit {proc.returncode})" + (f": {detail}" if detail else ""),
    }


def _read_crontab() -> str:
    proc = _run(["crontab", "-l"])
    # Exit 1 with "no crontab for <user>" is the empty case, not an error.
    return proc.stdout if proc.returncode == 0 else ""


def _strip_cron_block(text: str) -> str:
    lines = text.splitlines()
    kept: list[str] = []
    inside = False
    for line in lines:
        if line.strip() == CRON_BEGIN:
            inside = True
            continue
        if line.strip() == CRON_END:
            inside = False
            continue
        if not inside:
            kept.append(line)
    out = "\n".join(kept).strip("\n")
    return out + "\n" if out else ""


def _schtasks_names() -> list[str]:
    proc = _run(["schtasks", "/Query", "/FO", "CSV", "/NH"])
    if proc.returncode != 0:
        return []
    names = []
    for line in proc.stdout.splitlines():
        # First CSV field is "\TaskName"; jshq tasks live at the root.
        name = line.split(",", 1)[0].strip().strip('"').lstrip("\\")
        if name.startswith(TASK_PREFIX):
            names.append(name)
    return names


def install(times: dict[str, list[str]]) -> dict:
    be = backend()
    if be is None:
        return {"ok": False, "supported": False, "manual": manual_text()}
    argv = paths.jshq_argv()
    data_dir = paths.DATA_DIR

    if be == "launchd":
        LAUNCH_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
        for job in JOBS:
            plist_path = LAUNCH_AGENTS_DIR / f"{LABELS[job]}.plist"
            # Unload any prior version first (idempotency); failure here just
            # means it wasn't loaded.
            _run(["launchctl", "unload", str(plist_path)])
            plist_path.write_text(launchd_plist(job, times[job], argv, data_dir), encoding="utf-8")
            proc = _run(["launchctl", "load", str(plist_path)])
            if proc.returncode != 0:
                return _fail(f"launchctl load ({job})", proc)
        return {"ok": True, "supported": True}

    if be == "cron":
        remaining = _strip_cron_block(_read_crontab())
        new_text = remaining + cron_block(times, argv, data_dir)
        proc = _run(["crontab", "-"], input_text=new_text)
        if proc.returncode != 0:
            return _fail("crontab write", proc)
        return {"ok": True, "supported": True}

    # schtasks: sweep every jshq-* task first so a changed time never strands
    # its old task, then create the current set.
    for name in _schtasks_names():
        proc = _run(["schtasks", "/Delete", "/F", "/TN", name])
        if proc.returncode != 0:
            return _fail(f"schtasks delete ({name})", proc)
    for job in JOBS:
        for cmd in schtasks_commands(job, times[job], argv, data_dir):
            proc = _run(cmd)
            if proc.returncode != 0:
                return _fail(f"schtasks create ({job})", proc)
    return {"ok": True, "supported": True}


def uninstall() -> dict:
    be = backend()
    if be is None:
        return {"ok": False, "supported": False, "manual": manual_text()}

    if be == "launchd":
        for job in JOBS:
            plist_path = LAUNCH_AGENTS_DIR / f"{LABELS[job]}.plist"
            _run(["launchctl", "unload", str(plist_path)])
            plist_path.unlink(missing_ok=True)
        return {"ok": True, "supported": True}

    if be == "cron":
        current = _read_crontab()
        if CRON_BEGIN not in current:
            return {"ok": True, "supported": True}
        proc = _run(["crontab", "-"], input_text=_strip_cron_block(current))
        if proc.returncode != 0:
            return _fail("crontab write", proc)
        return {"ok": True, "supported": True}

    for name in _schtasks_names():
        proc = _run(["schtasks", "/Delete", "/F", "/TN", name])
        if proc.returncode != 0:
            return _fail(f"schtasks delete ({name})", proc)
    return {"ok": True, "supported": True}


def status(conn: sqlite3.Connection | None = None) -> dict:
    """Live installed state per job plus the effective times. Installed-ness
    is read from the OS (plist on disk / managed block present / jshq-*
    tasks listed), never from a stored flag — reality is the source of
    truth. `conn` supplies the stored times; without it, defaults."""
    be = backend()
    times = read_times(conn) if conn is not None else {j: list(DEFAULT_TIMES[j]) for j in JOBS}
    result: dict = {
        "platform": be or sys.platform,
        "supported": be is not None,
        "times": times,
        "command": paths.jshq_argv(),
        "data_dir": str(paths.DATA_DIR),
        "installed": {job: False for job in JOBS},
    }
    if be == "launchd":
        for job in JOBS:
            result["installed"][job] = (LAUNCH_AGENTS_DIR / f"{LABELS[job]}.plist").is_file()
    elif be == "cron":
        current = _read_crontab()
        if CRON_BEGIN in current:
            block = current.split(CRON_BEGIN, 1)[1].split(CRON_END, 1)[0]
            for job in JOBS:
                result["installed"][job] = any(
                    line.rstrip().endswith(f" {job}") for line in block.splitlines()
                )
    elif be == "schtasks":
        names = _schtasks_names()
        for job in JOBS:
            result["installed"][job] = any(n.startswith(f"{TASK_PREFIX}{job}-") for n in names)
    else:
        result["manual"] = manual_text()
    return result
