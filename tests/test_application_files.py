"""Application document routes (2026-07-21): list / raw-body upload / delete /
serve. Hermetic: conftest client + seeders, APPLICATIONS_DIR in tmp."""

import pytest

import jshq.main as main_module


@pytest.fixture
def apps_dir(tmp_path, monkeypatch):
    directory = tmp_path / "applications"
    monkeypatch.setattr(main_module, "APPLICATIONS_DIR", directory)
    return directory


def test_upload_list_serve_roundtrip(client, seed_application, apps_dir):
    app_id = seed_application()
    r = client.put(
        f"/api/applications/{app_id}/files/My Resume (Exampleco).pdf",
        content=b"%PDF-1.4 fake",
    )
    assert r.status_code == 201
    assert r.json() == {"name": "My Resume (Exampleco).pdf", "size": 13}

    listing = client.get(f"/api/applications/{app_id}/files").json()
    assert [(f["name"], f["size"], f["generated"]) for f in listing] == [
        ("My Resume (Exampleco).pdf", 13, False)
    ]
    assert listing[0]["modified"]  # ISO stamp present

    served = client.get(f"/api/applications/{app_id}/files/My Resume (Exampleco).pdf")
    assert served.status_code == 200
    assert served.content == b"%PDF-1.4 fake"
    assert served.headers["content-type"] == "application/pdf"


def test_upload_collision_gets_suffix_not_overwrite(client, seed_application, apps_dir):
    app_id = seed_application()
    client.put(f"/api/applications/{app_id}/files/resume.pdf", content=b"one")
    r = client.put(f"/api/applications/{app_id}/files/resume.pdf", content=b"two")
    assert r.json()["name"] == "resume-2.pdf"
    names = [f["name"] for f in client.get(f"/api/applications/{app_id}/files").json()]
    assert names == ["resume-2.pdf", "resume.pdf"]
    # the original is untouched
    assert client.get(f"/api/applications/{app_id}/files/resume.pdf").content == b"one"


def test_upload_rejects_bad_names_and_extensions(client, seed_application, apps_dir):
    app_id = seed_application()
    base = f"/api/applications/{app_id}/files"
    assert client.put(f"{base}/evil.exe", content=b"x").status_code == 400
    assert client.put(f"{base}/.hidden.pdf", content=b"x").status_code == 400  # must start alphanumeric
    assert client.put(f"{base}/resume-v1.pdf", content=b"x").status_code == 409  # reserved generated name
    assert client.put(f"{base}/ok.pdf", content=b"").status_code == 400  # empty body
    # traversal never reaches the filesystem: encoded slashes fail SAFE_FILE_RE
    # (405 = the unmatched path fell through to the static mount, which
    # rejects non-GET outright — still never touches the files dir)
    r = client.put(f"{base}/..%2Fescape.pdf", content=b"x")
    assert r.status_code in (400, 404, 405)
    assert not (apps_dir.parent / "escape.pdf").exists()


def test_windows_reserved_names_rejected(client, seed_application, apps_dir):
    """CON.pdf and friends resolve to DEVICES on Windows — write_bytes would
    'succeed' into the console and store nothing. Rejected on every platform
    so a data dir stays copyable to Windows."""
    app_id = seed_application()
    base = f"/api/applications/{app_id}/files"
    for name in ("CON.pdf", "con.pdf", "NUL.txt", "COM1.docx", "lpt3.md", "AUX.html", "PRN .pdf"):
        assert client.put(f"{base}/{name}", content=b"x").status_code == 400, name
        assert client.get(f"{base}/{name}").status_code == 404, name
        assert client.delete(f"{base}/{name}").status_code == 404, name
    assert not (apps_dir / str(app_id)).exists()  # nothing ever reached disk


def test_trailing_dot_and_space_rejected(client, seed_application, apps_dir):
    """Windows strips trailing dots/spaces on path resolution, so
    'resume.pdf.' would alias 'resume.pdf' — GET and DELETE must not accept
    a second spelling of an existing file's name."""
    app_id = seed_application()
    base = f"/api/applications/{app_id}/files"
    client.put(f"{base}/resume.pdf", content=b"real")
    assert client.put(f"{base}/resume.pdf.", content=b"x").status_code == 400
    assert client.get(f"{base}/resume.pdf.").status_code == 404
    assert client.delete(f"{base}/resume.pdf.").status_code == 404
    assert client.get(f"{base}/resume.pdf%20").status_code == 404
    assert client.delete(f"{base}/resume.pdf%20").status_code == 404
    # the real file is untouched throughout
    assert client.get(f"{base}/resume.pdf").content == b"real"


