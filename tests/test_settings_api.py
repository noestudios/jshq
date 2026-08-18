"""Settings + suggestions endpoints."""

import json


def seed_dismissal(db, title, reason="not my focus area"):
    db.execute(
        """INSERT INTO activities (entity_type, entity_id, date, type, content)
           VALUES ('job', 1, date('now'), 'dismissal', ?)""",
        (json.dumps({"reason": reason, "note": None, "title": title}),),
    )
    db.commit()


def test_get_seeded_setting(client):
    resp = client.get("/api/settings/dismiss_reasons")
    assert resp.status_code == 200
    assert "not my focus area" in resp.json()["value"]


def test_put_round_trips(client):
    resp = client.put("/api/settings/title_exclude_keywords", json={"value": ["ml", "sales"]})
    assert resp.status_code == 200
    assert client.get("/api/settings/title_exclude_keywords").json()["value"] == ["ml", "sales"]


def test_put_rejects_wrong_shapes(client):
    # SettingIn accepts any JSON value, so the shape check is the route's job.
    # A string where a list belongs didn't fail the PUT — it 500ed a LATER
    # endpoint (str.append on the ignore list) or quietly seeded one LinkedIn
    # title per character via list("text").
    assert client.put("/api/settings/suggestions_ignored", json={"value": "oops"}).status_code == 422
    assert client.put("/api/settings/title_keywords", json={"value": [1, 2]}).status_code == 422
    assert client.put("/api/settings/notify_popups", json={"value": ["x"]}).status_code == 422
    assert client.put("/api/settings/notify_popups", json={"value": False}).status_code == 200
    assert client.get("/api/settings/notify_popups").json()["value"] is False


def test_internal_keys_hidden(client):
    assert client.get("/api/settings/schema_version").status_code == 404
    assert client.put("/api/settings/last_refresh", json={"value": "x"}).status_code == 404


def test_suggestions_empty_without_dismissals(client):
    assert client.get("/api/suggestions").json() == {
        "title_exclude": [],
        "scoring_rule": [],
        "reminders": [],
    }


def test_suggestion_lifecycle(client, db):
    for title in (
        "Machine Learning Engineer",
        "Senior Machine Learning Researcher",
        "Machine Learning Research Scientist",
    ):
        seed_dismissal(db, title)

    out = client.get("/api/suggestions").json()["title_exclude"]
    assert any(s["keyword"] == "machine learning" for s in out)

    resp = client.post(
        "/api/suggestions/title-exclude",
        json={"keyword": "machine learning", "action": "accept"},
    )
    assert resp.json() == {"title_exclude_keywords": ["machine learning"]}
    # accepted -> no longer suggested
    out = client.get("/api/suggestions").json()["title_exclude"]
    assert not any(s["keyword"] == "machine learning" for s in out)


def test_ignore_suppresses_suggestion(client, db):
    for title in ("ML Ops Engineer", "ML Ops Lead", "ML Ops Manager"):
        seed_dismissal(db, title)
    assert any(
        s["keyword"] == "ml ops"
        for s in client.get("/api/suggestions").json()["title_exclude"]
    )
    client.post("/api/suggestions/title-exclude", json={"keyword": "ml ops", "action": "ignore"})
    assert not any(
        s["keyword"] == "ml ops"
        for s in client.get("/api/suggestions").json()["title_exclude"]
    )
    # ignore must not add it to the exclude list
    assert client.get("/api/settings/title_exclude_keywords").json()["value"] == []


def test_accept_is_idempotent(client, db):
    client.post("/api/suggestions/title-exclude", json={"keyword": "sales", "action": "accept"})
    client.post("/api/suggestions/title-exclude", json={"keyword": "sales", "action": "accept"})
    assert client.get("/api/settings/title_exclude_keywords").json()["value"] == ["sales"]


def test_notify_popups_round_trips_and_defaults_on(client):
    # absent key -> [] (the API's absent-value marker); the frontend and
    # notify.popups_enabled treat anything but false as ON
    assert client.get("/api/settings/notify_popups").json()["value"] == []
    assert client.put("/api/settings/notify_popups", json={"value": False}).status_code == 200
    assert client.get("/api/settings/notify_popups").json()["value"] is False
    assert client.put("/api/settings/notify_popups", json={"value": True}).status_code == 200
    assert client.get("/api/settings/notify_popups").json()["value"] is True
