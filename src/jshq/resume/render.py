"""Resume rendering: data/resume/content.json → HTML → PDF.

The template is the formatting: agents edit content.json, never this module
or the CSS, so every render matches the blessed design. PDF generation uses
the system Google Chrome in headless mode (owner decision, 2026-06-11 —
zero new dependencies; re-bless fidelity after major Chrome updates).

Content text supports minimal inline markup: **bold**, *italic*,
[text](https://url). Everything is HTML-escaped before markup expansion.
"""

import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from jshq import paths

CONTENT_PATH = paths.DATA_DIR / "resume" / "content.json"
TEMPLATE_DIR = Path(__file__).resolve().parent
TEMPLATE_PATH = TEMPLATE_DIR / "template.html"
CSS_PATH = TEMPLATE_DIR / "resume.css"

# Explicit override: JSHQ_CHROME env var, or tests monkeypatching this
# attribute. None means "discover at render time" via _find_chrome().
CHROME_BIN: Path | None = Path(os.environ["JSHQ_CHROME"]) if os.environ.get("JSHQ_CHROME") else None
CHROME_TIMEOUT = 60  # seconds

# PATH names first, then per-OS install locations. Edge is a Chromium and
# ships with Windows, so Windows users need zero installs to render PDFs.
_CHROME_NAMES = ("google-chrome", "google-chrome-stable", "chromium",
                 "chromium-browser", "chrome", "msedge")


def _chrome_candidates() -> list[Path]:
    if sys.platform == "darwin":
        return [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
            Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        ]
    if sys.platform == "win32":
        roots = [os.environ.get(v) for v in ("ProgramFiles", "ProgramFiles(x86)", "LocalAppData")]
        subpaths = (r"Google\Chrome\Application\chrome.exe",
                    r"Microsoft\Edge\Application\msedge.exe",
                    r"Chromium\Application\chrome.exe")
        return [Path(root) / sub for root in roots if root for sub in subpaths]
    return [Path("/usr/bin/google-chrome"), Path("/usr/bin/chromium"),
            Path("/usr/bin/chromium-browser"), Path("/snap/bin/chromium")]


def _find_chrome() -> Path:
    for name in _CHROME_NAMES:
        found = shutil.which(name)
        if found:
            return Path(found)
    for candidate in _chrome_candidates():
        if candidate.exists():
            return candidate
    raise ResumeError(
        "no Chrome/Chromium/Edge found for PDF rendering — install one, "
        "or point the JSHQ_CHROME environment variable at its executable"
    )

SECTION_TYPES = {"paragraph", "columns", "keyvalue", "bullets", "roles"}


class ResumeError(Exception):
    """Bad content, missing files, or a failed render."""


# ---------------------------------------------------------------- content

