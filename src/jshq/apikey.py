"""The single authority for the secrets jshq holds in ``DATA_DIR/.env``.

The Anthropic API key is the original resident; Providers Tier 2 added the
OpenAI-compatible endpoint's key alongside it. Both live in ``DATA_DIR/.env``
(the file ``cli._load_env()`` stage 2 loads), never in the database and never
sent to the frontend. This module is the only place that reads or writes
them, so the rules about *which* copy is in force and how a saved value
becomes live live in exactly one spot.

The module-level functions (``status``, ``write_key``, …) keep their original
Anthropic-only signatures — they are the contract the api-key routes, the
degradation paths, and the tests pin — and delegate to the generic
``*_env_value`` forms that take an env-var name.

Nothing here imports ``anthropic``: the app must run without the package, and
key management is pure file + ``os.environ`` work. Clients are still built
per-call by the consumers, each of which reads ``os.environ`` — so a key
saved here is live for the next request without a restart (no singleton to
invalidate).
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


def _key_line_re(env_key: str) -> re.Pattern:
    """A `env_key` line in the .env, tolerating a leading `export ` and
    surrounding space. Comment lines (`# ...`) never match, so guidance in the
    file is preserved."""
    return re.compile(rf"^\s*(?:export\s+)?{re.escape(env_key)}\s*=", re.ASCII)


def _env_path() -> Path:
    """The user's .env in the data dir. Resolved at call time so a test that
    monkeypatches ``paths.DATA_DIR`` (or points ``JSHQ_DATA_DIR`` at a temp dir)
    is followed."""
    return paths.DATA_DIR / ".env"


def _read_file_value(env_key: str) -> str | None:
    """The `env_key` value written in DATA_DIR/.env, or None. A minimal
    reader — enough to compare on-disk against the effective env, not a full
    dotenv parser (python-dotenv owns loading; this only answers "what did we
    write")."""
    path = _env_path()
    if not path.exists():
        return None
    line_re = _key_line_re(env_key)
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line_re.match(line):
            continue
        value = line.split("=", 1)[1].strip()
        # Strip a single layer of matching quotes, mirroring dotenv.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        return value or None
    return None


def _validate(value: str) -> str:
    """A key is a single opaque token. Reject whitespace and control characters
    (either would corrupt the .env line or hand the client a malformed header);
    do NOT enforce a provider prefix — key formats are the provider's to change.
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


def env_value_status(env_key: str) -> dict:
    """What a Settings UI needs for one secret, never the secret itself.

    ``source`` says where the in-force value comes from: ``"data-dir"`` (our
    .env, which we can rewrite), ``"environment"`` (a real exported var or a
    cwd .env that dotenv loaded first — stage 1 wins under ``override=False``),
    or ``None`` when nothing is set. ``editable`` is False only for
    ``"environment"``: writing DATA_DIR/.env would be silently beaten by that
    shadow on the next start, so the UI must not claim a durable save.

    Limitation: provenance is inferred by value, so an exported var holding the
    *same* string as our .env reads as ``"data-dir"``. Harmless — the values agree.
    """
    effective = os.environ.get(env_key) or None
    file_value = _read_file_value(env_key)
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
    # holds a secret world-readable, and a swallowed chmod failure would
    # install it that way permanently. os.open's mode is advisory-only on
    # Windows (no mode bits) — same effective no-op as before.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    tmp.replace(path)


def write_env_value(env_key: str, value: str) -> dict:
    """Set `env_key` in DATA_DIR/.env, preserving every other line
    (JSHQ_DATA_DIR and the other keys especially), and make it live in this
    process. Returns the new status."""
    clean = _validate(value)
    path = _env_path()
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    line_re = _key_line_re(env_key)
    new_line = f"{env_key}={clean}"
    out: list[str] = []
    replaced = False
    for line in existing:
        if line_re.match(line):
            if not replaced:  # collapse any duplicate key lines into one
                out.append(new_line)
                replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(new_line)
    _write_lines(path, out)
    os.environ[env_key] = clean
    return env_value_status(env_key)


def clear_env_value(env_key: str) -> dict:
    """Remove `env_key` from DATA_DIR/.env and unset it in this process. Any
    other lines survive. Returns the new status (which may still report a
    shadowing ``environment`` value we cannot remove)."""
    path = _env_path()
    line_re = _key_line_re(env_key)
    if path.exists():
        kept = [ln for ln in path.read_text(encoding="utf-8").splitlines() if not line_re.match(ln)]
        _write_lines(path, kept)
    os.environ.pop(env_key, None)
    return env_value_status(env_key)


# --- The Anthropic key's original interface, unchanged for its consumers. ---


def is_configured() -> bool:
    """Whether the effective environment has an Anthropic key — the exact
    question the AI guards ask, since ``AsyncAnthropic()`` reads
    ``os.environ`` itself."""
    return bool(os.environ.get(ENV_KEY))


def set_process_key(value: str) -> None:
    """Make an Anthropic key live in this process immediately. Clients are
    built per-call, so the next AI request picks it up with no restart."""
    os.environ[ENV_KEY] = value


def status() -> dict:
    """The Anthropic key's status for the Settings UI, never the key itself."""
    return env_value_status(ENV_KEY)


def write_key(value: str) -> dict:
    """Set the Anthropic key in DATA_DIR/.env and make it live. Returns the
    new status."""
    return write_env_value(ENV_KEY, value)


def clear_key() -> dict:
    """Remove the Anthropic key from DATA_DIR/.env and this process."""
    return clear_env_value(ENV_KEY)
