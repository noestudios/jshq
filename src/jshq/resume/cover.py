"""Cover letter rendering: same pattern as the resume, simpler
template. Reuses template.html and the resume letterhead; the letter body is
plain text from the tailoring agent (greeting and sign-off included), split
into paragraphs on blank lines. The template adds only letterhead + date."""

import html

from jshq.resume import render

COVER_CSS_PATH = render.TEMPLATE_DIR / "cover.css"


def build_cover_html(
    content: dict, letter: str, letter_date: str, css_href: str | None = None
) -> str:
    render.validate_content(content)
    paragraphs = [
        p.strip()
        for p in letter.replace("\r\n", "\n").split("\n\n")
        if p.strip()
    ]
    body_parts = []
    for paragraph in paragraphs:
        # single newlines inside a paragraph (e.g. a "Best,\n<name>" sign-off)
        # stay as breaks
        inner = "<br>".join(render._inline(line) for line in paragraph.split("\n"))
        body_parts.append(f"<p>{inner}</p>")
    body = (
        render._header_html(content)
        + f'<p class="letter-date">{html.escape(letter_date)}</p>'
        + f'<div class="letter">{"".join(body_parts)}</div>'
    )
    template = render.TEMPLATE_PATH.read_text(encoding="utf-8")
    return (
        template
        .replace("__TITLE__", html.escape(f"{content['name']} — Cover Letter"))
        .replace("__CSS__", css_href or COVER_CSS_PATH.as_uri())
        .replace("__BODY__", body)
    )
