"""Tailoring endpoints (Phase 7e): generate, patch, apply, discard, files,
delete cascades. Hermetic: fake Anthropic client, fake content.json in tmp,
render_pdf stubbed (no Chrome), APPLICATIONS_DIR in tmp."""

import json

import pytest
from test_compose import fake_client
from test_resume_render import fake_content

import jshq.main as main_module
from jshq import compose, tailor
from jshq.main import app, get_compose_client
from jshq.resume import render
from jshq.scoring.criteria import persona_display_name

EM_DASH = "—"


def test_tailoring_prompts_and_voice_guide_have_no_em_dashes():
    """The voice guide bans em dashes; neither the guide nor the assembled
    prompts may model them, or the model imitates the punctuation it's bathed in
    (the leak this guards against). en dashes in ranges like 250-350 are fine."""
    vg = compose.load_voice_guide()
    assert EM_DASH not in vg, "voice_guide.md must not use em dashes (it bans them)"
    assert EM_DASH not in tailor.build_system_prompt(vg)
    assert EM_DASH not in tailor.build_chat_system_prompt(vg)
    # the constraint is also stated explicitly for the rewrites + the letter
    assert "no em dashes" in tailor.build_system_prompt(vg).lower()


def test_tailoring_prompts_hardcode_no_person():
    """Whoever the prompts are written for comes from the criteria doc's persona
    block, never from the code (no personal data in the repo)."""
    vg = compose.load_voice_guide()
    for system in (tailor.build_system_prompt(vg), tailor.build_chat_system_prompt(vg)):
        assert "Chris" not in system
        assert persona_display_name() in system


def test_tailoring_prompt_enforces_two_page_length():
    """Rewrites must not lengthen the resume — guards the fix for the +31% bloat
    that pushed a tailored resume to 3 pages."""
    low = tailor.build_system_prompt(compose.load_voice_guide()).lower()
    assert "no longer than" in low
    assert "two pages" in low


def tailor_text(changes=None, **overrides):
    data = {
        "analysis": "They want a systems-minded design leader.",
        "changes": changes if changes is not None else [
            {"id": "summary", "new": "A tailored summary.", "rationale": "Lead with it."},
            {"id": "win-1", "new": "Did **bold** tailored things", "rationale": "Echo JD."},
        ],
        "cover_letter": "Dear team,\n\nI am excited.\n\nBest,\nPat",
    }
    data.update(overrides)
    return json.dumps(data)


TWO_PAGE_PDF = b"%PDF-1.4\n/Type /Pages\n/Type /Page\n/Type /Page\n%%EOF"


@pytest.fixture
def content_path(tmp_path, monkeypatch):
    path = tmp_path / "content.json"
    path.write_text(json.dumps(fake_content()), encoding="utf-8")
    monkeypatch.setattr(render, "CONTENT_PATH", path)
    return path


@pytest.fixture
def apps_dir(tmp_path, monkeypatch):
    directory = tmp_path / "applications"
    monkeypatch.setattr(main_module, "APPLICATIONS_DIR", directory)
    return directory


@pytest.fixture
def stub_render(monkeypatch):
    """No Chrome in tests; writes a recognizable two-page PDF."""
    calls = []

    def fake_render_pdf(html_text, out_pdf, pdf_bytes=TWO_PAGE_PDF):
        out_pdf.parent.mkdir(parents=True, exist_ok=True)
        out_pdf.write_bytes(pdf_bytes)
        calls.append(out_pdf)
        return out_pdf

    monkeypatch.setattr(render, "render_pdf", fake_render_pdf)
    return calls


@pytest.fixture
def tailor_client(client, content_path, apps_dir, stub_render):
    fake, state = fake_client(tailor_text())
    app.dependency_overrides[get_compose_client] = lambda: fake
    return client, state


@pytest.fixture
def seed_tailorable(seed_job, seed_application):
    """An application whose job carries a JD + fit fields."""

    def _seed(**job_overrides):
        job_id = seed_job(
            description_text="We need a design leader.",
            fit_score=82,
            fit_quadrant="core",
            scoring_notes="Strong fit.",
            **job_overrides,
        )
        return seed_application(job_id=job_id), job_id

    return _seed


def make_tailoring(client, application_id):
    response = client.post(f"/api/applications/{application_id}/tailor", json={})
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------- generate