def load_content(path: Path = CONTENT_PATH) -> dict:
    try:
        content = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        # Data-dir-relative on purpose: this string reaches the user as a
        # toast, and an absolute server path is noise they can't act on.
        raise ResumeError(
            "resume/content.json is missing from your data folder — it is "
            "seeded with a starter on first run; restart the app to re-seed it"
        ) from None
    except OSError as exc:
        raise ResumeError(f"cannot read resume content: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ResumeError(f"resume content is not valid JSON: {exc}") from exc
    validate_content(content)
    return content


def validate_content(content: dict) -> None:
    if not isinstance(content, dict):
        raise ResumeError("content must be a JSON object")
    if content.get("version") != 1:
        raise ResumeError(f"unsupported content version: {content.get('version')!r}")
    for key in ("name", "title", "contact", "sections"):
        if not content.get(key):
            raise ResumeError(f"content is missing {key!r}")
    if not isinstance(content["sections"], list):
        raise ResumeError("sections must be a list")

    seen_ids: set[str] = set()

    def claim(node_id, where):
        if not node_id or not isinstance(node_id, str):
            raise ResumeError(f"missing id in {where}")
        if node_id in seen_ids:
            raise ResumeError(f"duplicate id {node_id!r}")
        seen_ids.add(node_id)

    required_field = {
        "paragraph": "text", "columns": "items", "keyvalue": "rows",
        "bullets": "bullets", "roles": "roles",
    }

    def require_str(obj, key, where):
        # build_html and tailor's build_resume_block/get_editable_nodes index
        # these leaves directly, so a missing key or a non-string value raised a
        # native KeyError/AttributeError PAST load_content's guard and 500ed the
        # four render/tailor endpoints. Reject it here so the same ResumeError ->
        # 422 (_resume_content_error) fires that the rest of validation already
        # produces, honoring the "the tailoring buttons say exactly what and
        # where" promise for a hand-edited content.json.
        if key not in obj:
            raise ResumeError(f"{where}: missing {key!r}")
        if not isinstance(obj[key], str):
            raise ResumeError(f"{where}: {key!r} must be text, not {type(obj[key]).__name__}")

    for section in content["sections"]:
        claim(section.get("id"), "section")
        sid = section["id"]
        stype = section.get("type")
        if stype not in SECTION_TYPES:
            raise ResumeError(f"section {sid!r}: unknown type {stype!r}")
        if not section.get("heading"):
            raise ResumeError(f"section {sid!r}: missing heading")
        if not section.get(required_field[stype]):
            raise ResumeError(
                f"section {sid!r}: type {stype!r} needs {required_field[stype]!r}")
        if stype == "paragraph":
            require_str(section, "text", f"section {sid!r}")
        elif stype == "columns":
            cols = section.get("columns", 3)
            try:
                int(cols)
            except (TypeError, ValueError):
                raise ResumeError(f"section {sid!r}: columns must be a number, not {cols!r}") from None
            if not isinstance(section["items"], list):
                raise ResumeError(f"section {sid!r}: items must be a list")
            for item in section["items"]:
                if not isinstance(item, str):
                    raise ResumeError(
                        f"section {sid!r}: every column item must be text, not {type(item).__name__}")
        elif stype == "keyvalue":
            if not isinstance(section["rows"], list):
                raise ResumeError(f"section {sid!r}: rows must be a list")
            for row in section["rows"]:
                if not isinstance(row, dict):
                    raise ResumeError(f"section {sid!r}: each keyvalue row must be an object")
                require_str(row, "label", f"section {sid!r} row")
                require_str(row, "text", f"section {sid!r} row")
        for bullet in section.get("bullets", []):
            claim(bullet.get("id"), f"section {sid!r} bullet")
            require_str(bullet, "text", f"section {sid!r} bullet")
        for role in section.get("roles", []):
            claim(role.get("id"), f"section {sid!r} role")
            if not role.get("title"):
                raise ResumeError(f"role {role['id']!r}: missing title")
            require_str(role, "title", f"role {role['id']!r}")
            dates = role.get("dates")
            if dates is not None and not isinstance(dates, str):
                raise ResumeError(f"role {role['id']!r}: dates must be text")
            for bullet in role.get("bullets", []):
                claim(bullet.get("id"), f"role {role['id']!r} bullet")
                require_str(bullet, "text", f"role {role['id']!r} bullet")


# ---------------------------------------------------------------- HTML

_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"\*(.+?)\*")


def _inline(text: str) -> str:
    """Escape, then expand the minimal markup. Order matters: ** before *."""
    out = html.escape(text, quote=False)
    out = _LINK_RE.sub(r'<a href="\2">\1</a>', out)
    out = _BOLD_RE.sub(r"<strong>\1</strong>", out)
    out = _ITALIC_RE.sub(r"<em>\1</em>", out)
    return out


def _no_widow(text: str) -> str:
    """Join the last two words so a line can never end as a single word
    (text-wrap: pretty alone leaves short standalone words behind)."""
    head, sep, tail = text.rstrip().rpartition(" ")
    return f"{head} {tail}" if sep else text


def _bullets_html(bullets: list) -> str:
    items = "".join(
        f'<li data-id="{html.escape(b["id"])}">{_inline(_no_widow(b["text"]))}</li>'
        for b in bullets)
    return f"<ul>{items}</ul>"


