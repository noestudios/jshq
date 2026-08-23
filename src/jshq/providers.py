"""The AI provider roster and the one client factory (Providers Tier 2).

Two providers: ``anthropic`` (api.anthropic.com with the user's key — the
shipped default, untouched) and ``openai_compat`` (any OpenAI-compatible
endpoint the user explicitly configures: Ollama, LM Studio, llama.cpp server,
vLLM, OpenAI itself, Gemini's compat endpoint). aicfg owns WHICH provider and
model each task resolves to; this module owns the endpoint configuration,
readiness, and client construction, so main.py and the rescore loop never
name a client class again.

Storage split, deliberately: the compat API key is a secret and rides
apikey's ``DATA_DIR/.env`` machinery (0600, never in the DB, never in a
response); the base URL is configuration-as-disclosure and lives in a
settings row (``ai_providers``) where GET responses and backups can see it.
The env var is ``JSHQ_OPENAI_COMPAT_API_KEY`` — deliberately NOT
``OPENAI_API_KEY``, so a variable the user exported for other tools is never
silently picked up; the only endpoints this app contacts are the ones the
user configured in it.

"Graceful without an API key" widens here to "graceful without a credential
or endpoint, per task": ``is_ready``/``missing_message`` are the guard every
degradation path (503, scoring skip, jobparse fallback) asks.
"""

import ipaddress
import json
import sqlite3
from urllib.parse import urlsplit

from jshq import apikey

PROVIDERS = ("anthropic", "openai_compat")

SETTING_KEY = "ai_providers"

COMPAT_ENV_KEY = "JSHQ_OPENAI_COMPAT_API_KEY"

# The actionable line wherever an AI task points at the compat provider but no
# endpoint is configured — the sibling of apikey.MISSING_MESSAGE.
MISSING_ENDPOINT_MESSAGE = (
    "No AI endpoint configured — add the OpenAI-compatible base URL in "
    "Settings → System, or switch this task back to Anthropic."
)


def read_config(conn: sqlite3.Connection) -> dict:
    """{"openai_compat": {"base_url": str|None}} from the settings row.
    Tolerant of a missing/garbled row — anything unreadable reads as
    unconfigured (mirrors aicfg.read_overrides)."""
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
    compat = data.get("openai_compat")
    base_url = compat.get("base_url") if isinstance(compat, dict) else None
    if not isinstance(base_url, str) or not base_url.strip():
        base_url = None
    return {"openai_compat": {"base_url": base_url}}


def compat_base_url(conn: sqlite3.Connection) -> str | None:
    return read_config(conn)["openai_compat"]["base_url"]


def validate_base_url(url: str) -> str:
    """A usable endpoint base URL or ValueError with a user-facing sentence.
    http is allowed on purpose — localhost runtimes are the primary case; the
    cleartext caveat for non-local http lives in PRIVACY.md and the UI copy."""
    cleaned = url.strip()
    if any(ch.isspace() for ch in cleaned) or any(ch < " " for ch in cleaned):
        raise ValueError("Base URL must be a single line with no spaces")
    parts = urlsplit(cleaned)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(
            "Base URL must start with http:// or https:// and name a host, "
            "like http://localhost:11434/v1"
        )
    return cleaned.rstrip("/")


def is_local(base_url: str) -> bool:
    """Whether the endpoint is loopback-only — the ledger's "local" label and
    PRIVACY.md's "nothing leaves this machine" case. Deliberately
    conservative: localhost / 127.0.0.0/8 / [::1] only. A LAN box
    (192.168.x.x) is NOT local — its traffic leaves the machine and its spend
    shows as unpriced, not free-by-assumption."""
    host = urlsplit(base_url).hostname
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def is_ready(conn: sqlite3.Connection, provider: str) -> bool:
    """Whether `provider` can take a call right now: a key for Anthropic, a
    base URL for the compat endpoint (its key is optional — Ollama has none)."""
    if provider == "anthropic":
        return apikey.is_configured()
    if provider == "openai_compat":
        return compat_base_url(conn) is not None
    return False


def missing_message(provider: str) -> str:
    """The actionable not-ready sentence for `provider` — what the 503s, the
    scoring skip, and the jobparse fallback all say."""
    if provider == "openai_compat":
        return MISSING_ENDPOINT_MESSAGE
    return apikey.MISSING_MESSAGE


def build_client(conn: sqlite3.Connection, provider: str, *, max_retries: int):
    """The one place a client class is named. Callers already hold the conn
    and have checked is_ready; max_retries stays the caller's deliberate
    budget (scoring 6, jobparse 4, interactive 2, probes 0)."""
    if provider == "anthropic":
        # Lazy: the app must run without the anthropic package.
        from anthropic import AsyncAnthropic

        return AsyncAnthropic(max_retries=max_retries)
    if provider == "openai_compat":
        import os

        from jshq import oaicompat

        base_url = compat_base_url(conn)
        if base_url is None:
            raise ValueError(MISSING_ENDPOINT_MESSAGE)
        return oaicompat.OpenAICompatClient(
            base_url,
            os.environ.get(COMPAT_ENV_KEY) or None,
            max_retries=max_retries,
        )
    raise ValueError(f"unknown provider: {provider!r}")


def compat_status(conn: sqlite3.Connection) -> dict:
    """What the Settings UI needs about the compat endpoint — never the key."""
    base_url = compat_base_url(conn)
    return {
        "configured": base_url is not None,
        "base_url": base_url,
        "local": is_local(base_url) if base_url else None,
        "key": apikey.env_value_status(COMPAT_ENV_KEY),
    }