def test_tailor_creates_pending_with_server_filled_old(tailor_client, seed_tailorable, db):
    client, state = tailor_client
    application_id, _ = seed_tailorable()
    body = make_tailoring(client, application_id)
    assert body["status"] == "pending"
    assert body["model"] == tailor.MODEL
    assert body["warnings"] == []
    assert [c["id"] for c in body["change_plan"]] == ["summary", "win-1"]
    assert body["change_plan"][0]["old"].startswith("A summary")
    assert all(c["approved"] is False for c in body["change_plan"])
    # prompt carried the JD, the fit notes, and the editable-id list
    user = state["kwargs"]["messages"][0]["content"]
    assert "We need a design leader." in user
    assert "Strong fit." in user
    assert "EDITABLE IDS: summary," in user
    # no activity at generate time — it lands at apply
    rows = db.execute("SELECT * FROM activities WHERE entity_type = 'application'").fetchall()
    assert rows == []


def test_tailor_second_pending_is_409(tailor_client, seed_tailorable):
    client, _ = tailor_client
    application_id, _ = seed_tailorable()
    first = make_tailoring(client, application_id)
    response = client.post(f"/api/applications/{application_id}/tailor", json={})
    assert response.status_code == 409
    assert str(first["id"]) in response.json()["detail"]


def test_tailor_requires_a_jd(tailor_client, seed_application):
    client, _ = tailor_client
    application_id = seed_application()  # seed job has no description_text
    response = client.post(f"/api/applications/{application_id}/tailor", json={})
    assert response.status_code == 409
    assert "description" in response.json()["detail"]


def test_tailor_missing_application_404(tailor_client):
    client, _ = tailor_client
    assert client.post("/api/applications/999/tailor", json={}).status_code == 404


def test_tailor_without_api_key_503(client, seed_tailorable, content_path):
    application_id, _ = seed_tailorable()
    response = client.post(f"/api/applications/{application_id}/tailor", json={})
    assert response.status_code == 503


def test_tailor_unusable_model_output_502_after_retry(
    client, seed_tailorable, content_path, apps_dir
):
    fake, state = fake_client("never json")
    app.dependency_overrides[get_compose_client] = lambda: fake
    application_id, _ = seed_tailorable()
    response = client.post(f"/api/applications/{application_id}/tailor", json={})
    assert response.status_code == 502
    assert state["calls"] == 2


def test_tailor_failure_still_records_usage(
    client, seed_tailorable, content_path, apps_dir, db
):
    """A failed generate (unparseable output — e.g. sonnet-5 spending its whole
    budget on thinking) must still bill the tokens it burned, or the ledger
    silently under-reports a runaway. Both retry attempts get recorded."""
    from types import SimpleNamespace

    from jshq import usage

    burned = SimpleNamespace(
        input_tokens=100, output_tokens=8192,
        cache_read_input_tokens=0, cache_creation_input_tokens=0,
    )
    calls = {"n": 0}

    async def create(**kwargs):
        calls["n"] += 1
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="never json")], usage=burned
        )

    fake = SimpleNamespace(messages=SimpleNamespace(create=create))
    app.dependency_overrides[get_compose_client] = lambda: fake
    application_id, _ = seed_tailorable()

    response = client.post(f"/api/applications/{application_id}/tailor", json={})
    assert response.status_code == 502
    assert calls["n"] == 2  # both attempts ran

    by_model = usage.read_usage_totals(db)["by_model"][tailor.MODEL]
    assert by_model["output"] == 8192 * 2  # both failed attempts recorded
    assert by_model["cost"] > 0


def test_tailor_surfaces_warnings_for_dropped_changes(
    client, seed_tailorable, content_path, apps_dir
):
    fake, _ = fake_client(tailor_text(changes=[
        {"id": "bogus", "new": "x"},
        {"id": "summary", "new": "A tailored summary."},
    ]))
    app.dependency_overrides[get_compose_client] = lambda: fake
    application_id, _ = seed_tailorable()
    body = make_tailoring(client, application_id)
    assert len(body["change_plan"]) == 1
    assert any("bogus" in w for w in body["warnings"])


# ---------------------------------------------------------------- GET / PATCH


