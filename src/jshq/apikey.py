"""The single authority for the Anthropic API key.

The key is the one secret jshq holds. It lives in ``DATA_DIR/.env`` (the file
``cli._load_env()`` stage 2 loads), never in the database and never sent to the
frontend. This module is the only place that reads or writes it, so the rules
about *which* copy is in force and how a saved key becomes live live in exactly
one spot.

Nothing here imports ``anthropic``: the app must run without the package, and
key management is pure file + ``os.environ`` work. The client is still built
per-call by the consumers (scoring, compose, jobparse), each of which reads
``os.environ`` through :func:`is_configured` — so a key saved via :func:`write_key`
is live for the next request without a restart (no singleton to invalidate).
"""

import os
import re
from pathlib import Path

from jshq import paths

ENV_KEY = "ANTHROPIC_API_KEY"

# The one actionable line shown wherever an AI feature is off for lack of a key.
# Every degraded path (the compose/tailor 503, the scoring skip, the URL-parse
# fallback) says the same thing and points at the Settings surface that sets it.
MISSING_MESSAGE = (
    "No Anthropic API key — add one in Settings → System to turn on AI features."
)

# A key line in the .env, tolerating a leading `export ` and surrounding space.
# Comment lines (`# ...`) never match, so guidance in the file is preserved.
_KEY_LINE = re.compile(rf"^\s*(?:export\s+)?{re.escape(ENV_KEY)}\s*=", re.ASCII)


def _env_path() -> Path:
    """The user's .env in the data dir. Resolved at call time so a test that
    monkeypatches ``paths.DATA_DIR`` (or points ``JSHQ_DATA_DIR`` at a temp dir)
    is followed."""
    return paths.DATA_DIR / ".env"


def _read_file_value() -> str | None:
    """The ANTHROPIC_API_KEY value written in DATA_DIR/.env, or None. A minimal
    reader — enough to compare on-disk against the effective env, not a full
    dotenv parser (python-dotenv owns loading; this only answers "what did we
    write")."""
    path = _env_path()
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not _KEY_LINE.match(line):
            continue
        value = line.split("=", 1)[1].strip()
        # Strip a single layer of matching quotes, mirroring dotenv.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        return value or None
    return None


def _validate(value: str) -> str:
    """A key is a single opaque token. Reject whitespace and control characters
    (either would corrupt the .env line or hand the SDK a malformed header); do
    NOT enforce an ``sk-ant-`` prefix — key formats are the provider's to change.
    """
    stripped = value.strip()
    if not stripped:
        raise ValueError("API key must not be empty")
    if any(ch.isspace() for ch in stripped) or any(ch < " " for ch in stripped):
        raise ValueError("API key must be a single token with no spaces")
    return stripped


def mask(value: str) -> str:
    """A key rendered safe to show: dots plus the last four characters, so a user
    can recognize which key is set without it being copyable or logged."""
    tail = value[-4:] if len(value) >= 4 else value
    return "····" + tail


def is_configured() -> bool:
    """Whether the effective environment has a key — the exact question the AI
    guards ask, since ``AsyncAnthropic()`` reads ``os.environ`` itself."""
    return bool(os.environ.get(ENV_KEY))


def set_process_key(value: str) -> None:
    """Make a key live in this process immediately. Clients are built per-call,
    so the next AI request picks it up with no restart."""
    os.environ[ENV_KEY] = value


def status() -> dict:
    """What the Settings UI needs, never the key itself.

    ``source`` says where the in-force key comes from: ``"data-dir"`` (our .env,
    which we can rewrite), ``"environment"`` (a real exported var or a cwd .env
    that dotenv loaded first — stage 1 wins under ``override=False``), or ``None``
    when no key is set. ``editable`` is False only for ``"environment"``: writing
    DATA_DIR/.env would be silently beaten by that shadow on the next start, so
    the UI must not claim a durable save.

    Limitation: provenance is inferred by value, so an exported var holding the
    *same* string as our .env reads as ``"data-dir"``. Harmless — the values agree.
    """
    effective = os.environ.get(ENV_KEY) or None
    file_value = _read_file_value()
    if effective is None:
        return {"configured": False, "masked": None, "source": None, "editable": True}
    source = "data-dir" if file_value == effective else "environment"
    return {
        "configured": True,
        "masked": mask(effective),
        "source": source,
        "editable": source == "data-dir",
    }


def _write_lines(path: Path, lines: list[str]) -> None:
    """Atomic, owner-only .env write: temp file created 0600 (POSIX; Windows
    has no mode bits), then replace. Mirrors the tmp+swap discipline of
    ``criteria.write_criteria``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines) + "\n" if lines else ""
    tmp = path.with_name(path.name + ".tmp")
    # 0600 at CREATION, not chmod-after-write: a default-umask temp briefly
    # holds the one secret the app has world-readable, and a swallowed chmod
    # failure would install it that way permanently. os.open's mode is
    # advisory-only on Windows (no mode bits) — same effective no-op as before.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    tmp.replace(path)


def write_key(value: str) -> dict:
    """Set the key in DATA_DIR/.env, preserving every other line (JSHQ_DATA_DIR
    especially), and make it live in this process. Returns the new status."""
    clean = _validate(value)
    path = _env_path()
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    new_line = f"{ENV_KEY}={clean}"
    out: list[str] = []
    replaced = False
    for line in existing:
        if _KEY_LINE.match(line):
            if not replaced:  # collapse any duplicate key lines into one
                out.append(new_line)
                replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(new_line)
    _write_lines(path, out)
    set_process_key(clean)
    return status()


def clear_key() -> dict:
    """Remove the key from DATA_DIR/.env and unset it in this process. Any other
    lines survive. Returns the new status (which may still report a shadowing
    ``environment`` key we cannot remove)."""
    path = _env_path()
    if path.exists():
        kept = [ln for ln in path.read_text(encoding="utf-8").splitlines() if not _KEY_LINE.match(ln)]
        _write_lines(path, kept)
    os.environ.pop(ENV_KEY, None)
    return status()
