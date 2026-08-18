CONTACT = {
    "name": "Jane Doe",
    "role": "Head of Design",
    "linkedin_url": "https://www.linkedin.com/in/janedoe/",
    "email": "jane@example.com",
    "source": "event",
    "relationship_notes": "met at an industry meetup",
    "last_contact_date": "2026-06-01",
}


def create_company(client, name="Acme Studio"):
    return client.post("/api/companies", json={"name": name}).json()


def test_source_is_free_text(client):
    # The vocabulary lives in the contact_sources setting (Phase 5b), so the
    # API accepts any value verbatim — a closed Literal here would 422 every
    # source the user adds in Settings.
    created = client.post("/api/contacts", json={**CONTACT, "source": "book club"}).json()
    assert created["source"] == "book club"


def test_create_and_list_with_company_name(client):
    company = create_company(client)
    response = client.post("/api/contacts", json={**CONTACT, "company_id": company["id"]})
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["company_name"] == "Acme Studio"

    contacts = client.get("/api/contacts").json()
    assert [c["id"] for c in contacts] == [created["id"]]


def test_create_without_company(client):
    response = client.post("/api/contacts", json={"name": "Solo Contact"})
    assert response.status_code == 201
    assert response.json()["company_id"] is None


def test_create_bogus_company_400(client):
    response = client.post("/api/contacts", json={**CONTACT, "company_id": 999})
    assert response.status_code == 400


def test_validation_422(client):
    assert client.post("/api/contacts", json={"name": ""}).status_code == 422
    # source is deliberately NOT validated here — see test_source_is_free_text.


def test_update(client, db):
    created = client.post("/api/contacts", json=CONTACT).json()
    db.execute(
        "UPDATE contacts SET updated_at = '2000-01-01 00:00:00' WHERE id = ?",
        (created["id"],),
    )
    db.commit()

    response = client.put(
        f"/api/contacts/{created['id']}", json={**CONTACT, "role": "VP Design"}
    )
    assert response.status_code == 200
    updated = response.json()
    assert updated["role"] == "VP Design"
    assert updated["updated_at"] > "2000-01-01 00:00:00"


def test_update_missing_404(client):
    assert client.put("/api/contacts/999", json=CONTACT).status_code == 404


def test_delete_removes_activities(client, db):
    created = client.post("/api/contacts", json=CONTACT).json()
    db.execute(
        "INSERT INTO activities (entity_type, entity_id, date, type, content)"
        " VALUES ('contact', ?, '2026-06-01', 'meeting', 'intro call')",
        (created["id"],),
    )
    db.commit()

    response = client.delete(f"/api/contacts/{created['id']}")
    assert response.status_code == 200
    assert response.json() == {"deleted": created["id"]}
    assert client.get("/api/contacts").json() == []
    remaining = db.execute(
        "SELECT COUNT(*) AS n FROM activities WHERE entity_type = 'contact'"
    ).fetchone()["n"]
    assert remaining == 0


def test_delete_missing_404(client):
    assert client.delete("/api/contacts/999").status_code == 404