def test_get_tailoring_prefers_pending_else_latest_applied(tailor_client, seed_tailorable):
    client, _ = tailor_client
    application_id, _ = seed_tailorable()
    assert client.get(f"/api/applications/{application_id}/tailoring").status_code == 404
    t1 = make_tailoring(client, application_id)
    assert client.post(f"/api/tailorings/{t1['id']}/apply").status_code == 200
    body = client.get(f"/api/applications/{application_id}/tailoring").json()
    assert body["id"] == t1["id"] and body["status"] == "applied"
    t2 = make_tailoring(client, application_id)
    body = client.get(f"/api/applications/{application_id}/tailoring").json()
    assert body["id"] == t2["id"] and body["status"] == "pending"


def test_patch_toggles_approval_and_edits_letter(tailor_client, seed_tailorable):
    client, _ = tailor_client
    application_id, _ = seed_tailorable()
    t = make_tailoring(client, application_id)
    response = client.patch(f"/api/tailorings/{t['id']}", json={
        "changes": [{"id": "summary", "approved": True}],
        "cover_letter": "Rewritten letter.",
    })
    assert response.status_code == 200
    body = response.json()
    approvals = {c["id"]: c["approved"] for c in body["change_plan"]}
    assert approvals == {"summary": True, "win-1": False}
    assert body["cover_letter"] == "Rewritten letter."


def test_patch_accepts_new_text_edit(tailor_client, seed_tailorable):
    client, _ = tailor_client
    application_id, _ = seed_tailorable()
    t = make_tailoring(client, application_id)
    body = client.patch(f"/api/tailorings/{t['id']}", json={
        "changes": [{"id": "win-1", "approved": True, "new": "Hand-edited rewrite"}],
    }).json()
    win = next(c for c in body["change_plan"] if c["id"] == "win-1")
    assert win["new"] == "Hand-edited rewrite" and win["approved"] is True


def test_patch_unknown_change_id_400_and_empty_422(tailor_client, seed_tailorable):
    client, _ = tailor_client
    application_id, _ = seed_tailorable()
    t = make_tailoring(client, application_id)
    response = client.patch(f"/api/tailorings/{t['id']}", json={
        "changes": [{"id": "nope", "approved": True}],
    })
    assert response.status_code == 400
    assert client.patch(f"/api/tailorings/{t['id']}", json={}).status_code == 422


def test_patch_after_apply_409(tailor_client, seed_tailorable):
    client, _ = tailor_client
    application_id, _ = seed_tailorable()
    t = make_tailoring(client, application_id)
    client.post(f"/api/tailorings/{t['id']}/apply")
    response = client.patch(f"/api/tailorings/{t['id']}", json={"cover_letter": "x"})
    assert response.status_code == 409


# ---------------------------------------------------------------- apply


def test_apply_renders_stamps_and_logs(tailor_client, seed_tailorable, apps_dir, db):
    client, _ = tailor_client
    application_id, _ = seed_tailorable()
    t = make_tailoring(client, application_id)
    client.patch(f"/api/tailorings/{t['id']}", json={
        "changes": [{"id": "summary", "approved": True}],
    })
    response = client.post(f"/api/tailorings/{t['id']}/apply")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["warnings"] == []  # stub writes a two-page PDF
    assert body["files"] == ["resume-v1.pdf", "cover-v1.pdf"]
    for name in body["files"]:
        assert (apps_dir / str(application_id) / name).is_file()
    assert body["tailoring"]["status"] == "applied"
    assert body["tailoring"]["version"] == 1
    assert body["tailoring"]["applied_at"]
    assert body["application"]["resume_version"] == "v1"
    assert body["application"]["cover_note"].startswith("Dear team")
    activity = db.execute(
        "SELECT * FROM activities WHERE entity_type = 'application' AND entity_id = ?",
        (application_id,),
    ).fetchone()
    content = json.loads(activity["content"])
    assert activity["type"] == "compose"
    assert content["intent"] == "tailoring"
    assert content["draft"] == "v1 — 1 resume change + cover letter"
    assert content["version"] == 1


def test_apply_uses_the_approved_rewrite_in_the_resume_html(tailor_client, seed_tailorable):
    client, _ = tailor_client
    application_id, _ = seed_tailorable()
    t = make_tailoring(client, application_id)
    client.patch(f"/api/tailorings/{t['id']}", json={
        "changes": [{"id": "summary", "approved": True}],
    })
    client.post(f"/api/tailorings/{t['id']}/apply")
    # the stored plan is the regeneration recipe — re-apply it to
    # the same content and confirm what the rendered HTML carried
    plan = client.get(f"/api/applications/{application_id}/tailoring").json()["change_plan"]
    html = render.build_html(tailor.apply_changes(fake_content(), plan))
    # render joins each line's last two words with nbsp (_no_widow)
    assert "A tailored\xa0summary." in html
    assert "Did <strong>bold</strong>\xa0things" in html  # win-1 stayed unapproved


