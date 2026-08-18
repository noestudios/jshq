"""jshq.notify unit tests. The osascript subprocess boundary is monkeypatched by
the autouse notify_calls fixture (conftest.py), so nothing here — or anywhere in
the suite — can pop a real macOS banner."""

from jshq import notify


def test_popups_enabled_truth_table(db):
    assert notify.popups_enabled(db) is True  # absent key = ON
    db.execute("INSERT INTO settings (key, value) VALUES ('notify_popups', 'false')")
    db.commit()
    assert notify.popups_enabled(db) is False
    db.execute("UPDATE settings SET value = 'true' WHERE key = 'notify_popups'")
    db.commit()
    assert notify.popups_enabled(db) is True
    db.execute("UPDATE settings SET value = 'not-json' WHERE key = 'notify_popups'")
    db.commit()
    assert notify.popups_enabled(db) is True  # unparseable = ON, never crash


def test_send_truncates_and_uses_defaults(notify_calls):
    notify.send("x" * 500)
    assert len(notify_calls) == 1
    assert len(notify_calls[0]["message"]) == 200
    assert notify_calls[0]["title"] == "Job Search HQ"
    assert notify_calls[0]["sound"] == "Glass"


def test_send_never_raises(monkeypatch):
    def boom(message, title, sound):
        raise OSError("no osascript")

    monkeypatch.setattr(notify, "_osascript", boom)
    notify.send("hello")  # must swallow
