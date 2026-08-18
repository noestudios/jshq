"""Resume render pipeline (Phase 7d): validation, HTML build, Chrome errors.

Hermetic: fake content only (no personal data), no Chrome launch.
"""

import json

import pytest

from jshq.resume import render


def fake_content():
    return {
        "version": 1,
        "name": "Pat Example",
        "title": "Test Title",
        "contact": {
            "email": "pat@example.com", "phone": "555.0100",
            "linkedin": "linkedin.com/in/pat", "website": "example.com",
            "location": "Testville, IL",
        },
        "sections": [
            {"id": "summary", "heading": "SUMMARY", "type": "paragraph",
             "text": "A summary with a [link](https://example.com) & ampersand."},
            {"id": "skills", "heading": "SKILLS", "type": "columns",
             "columns": 3, "items": ["Alpha & Beta", "Gamma", "Delta"]},
            {"id": "tools", "heading": "TOOLS", "type": "keyvalue",
             "rows": [{"label": "Design", "text": "Tool A, Tool B"}]},
            {"id": "wins", "heading": "WINS", "type": "bullets",
             "bullets": [{"id": "win-1", "text": "Did **bold** things"},
                         {"id": "win-2", "text": "Did *italic* things"}]},
            {"id": "experience", "heading": "EXPERIENCE", "type": "roles",
             "roles": [
                 {"id": "exp-1", "title": "Lead, TestCo",
                  "dates": "Jan 2020 – Dec 2021",
                  "bullets": [{"id": "exp-1-b1", "text": "<scored> 100%"}]},
                 {"id": "exp-2", "title": "Maker, OldCo", "dates": None,
                  "bullets": [{"id": "exp-2-b1", "text": "Made things"}]},
             ]},
        ],
    }


# ---------------------------------------------------------------- validate

def test_validate_accepts_fake_content():
    render.validate_content(fake_content())


@pytest.mark.parametrize("mutate, match", [
    (lambda c: c.pop("name"), "missing 'name'"),
    (lambda c: c.update(version=2), "version"),
    (lambda c: c["sections"][0].update(type="mystery"), "unknown type"),
    (lambda c: c["sections"][0].pop("heading"), "missing heading"),
    (lambda c: c["sections"][0].pop("text"), "needs 'text'"),
    (lambda c: c["sections"][3]["bullets"][1].update(id="win-1"), "duplicate id"),
    (lambda c: c["sections"][4]["roles"][0].pop("title"), "missing title"),
    (lambda c: c["sections"][4]["roles"][0]["bullets"][0].pop("id"), "missing id"),
    # Structurally valid (container present) but a leaf is the wrong type or
    # missing -- these used to pass validate_content and then 500 build_html /
    # the tailor block with a native KeyError/TypeError/ValueError/AttributeError.
    (lambda c: c["sections"][0].update(text=123), "must be text"),          # paragraph text int
    (lambda c: c["sections"][1].update(columns="three"), "columns must"),   # columns not a number
    (lambda c: c["sections"][1].update(columns=None), "columns must"),      # columns explicit null
    (lambda c: c["sections"][1]["items"].__setitem__(0, 5), "item must be text"),  # column item int
    (lambda c: c["sections"][2]["rows"][0].pop("label"), "missing 'label'"),  # keyvalue row no label
    (lambda c: c["sections"][2]["rows"][0].update(text=7), "must be text"),   # keyvalue row text int
    (lambda c: c["sections"][3]["bullets"][0].pop("text"), "missing 'text'"),  # bullet no text
    (lambda c: c["sections"][4]["roles"][0]["bullets"][0].pop("text"), "missing 'text'"),  # role bullet no text
])
def test_validate_rejects_bad_content(mutate, match):
    content = fake_content()
    mutate(content)
    with pytest.raises(render.ResumeError, match=match):
        render.validate_content(content)


def test_load_content_bad_json(tmp_path):
    p = tmp_path / "content.json"
    p.write_text("{nope", encoding="utf-8")
    with pytest.raises(render.ResumeError, match="not valid JSON"):
        render.load_content(p)


def test_load_content_missing_file(tmp_path):
    # Data-dir-relative message, not a raw OSError: this string reaches the
    # user as a toast (Phase 5b — the old text carried an absolute server path).
    with pytest.raises(render.ResumeError, match="missing from your data folder"):
        render.load_content(tmp_path / "absent.json")