def test_apply_with_zero_approved_is_cover_letter_only(tailor_client, seed_tailorable, db):
    client, _ = tailor_client
    application_id, _ = seed_tailorable()
    t = make_tailoring(client, application_id)
    response = client.post(f"/api/tailorings/{t['id']}/apply")
    assert response.status_code == 200
    assert response.json()["application"]["cover_note"].startswith("Dear team")
    activity = db.execute(
        "SELECT content FROM activities WHERE entity_type = 'application' AND entity_id = ?",
        (application_id,),
    ).fetchone()
    assert "0 resume changes" in json.loads(activity["content"])["draft"]


def test_apply_versions_increment_per_application(tailor_client, seed_tailorable, apps_dir):
    client, _ = tailor_client
    application_id, _ = seed_tailorable()
    t1 = make_tailoring(client, application_id)
    client.post(f"/api/tailorings/{t1['id']}/apply")
    t2 = make_tailoring(client, application_id)
    body = client.post(f"/api/tailorings/{t2['id']}/apply").json()
    assert body["tailoring"]["version"] == 2
    assert body["application"]["resume_version"] == "v2"
    assert (apps_dir / str(application_id) / "resume-v2.pdf").is_file()
    assert (apps_dir / str(application_id) / "resume-v1.pdf").is_file()  # v1 kept


def test_apply_drift_409(tailor_client, seed_tailorable, content_path):
    client, _ = tailor_client
    application_id, _ = seed_tailorable()
    t = make_tailoring(client, application_id)
    client.patch(f"/api/tailorings/{t['id']}", json={
        "changes": [{"id": "summary", "approved": True}],
    })
    drifted = fake_content()
    drifted["sections"][0]["text"] = "Master edited after generation."
    content_path.write_text(json.dumps(drifted), encoding="utf-8")
    response = client.post(f"/api/tailorings/{t['id']}/apply")
    assert response.status_code == 409
    assert "regenerate" in response.json()["detail"]
    # still pending — nothing was stamped
    body = client.get(f"/api/applications/{application_id}/tailoring").json()
    assert body["status"] == "pending"


# ---------------------------------------------------------------- rerender cover