def test_case_collision_never_overwrites(client, seed_application, apps_dir):
    """Resume.pdf vs resume.pdf: distinct on case-sensitive filesystems,
    colliding on Windows/macOS — either way the first upload's content
    survives and the two results get distinct names."""
    app_id = seed_application()
    base = f"/api/applications/{app_id}/files"
    client.put(f"{base}/Resume.pdf", content=b"first")
    second = client.put(f"{base}/resume.pdf", content=b"second").json()["name"]
    assert second in ("resume.pdf", "resume-2.pdf")  # filesystem-dependent
    assert client.get(f"{base}/Resume.pdf").content == b"first"
    names = [f["name"] for f in client.get(f"{base}").json()]
    assert len(set(names)) == 2


def test_served_mime_types_are_deterministic(client, seed_application, apps_dir):
    """A closed map, not mimetypes.guess_type — the Windows registry must not
    decide what Content-Type a resume is served with."""
    app_id = seed_application()
    base = f"/api/applications/{app_id}/files"
    for name, mime in (
        ("a.pdf", "application/pdf"),
        ("a.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("a.txt", "text/plain"),
        ("a.md", "text/markdown"),
    ):
        client.put(f"{base}/{name}", content=b"x")
        assert client.get(f"{base}/{name}").headers["content-type"].startswith(mime), name


def test_upload_size_cap(client, seed_application, apps_dir, monkeypatch):
    app_id = seed_application()
    monkeypatch.setattr(main_module, "UPLOAD_MAX_BYTES", 10)
    r = client.put(f"/api/applications/{app_id}/files/big.pdf", content=b"x" * 11)
    assert r.status_code == 413


def test_delete_uploaded_but_never_generated(client, seed_application, apps_dir):
    app_id = seed_application()
    client.put(f"/api/applications/{app_id}/files/mine.pdf", content=b"x")
    # a generated-name file (as the tailoring pipeline writes them)
    (apps_dir / str(app_id) / "resume-v1.pdf").write_bytes(b"%PDF generated")

    assert client.delete(f"/api/applications/{app_id}/files/resume-v1.pdf").status_code == 409
    # The guard is case-insensitive like the macOS/Windows filesystems these
    # paths live on: "resume-v1.PDF" unlinks the protected resume-v1.pdf
    # there, so a case-sensitive guard was a delete bypass.
    assert client.delete(f"/api/applications/{app_id}/files/resume-v1.PDF").status_code == 409
    assert client.delete(f"/api/applications/{app_id}/files/Resume-V1.pdf").status_code == 409
    assert client.delete(f"/api/applications/{app_id}/files/mine.pdf").json() == {"deleted": "mine.pdf"}
    assert client.delete(f"/api/applications/{app_id}/files/mine.pdf").status_code == 404
    names = [f["name"] for f in client.get(f"/api/applications/{app_id}/files").json()]
    assert names == ["resume-v1.pdf"]  # generated file survives, flagged as such


def test_delete_while_open_is_conflict_not_500(client, seed_application, apps_dir, monkeypatch):
    """Windows can't unlink a file with an open handle — a retryable 409."""
    import pathlib

    app_id = seed_application()
    client.put(f"/api/applications/{app_id}/files/held.pdf", content=b"x")
    monkeypatch.setattr(
        pathlib.Path, "unlink",
        lambda self, missing_ok=False: (_ for _ in ()).throw(PermissionError("in use")),
    )
    r = client.delete(f"/api/applications/{app_id}/files/held.pdf")
    assert r.status_code == 409
    assert "in use" in r.json()["detail"]


def test_generated_files_flagged_in_listing(client, seed_application, apps_dir):
    app_id = seed_application()
    gen_dir = apps_dir / str(app_id)
    gen_dir.mkdir(parents=True)
    (gen_dir / "cover-v2.pdf").write_bytes(b"%PDF")
    # the .html render sources beside the PDFs are generated artifacts too
    (gen_dir / "cover-v2.html").write_bytes(b"<html>")
    client.put(f"/api/applications/{app_id}/files/sent-version.docx", content=b"docx")
    flags = {f["name"]: f["generated"] for f in client.get(f"/api/applications/{app_id}/files").json()}
    assert flags == {"cover-v2.html": True, "cover-v2.pdf": True, "sent-version.docx": False}
    assert client.delete(f"/api/applications/{app_id}/files/cover-v2.html").status_code == 409


def test_unknown_application_404s(client, apps_dir):
    assert client.get("/api/applications/999/files").status_code == 404
    assert client.put("/api/applications/999/files/x.pdf", content=b"x").status_code == 404
    assert client.delete("/api/applications/999/files/x.pdf").status_code == 404