def test_shipped_starter_content_validates_and_renders():
    from jshq import paths

    content = json.loads(
        (paths.DEFAULTS_DIR / "resume" / "content.starter.json").read_text(encoding="utf-8")
    )
    render.validate_content(content)
    html_out = render.build_html(content)
    assert "Your Name" in html_out and "you@example.com" in html_out


def test_load_content_roundtrip(tmp_path):
    p = tmp_path / "content.json"
    p.write_text(json.dumps(fake_content()), encoding="utf-8")
    assert render.load_content(p)["name"] == "Pat Example"


# ---------------------------------------------------------------- build_html

def test_build_html_sections_in_order_with_ids():
    html = render.build_html(fake_content())
    order = [html.index(f'data-id="{sid}"')
             for sid in ("summary", "skills", "tools", "wins", "experience")]
    assert order == sorted(order)
    for node in ("win-1", "win-2", "exp-1", "exp-1-b1", "exp-2"):
        assert f'data-id="{node}"' in html


def test_build_html_escapes_content_text():
    html = render.build_html(fake_content())
    assert "<scored>" not in html
    assert "&lt;scored&gt; 100%" in html
    assert "Alpha &amp; Beta" in html


def test_no_widow_joins_last_two_words():
    # bullets and body paragraphs end with a non-breaking join, so a line
    # can never break before a lone last word (7d fidelity pass)
    html = render.build_html(fake_content())
    assert "Made things" in html
    assert "ampersand." in html  # summary text present…
    assert "&amp; ampersand." in html  # …with its last space joined


def test_build_html_inline_markup():
    html = render.build_html(fake_content())
    assert '<a href="https://example.com">link</a>' in html
    assert "<strong>bold</strong>" in html
    assert "<em>italic</em>" in html


def test_build_html_header_and_contact_links():
    html = render.build_html(fake_content())
    assert '<h1 class="name">Pat Example</h1>' in html
    assert '<a href="https://linkedin.com/in/pat">linkedin.com/in/pat</a>' in html
    assert '<a href="https://example.com">example.com</a>' in html
    assert "pat@example.com" in html  # plain text, not a link
    # divider bullets are symmetrically spaced (one space each side), not the old
    # nbsp-before / nbsp+space-after that floated the bullet rightward.
    assert "&nbsp;· " in html
    assert "&nbsp;·&nbsp;" not in html


def test_build_html_role_dates_optional():
    html = render.build_html(fake_content())
    assert "Jan 2020 – Dec 2021" in html
    # exp-2 has dates=None: its role-head must not render an empty dates span
    exp2 = html.split('data-id="exp-2"')[1]
    assert "role-dates" not in exp2.split("</div>")[0]


def test_build_html_validates_first():
    bad = fake_content()
    bad.pop("sections")
    with pytest.raises(render.ResumeError):
        render.build_html(bad)


# ---------------------------------------------------------------- render_pdf

def test_render_pdf_chrome_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(render, "CHROME_BIN", tmp_path / "no-chrome")
    with pytest.raises(render.ResumeError, match="Chrome not found"):
        render.render_pdf("<html></html>", tmp_path / "out.pdf")


def test_render_pdf_locked_target_is_resume_error(tmp_path, monkeypatch):
    """Windows refuses to unlink an open file — the pre-render replace must
    surface as the actionable ResumeError, not a bare 500."""
    import pathlib

    monkeypatch.setattr(render, "CHROME_BIN", tmp_path)  # exists (dir)
    monkeypatch.setattr(
        pathlib.Path, "unlink",
        lambda self, missing_ok=False: (_ for _ in ()).throw(PermissionError("in use")),
    )
    with pytest.raises(render.ResumeError, match="close it and retry"):
        render.render_pdf("<html></html>", tmp_path / "out.pdf")


def test_render_pdf_chrome_exits_without_pdf(tmp_path, monkeypatch):
    class FakeProc:
        returncode = 1
        def poll(self): return 1
        def terminate(self): pass
        def wait(self, timeout=None): pass

    monkeypatch.setattr(render, "CHROME_BIN", tmp_path)  # exists (dir)
    monkeypatch.setattr(render.subprocess, "Popen", lambda *a, **k: FakeProc())
    with pytest.raises(render.ResumeError, match="without writing"):
        render.render_pdf("<html></html>", tmp_path / "out.pdf")
    # the html companion is still written beside the target
    assert (tmp_path / "out.html").exists()