def test_rerender_makes_new_cover_version_resume_untouched(
    tailor_client, stub_render, seed_tailorable, apps_dir, db
):
    client, _ = tailor_client
    application_id, _ = seed_tailorable()
    t = make_tailoring(client, application_id)
    client.post(f"/api/tailorings/{t['id']}/apply")  # v1 (resume + cover)
    renders_after_apply = len(stub_render)
    edited = "Dear team,\n\nI revised this letter by hand.\n\nBest,\nPat"
    response = client.post(
        f"/api/tailorings/{t['id']}/rerender", json={"cover_letter": edited}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tailoring"]["status"] == "applied"
    assert body["tailoring"]["version"] == 2  # cover advances
    assert body["tailoring"]["cover_letter"] == edited
    # The resume keeps its own version — its PDF is neither re-rendered nor copied.
    assert body["files"] == ["resume-v1.pdf", "cover-v2.pdf"]
    assert body["application"]["resume_version"] == "v1"  # NOT bumped
    assert body["application"]["cover_note"] == edited
    app_dir = apps_dir / str(application_id)
    assert (app_dir / "cover-v2.pdf").is_file()
    assert (app_dir / "resume-v1.pdf").is_file()
    assert not (app_dir / "resume-v2.pdf").exists()  # no phantom resume version
    # Only the cover hit render_pdf — the resume was left entirely alone.
    assert len(stub_render) == renders_after_apply + 1
    assert stub_render[-1].name == "cover-v2.pdf"
    # v1 is kept as history; GET returns the latest (v2).
    applied = db.execute(
        "SELECT version FROM tailorings WHERE application_id = ? AND status = 'applied' "
        "ORDER BY version",
        (application_id,),
    ).fetchall()
    assert [r["version"] for r in applied] == [1, 2]
    assert client.get(f"/api/applications/{application_id}/tailoring").json()["version"] == 2
    activity = db.execute(
        "SELECT content FROM activities WHERE entity_type = 'application' AND entity_id = ? "
        "ORDER BY id DESC",
        (application_id,),
    ).fetchone()
    payload = json.loads(activity["content"])
    assert payload["draft"] == "cover v2 — re-rendered cover letter (manual edit)"
    assert payload["files"] == ["resume-v1.pdf", "cover-v2.pdf"]


def test_rerender_unchanged_letter_400(tailor_client, seed_tailorable):
    client, _ = tailor_client
    application_id, _ = seed_tailorable()
    t = make_tailoring(client, application_id)
    client.post(f"/api/tailorings/{t['id']}/apply")
    same = client.get(f"/api/applications/{application_id}/tailoring").json()["cover_letter"]
    response = client.post(f"/api/tailorings/{t['id']}/rerender", json={"cover_letter": same})
    assert response.status_code == 400
    assert "no changes" in response.json()["detail"]


def test_rerender_pending_is_409(tailor_client, seed_tailorable):
    client, _ = tailor_client
    application_id, _ = seed_tailorable()
    t = make_tailoring(client, application_id)  # pending, never applied
    response = client.post(
        f"/api/tailorings/{t['id']}/rerender",
        json={"cover_letter": "Dear team,\n\nBrand new.\n\nBest,\nPat"},
    )
    assert response.status_code == 409
    assert "Apply" in response.json()["detail"]


def test_rerender_empty_letter_422(tailor_client, seed_tailorable):
    client, _ = tailor_client
    application_id, _ = seed_tailorable()
    t = make_tailoring(client, application_id)
    client.post(f"/api/tailorings/{t['id']}/apply")
    response = client.post(f"/api/tailorings/{t['id']}/rerender", json={"cover_letter": "   "})
    assert response.status_code == 422


def test_rerender_missing_tailoring_404(tailor_client):
    client, _ = tailor_client
    response = client.post(
        "/api/tailorings/9999/rerender",
        json={"cover_letter": "Dear team,\n\nHi.\n\nBest,\nPat"},
    )
    assert response.status_code == 404


def test_apply_warns_when_resume_is_not_two_pages(
    client, seed_tailorable, content_path, apps_dir, monkeypatch
):
    fake, _ = fake_client(tailor_text())
    app.dependency_overrides[get_compose_client] = lambda: fake

    def three_page_render(html_text, out_pdf):
        out_pdf.parent.mkdir(parents=True, exist_ok=True)
        out_pdf.write_bytes(b"%PDF\n/Type /Page\n/Type /Page\n/Type /Page\n")
        return out_pdf

    monkeypatch.setattr(render, "render_pdf", three_page_render)
    application_id, _ = seed_tailorable()
    t = make_tailoring(client, application_id)
    body = client.post(f"/api/tailorings/{t['id']}/apply").json()
    assert any("3 pages" in w for w in body["warnings"])


def test_apply_render_failure_leaves_no_db_change(
    client, seed_tailorable, content_path, apps_dir, monkeypatch, db
):
    fake, _ = fake_client(tailor_text())
    app.dependency_overrides[get_compose_client] = lambda: fake

    def broken_render(html_text, out_pdf):
        raise render.ResumeError("chrome went missing")

    monkeypatch.setattr(render, "render_pdf", broken_render)
    application_id, _ = seed_tailorable()
    t = make_tailoring(client, application_id)
    response = client.post(f"/api/tailorings/{t['id']}/apply")
    assert response.status_code == 502
    row = db.execute("SELECT status, version FROM tailorings WHERE id = ?", (t["id"],)).fetchone()
    assert row["status"] == "pending" and row["version"] is None
    application = db.execute(
        "SELECT resume_version FROM applications WHERE id = ?", (application_id,)
    ).fetchone()
    assert application["resume_version"] is None


# ---------------------------------------------------------------- discard


def test_discard_frees_the_pending_slot(tailor_client, seed_tailorable):
    client, _ = tailor_client
    application_id, _ = seed_tailorable()
    t = make_tailoring(client, application_id)
    body = client.post(f"/api/tailorings/{t['id']}/discard").json()
    assert body["status"] == "discarded"
    # discarded rows never come back through GET
    assert client.get(f"/api/applications/{application_id}/tailoring").status_code == 404
    # and a fresh pending can be created (partial unique index)
    make_tailoring(client, application_id)
    # double-discard / discard-after-apply guard
    assert client.post(f"/api/tailorings/{t['id']}/discard").status_code == 409


# ---------------------------------------------------------------- files


def test_files_route_serves_pdfs_and_rejects_other_names(
    tailor_client, seed_tailorable, apps_dir
):
    client, _ = tailor_client
    application_id, _ = seed_tailorable()
    t = make_tailoring(client, application_id)
    client.post(f"/api/tailorings/{t['id']}/apply")
    ok = client.get(f"/api/applications/{application_id}/files/resume-v1.pdf")
    assert ok.status_code == 200
    assert ok.headers["content-type"] == "application/pdf"
    assert ok.content == TWO_PAGE_PDF
    for bad in ("resume-v1.html", "evil.pdf", "resume-v1.pdf.bak", "..%2Fcontent.json"):
        assert client.get(
            f"/api/applications/{application_id}/files/{bad}"
        ).status_code == 404
    assert client.get("/api/applications/999/files/resume-v1.pdf").status_code == 404


# ---------------------------------------------------------------- cascades


def test_application_delete_removes_tailorings_and_files(
    tailor_client, seed_tailorable, apps_dir, db
):
    client, _ = tailor_client
    application_id, _ = seed_tailorable()
    t = make_tailoring(client, application_id)
    client.post(f"/api/tailorings/{t['id']}/apply")
    assert (apps_dir / str(application_id)).is_dir()
    assert client.delete(f"/api/applications/{application_id}").status_code == 200
    assert db.execute("SELECT COUNT(*) AS n FROM tailorings").fetchone()["n"] == 0
    assert not (apps_dir / str(application_id)).exists()


def test_company_delete_cascades_tailorings(client, apps_dir, db, seed_company, seed_job, seed_application):
    company_id = seed_company()
    job_id = seed_job(company_id=company_id, description_text="JD")
    application_id = seed_application(job_id=job_id)
    cursor = db.execute(
        "INSERT INTO tailorings (application_id, change_plan, cover_letter) VALUES (?, '[]', 'x')",
        (application_id,),
    )
    db.execute(
        "INSERT INTO tailoring_messages (tailoring_id, role, content) VALUES (?, 'user', 'hi')",
        (cursor.lastrowid,),
    )
    db.commit()
    assert client.delete(f"/api/companies/{company_id}").status_code == 200
    assert db.execute("SELECT COUNT(*) AS n FROM tailorings").fetchone()["n"] == 0
    assert db.execute("SELECT COUNT(*) AS n FROM tailoring_messages").fetchone()["n"] == 0


# ---------------------------------------------------------------- chat (7f)


def chat_text(**overrides):
    data = {
        "reply": "Done — updated.",
        "changes": [],
        "remove": [],
        "cover_letter": None,
    }
    data.update(overrides)
    return json.dumps(data)


def fake_client_seq(texts):
    """Like fake_client, but successive replies; kwargs kept per call."""
    from types import SimpleNamespace

    state = {"calls": 0, "all_kwargs": []}
    replies = iter(texts)

    async def create(**kwargs):
        state["calls"] += 1
        state["all_kwargs"].append(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=next(replies))])

    return SimpleNamespace(messages=SimpleNamespace(create=create)), state