def _section_html(section: dict) -> str:
    stype = section["type"]
    head = f"<h2>{_inline(section['heading'])}</h2>"
    sid = html.escape(section["id"])
    if stype == "paragraph":
        body = f'<p class="body" data-id="{sid}">{_inline(_no_widow(section["text"]))}</p>'
    elif stype == "columns":
        cols = int(section.get("columns", 3))
        cells = "".join(f"<div>{_inline(item)}</div>" for item in section["items"])
        body = f'<div class="cols cols-{cols}">{cells}</div>'
    elif stype == "keyvalue":
        rows = "".join(
            f'<div class="label">{_inline(r["label"])}</div><div>{_inline(r["text"])}</div>'
            for r in section["rows"])
        body = f'<div class="kv">{rows}</div>'
    elif stype == "bullets":
        body = _bullets_html(section["bullets"])
    else:  # roles
        parts = []
        for role in section["roles"]:
            dates = (f'<span class="role-dates">{_inline(role["dates"])}</span>'
                     if role.get("dates") else "")
            parts.append(
                f'<div class="role" data-id="{html.escape(role["id"])}">'
                f'<div class="role-head"><span class="role-title">'
                f'{_inline(role["title"])}</span>{dates}</div>'
                f'{_bullets_html(role.get("bullets", []))}</div>')
        body = "".join(parts)
    return f'<section data-id="{sid}">{head}{body}</section>'


def _header_html(content: dict) -> str:
    """Name/title/contact letterhead — shared with the cover letter (cover.py)."""
    contact = content["contact"]
    bits = [html.escape(contact.get("email", ""))]
    for key in ("phone",):
        if contact.get(key):
            bits.append(html.escape(contact[key]))
    for key in ("linkedin", "website"):
        if contact.get(key):
            label = html.escape(contact[key])
            bits.append(f'<a href="https://{label}">{label}</a>')
    if contact.get("location"):
        bits.append(html.escape(contact["location"]))
    return (
        f'<header><h1 class="name">{_inline(content["name"])}</h1>'
        f'<p class="title">{_inline(content["title"])}</p>'
        f'<p class="contact">{"&nbsp;· ".join(bits)}</p></header>')


def build_html(content: dict, css_href: str | None = None) -> str:
    validate_content(content)
    header = _header_html(content)
    sections = "".join(_section_html(s) for s in content["sections"])

    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    return (template
            .replace("__TITLE__", html.escape(f"{content['name']} — Resume"))
            .replace("__CSS__", css_href or CSS_PATH.as_uri())
            .replace("__BODY__", header + sections))


# ---------------------------------------------------------------- PDF

def render_pdf(html_text: str, out_pdf: Path) -> Path:
    """Write html_text beside out_pdf and print it to PDF with headless Chrome."""
    if CHROME_BIN is not None:
        if not CHROME_BIN.exists():
            raise ResumeError(f"Chrome not found at {CHROME_BIN}")
        chrome = CHROME_BIN
    else:
        chrome = _find_chrome()
    out_pdf = Path(out_pdf)
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    html_path = out_pdf.with_suffix(".html")
    html_path.write_text(html_text, encoding="utf-8")
    try:
        out_pdf.unlink(missing_ok=True)
    except OSError as exc:
        # Windows can't unlink a file someone has open (a PDF viewer, say);
        # surface it as the actionable render error, not a bare 500.
        raise ResumeError(f"can't replace {out_pdf.name} — close it and retry ({exc})")
    # ignore_cleanup_errors: Chrome on Windows can hold profile handles briefly
    # after exit; a successful render must not fail on temp-dir cleanup.
    with tempfile.TemporaryDirectory(
        prefix="hq-resume-chrome-", ignore_cleanup_errors=True
    ) as profile:
        # This Chrome build writes the PDF but never exits in headless mode
        # (verified 2026-06-11), so poll for the file and terminate Chrome
        # ourselves instead of waiting on the process.
        cmd = [
            str(chrome), "--headless", "--disable-gpu",
            # Skip first-run setup and the update check: the updater tries to
            # write inside Chrome.app, which macOS App Management blocks and
            # attributes to our python process ("prevented from modifying apps").
            "--no-first-run", "--disable-component-update",
            f"--user-data-dir={profile}", "--no-pdf-header-footer",
            f"--print-to-pdf={out_pdf}", html_path.as_uri(),
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + CHROME_TIMEOUT
            size = -1
            while time.monotonic() < deadline:
                if proc.poll() is not None and not out_pdf.exists():
                    raise ResumeError(
                        f"Chrome exited ({proc.returncode}) without writing {out_pdf}")
                if out_pdf.exists():
                    new_size = out_pdf.stat().st_size
                    if new_size and new_size == size:
                        return out_pdf  # written and stable across two polls
                    size = new_size
                time.sleep(0.2)
            raise ResumeError(f"Chrome produced no PDF within {CHROME_TIMEOUT}s")
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
