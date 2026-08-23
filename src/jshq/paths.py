"""jshq.paths — the single source of truth for on-disk locations.

Two kinds of paths, deliberately kept apart:

- Package data (read-only, ships in the wheel): the frontend, and the
  default documents under defaults/. Resolved relative to this file —
  wheels install unzipped, so plain Path access works in both regular
  and editable installs.
- User data (read-write, survives upgrades): the SQLite DB, logos,
  application files, and the user's own criteria doc. Lives in DATA_DIR.

These are import-time constants, not functions, so consuming modules can
keep their own module-level constants (db.DB_PATH, logos.LOGOS_DIR, ...)
and tests can monkeypatch them attribute-by-attribute as they always have.
Consequence: JSHQ_DATA_DIR must be set before the first jshq import
(tests/conftest.py and cli.py both rely on this ordering).
"""

import os
import shutil
import sys
from pathlib import Path

ENV_DATA_DIR = "JSHQ_DATA_DIR"

PACKAGE_DIR: Path = Path(__file__).resolve().parent
FRONTEND_DIR: Path = PACKAGE_DIR / "frontend"
DEFAULTS_DIR: Path = PACKAGE_DIR / "defaults"

# Shipped defaults that get copied into DATA_DIR because the app edits them
# there: fit_criteria.md (written by the wizard, Settings → Scoring, and the
# synthesis applier) and voice_guide.md (the Settings voice editor). SEED_FILES
# holds the DEST names — what lands in
# DATA_DIR and what the rest of the app reads and writes. Read-only docs
# (AI-TELLS.md, user-manual.md) are served straight from DEFAULTS_DIR so they
# track the installed version. Seeding never overwrites, so an install that
# predates a new SEED_FILES entry gets the seed copied on its next start.
SEED_FILES: tuple[str, ...] = ("fit_criteria.md", "voice_guide.md", "resume/content.json")

# Phase 4 first-run is a BLANK SLATE: both editable docs seed from NEUTRAL
# *.starter.md templates, not the Alex Rivera example. Alex stays in DEFAULTS_DIR
# as the shipped reference (golden-prompt + calibration read it; the onboarding
# wizard offers it as a "see an example"). This maps each DEST name to its SOURCE
# filename in DEFAULTS_DIR; a dest absent here seeds from a same-named file.
SEED_RENAMES: dict[str, str] = {
    "fit_criteria.md": "fit_criteria.starter.md",
    "voice_guide.md": "voice_guide.starter.md",
    # Placeholder resume content (Phase 5b): before this seed existed, every
    # tailoring endpoint 500'd on a fresh install with a raw filesystem path.
    "resume/content.json": "resume/content.starter.json",
}


def default_data_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "jshq"
    if sys.platform == "win32":
        # LOCALAPPDATA, not roaming APPDATA: a WAL SQLite DB must never
        # ride a roaming profile.
        base = os.environ.get("LOCALAPPDATA")
        return (Path(base) if base else Path.home() / "AppData" / "Local") / "jshq"
    base = os.environ.get("XDG_DATA_HOME")
    return (Path(base) if base else Path.home() / ".local" / "share") / "jshq"


def resolve_data_dir() -> Path:
    override = os.environ.get(ENV_DATA_DIR)
    if override:
        return Path(override).expanduser().resolve()
    return default_data_dir()


DATA_DIR: Path = resolve_data_dir()  # frozen at first import — see module docstring


def jshq_argv() -> list[str]:
    """The absolute command that runs this install's `jshq` CLI — what a
    scheduler entry must invoke (schedulers run without the shell PATH that
    found `jshq` interactively). Prefers the console script; falls back to
    the interpreter running jshq.cli as a module, which always exists."""
    exe = shutil.which("jshq")
    if exe:
        return [str(Path(exe).resolve())]
    argv0 = Path(sys.argv[0])
    if argv0.name in ("jshq", "jshq.exe") and argv0.is_file():
        return [str(argv0.resolve())]
    return [sys.executable, "-m", "jshq.cli"]


def seed_data_dir(data_dir: Path | None = None) -> list[Path]:
    """Mechanical first-run seeding: create the data dir and copy any absent
    SEED_FILES from the packaged defaults (via SEED_RENAMES when the source
    filename differs — a *.starter.md template copied to its live name).
    Idempotent, never overwrites. Returns the paths created this call."""
    target_dir = data_dir if data_dir is not None else DATA_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    for name in SEED_FILES:
        target = target_dir / name
        if not target.exists():
            # A seed name may carry a subdirectory (resume/content.json).
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(DEFAULTS_DIR / SEED_RENAMES.get(name, name), target)
            created.append(target)
    return created