@pytest.fixture
def chat_ready(client, content_path, apps_dir, stub_render, seed_tailorable):
    """A pending tailoring, then the client swapped to a chat-reply queue."""

    def _seed(*chat_replies):
        fake, _ = fake_client(tailor_text())
        app.dependency_overrides[get_compose_client] = lambda: fake
        application_id, _ = seed_tailorable()
        t = make_tailoring(client, application_id)
        fake_chat, state = fake_client_seq(list(chat_replies))
        app.dependency_overrides[get_compose_client] = lambda: fake_chat
        return application_id, t, state

    return _seed


def send_chat(client, tailoring_id, message="make it pop"):
    response = client.post(f"/api/tailorings/{tailoring_id}/chat", json={"message": message})
    assert response.status_code == 200, response.text
    return response.json()


def test_chat_revises_change_and_keeps_approved_flag(client, chat_ready):
    application_id, t, _ = chat_ready(chat_text(changes=[
        {"id": "win-1", "new": "Softer rewrite", "rationale": "Less salesy."},
    ]))
    client.patch(f"/api/tailorings/{t['id']}", json={
        "changes": [{"id": "win-1", "approved": True}],
    })
    body = send_chat(client, t["id"], "make bullet win-1 less salesy")
    assert body["warnings"] == []
    assert body["message"]["role"] == "assistant"
    assert body["message"]["content"] == "Done — updated."
    win = next(c for c in body["tailoring"]["change_plan"] if c["id"] == "win-1")
    assert win["new"] == "Softer rewrite"
    assert win["approved"] is True
    assert body["tailoring"]["cover_letter"].startswith("Dear team")  # untouched


