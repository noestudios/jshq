"""The Help view's user-manual endpoint serves docs/user-manual.md read-only,
mirroring the criteria-doc viewer (Phase 9). No path parameter — one file only."""


def test_user_manual_endpoint_serves_the_doc(client):
    response = client.get("/api/docs/user-manual")
    assert response.status_code == 200
    markdown = response.json()["markdown"]
    assert markdown.strip()
    assert "Job Search HQ" in markdown