def test_chat_adds_change_unapproved(client, chat_ready):
    _, t, _ = chat_ready(chat_text(changes=[
        {"id": "win-2", "new": "Also tightened", "rationale": "Pat asked."},
    ]))
    body = send_chat(client, t["id"], "also tighten win-2")
    plan = body["tailoring"]["change_plan"]
    assert [c["id"] for c in plan] == ["summary", "win-1", "win-2"]
    added = plan[2]
    assert added["approved"] is False
    assert added["old"] == "Did *italic* things"  # server-filled


def test_chat_removes_change(client, chat_ready):
    _, t, _ = chat_ready(chat_text(remove=["summary"]))
    body = send_chat(client, t["id"], "leave the summary alone")
    assert [c["id"] for c in body["tailoring"]["change_plan"]] == ["win-1"]


def test_chat_updates_cover_letter_and_null_leaves_it(client, chat_ready):
    _, t, _ = chat_ready(
        chat_text(cover_letter="Dear team,\n\nShorter.\n\nBest,\nPat"),
        chat_text(reply="Just a thought."),
    )
    body = send_chat(client, t["id"], "shorten the letter")
    assert body["tailoring"]["cover_letter"].startswith("Dear team,\n\nShorter.")
    body = send_chat(client, t["id"], "what do you think of the tone?")
    assert body["tailoring"]["cover_letter"].startswith("Dear team,\n\nShorter.")


def test_chat_discussion_only_changes_nothing_but_messages(client, chat_ready, db):
    _, t, _ = chat_ready(chat_text(reply="The plan already leads with that."))
    before = client.get(f"/api/tailorings/{t['id']}/messages").json()
    assert before == []
    body = send_chat(client, t["id"], "why no change to the summary?")
    assert body["tailoring"]["change_plan"] == t["change_plan"]
    assert body["tailoring"]["cover_letter"] == t["cover_letter"]
    rows = client.get(f"/api/tailorings/{t['id']}/messages").json()
    assert [(r["role"], r["content"]) for r in rows] == [
        ("user", "why no change to the summary?"),
        ("assistant", "The plan already leads with that."),
    ]
    assert all("payload" not in r for r in rows)  # audit JSON stays in the DB
    stored = db.execute(
        "SELECT payload FROM tailoring_messages WHERE role = 'assistant'"
    ).fetchone()
    assert json.loads(stored["payload"])["reply"] == "The plan already leads with that."


def test_chat_context_reflects_manual_edits(client, chat_ready):
    _, t, state = chat_ready(chat_text())
    client.patch(f"/api/tailorings/{t['id']}", json={
        "changes": [{"id": "summary", "approved": True, "new": "Hand-edited summary"}],
        "cover_letter": "Dear team, hand-edited letter.",
    })
    send_chat(client, t["id"])
    final_turn = state["all_kwargs"][0]["messages"][-1]["content"]
    assert "[summary] (approved) -> Hand-edited summary" in final_turn
    assert "Dear team, hand-edited letter." in final_turn


def test_chat_second_turn_replays_compact_history(client, chat_ready):
    _, t, state = chat_ready(chat_text(reply="First reply."), chat_text(reply="Second."))
    send_chat(client, t["id"], "first ask")
    send_chat(client, t["id"], "second ask")
    messages = state["all_kwargs"][1]["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert messages[0]["content"] == "first ask"  # raw text, no JD/context
    assert "--- JOB ---" not in messages[0]["content"]
    assert messages[1]["content"] == "First reply."
    assert "--- JOB ---" in messages[2]["content"]  # context rides the new turn
    assert f"Message from {persona_display_name()}: second ask" in messages[2]["content"]


def test_chat_non_pending_409_and_missing_404(client, chat_ready):
    _, t, _ = chat_ready()
    client.post(f"/api/tailorings/{t['id']}/apply")
    response = client.post(f"/api/tailorings/{t['id']}/chat", json={"message": "hi"})
    assert response.status_code == 409
    assert client.post("/api/tailorings/999/chat", json={"message": "hi"}).status_code == 404


def test_chat_empty_message_422(client, chat_ready):
    _, t, _ = chat_ready()
    assert client.post(
        f"/api/tailorings/{t['id']}/chat", json={"message": "   "}
    ).status_code == 422


def test_chat_without_api_key_503(client, content_path, db, seed_tailorable):
    application_id, _ = seed_tailorable()
    cursor = db.execute(
        "INSERT INTO tailorings (application_id, change_plan, cover_letter) VALUES (?, '[]', 'x')",
        (application_id,),
    )
    db.commit()
    response = client.post(f"/api/tailorings/{cursor.lastrowid}/chat", json={"message": "hi"})
    assert response.status_code == 503


def test_chat_unusable_output_502_persists_nothing(client, chat_ready, db):
    _, t, state = chat_ready("never json", "still not json")
    response = client.post(f"/api/tailorings/{t['id']}/chat", json={"message": "hi"})
    assert response.status_code == 502
    assert state["calls"] == 2  # one corrective retry
    assert db.execute("SELECT COUNT(*) AS n FROM tailoring_messages").fetchone()["n"] == 0
    row = db.execute("SELECT change_plan, cover_letter FROM tailorings WHERE id = ?",
                     (t["id"],)).fetchone()
    assert json.loads(row["change_plan"]) == t["change_plan"]
    assert row["cover_letter"] == t["cover_letter"]


def test_chat_no_activity_and_touches_updated_at(client, chat_ready, db):
    _, t, _ = chat_ready(chat_text())
    db.execute(
        "UPDATE tailorings SET updated_at = '2000-01-01 00:00:00' WHERE id = ?", (t["id"],)
    )
    db.commit()
    send_chat(client, t["id"])
    assert db.execute("SELECT COUNT(*) AS n FROM activities").fetchone()["n"] == 0
    row = db.execute("SELECT updated_at FROM tailorings WHERE id = ?", (t["id"],)).fetchone()
    assert row["updated_at"] != "2000-01-01 00:00:00"


def test_get_messages_missing_tailoring_404(client, db):
    assert client.get("/api/tailorings/999/messages").status_code == 404


def test_application_delete_cascades_chat_messages(client, chat_ready, db):
    application_id, t, _ = chat_ready(chat_text())
    send_chat(client, t["id"])
    assert db.execute("SELECT COUNT(*) AS n FROM tailoring_messages").fetchone()["n"] == 2
    assert client.delete(f"/api/applications/{application_id}").status_code == 200
    assert db.execute("SELECT COUNT(*) AS n FROM tailoring_messages").fetchone()["n"] == 0


# ---------------------------------------------------------------- AI-tell hygiene


def test_ai_tells_integrated_into_tailor_prompts():
    vg = compose.load_voice_guide()
    tells = compose.ai_tells_prompt_block()
    for build in (tailor.build_system_prompt, tailor.build_chat_system_prompt):
        prompt = build(vg, tells)
        assert "AI-TELL RUBRIC" in prompt
        assert EM_DASH not in prompt  # injecting the rubric keeps the prompt clean


def test_tailor_generate_sweeps_em_dashes(client, content_path, seed_tailorable):
    fake, _ = fake_client(tailor_text(
        cover_letter="Dear team,\n\nI led the rebuild — and shipped it.\n\nBest,\nPat",
        changes=[{"id": "summary", "new": "Design leader — systems-minded.", "rationale": "x"}],
    ))
    app.dependency_overrides[get_compose_client] = lambda: fake
    application_id, _ = seed_tailorable()
    body = make_tailoring(client, application_id)
    assert EM_DASH not in body["cover_letter"]
    assert body["cover_letter"] == "Dear team,\n\nI led the rebuild, and shipped it.\n\nBest,\nPat"
    assert body["change_plan"]  # the summary rewrite survived
    assert all(EM_DASH not in c["new"] for c in body["change_plan"])


def test_chat_sweeps_em_dashes_from_letter(client, chat_ready):
    _, t, _ = chat_ready(chat_text(
        cover_letter="Dear team,\n\nRewritten — cleaner now.\n\nBest,\nPat",
    ))
    body = send_chat(client, t["id"], "rewrite the letter")
    assert EM_DASH not in body["tailoring"]["cover_letter"]
    assert body["tailoring"]["cover_letter"] == "Dear team,\n\nRewritten, cleaner now.\n\nBest,\nPat"
