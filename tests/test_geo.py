"""geo.py — offline geocoder + haversine for the location-radius gate (7i)."""

from jshq.scoring import geo

EVANSTON = {"lat": 42.046391, "lng": -87.694352}


def dist(lat, lng):
    return geo.haversine(EVANSTON["lat"], EVANSTON["lng"], lat, lng)


def test_geocodes_city_state():
    for q in ("Evanston, IL", "Arlington Heights, IL", "Chicago, IL"):
        assert geo.geocode(q) is not None, q


def test_geocode_tolerates_noise_and_full_state_name():
    # remote/hybrid noise, parentheses, comma-less and spelled-out states resolve.
    for q in ("Remote - Arlington Heights, IL", "Evanston, IL (Hybrid)", "Wilmette, Illinois", "Oak Park IL"):
        assert geo.geocode(q) is not None, q


def test_geocode_drops_trailing_descriptor_word():
    # "Naperville Crossings" resolves by falling back to the "Naperville" place row.
    assert geo.geocode("Naperville Crossings, IL") is not None


def test_unresolvable_returns_none():
    for q in ("Springfield", "Remote", "Hybrid", "", None,
              "Sydney, Australia", "London, United Kingdom", "Nowhereville, ZZ"):
        assert geo.geocode(q) is None, q


def test_bare_town_without_state_is_none():
    # Ambiguous across states — return None rather than guess a coordinate.
    assert geo.geocode("Evanston") is None


def test_haversine_known_distances():
    chi = geo.geocode("Chicago, IL")
    assert 11 < dist(*chi) < 18  # ~14.5mi
    nap = geo.geocode("Naperville, IL")
    assert 26 < dist(*nap) < 38  # ~32mi
    mke = geo.geocode("Milwaukee, WI")
    assert dist(*mke) > 60  # ~72mi


def test_town_key_is_stable_and_normalized():
    assert geo.town_key("Arlington Heights, IL") == "arlington heights, il"
    assert geo.town_key("Remote - Arlington Heights, IL") == "arlington heights, il"  # noise-stripped, same key
    assert geo.town_key("Evanston, IL (Hybrid)") == "evanston, il"
    assert geo.town_key("Nowhereville") is None


def test_estimate_minutes_scales_with_distance():
    # Evanston→Arlington Heights ~15 straight-mi × 1.4 ÷ 33mph ≈ 39 min; Evanston→Evanston ≈ 0.
    heights = geo.estimate_minutes("Arlington Heights, IL", EVANSTON, 1.4, 33)
    assert 30 < heights < 45
    assert geo.estimate_minutes("Evanston, IL", EVANSTON, 1.4, 33) < 1
    assert geo.estimate_minutes("Milwaukee, WI", EVANSTON, 1.4, 33) > heights


def test_estimate_minutes_is_defensive():
    assert geo.estimate_minutes("Arlington Heights, IL", None, 1.4, 33) is None
    assert geo.estimate_minutes("Arlington Heights, IL", {"lat": 42.0}, 1.4, 33) is None  # no lng
    assert geo.estimate_minutes("Arlington Heights, IL", EVANSTON, 0, 33) is None  # bad detour factor
    assert geo.estimate_minutes("Arlington Heights, IL", EVANSTON, 1.4, 0) is None  # bad speed
    assert geo.estimate_minutes("Nowhere at all", EVANSTON, 1.4, 33) is None


def test_drive_times_roundtrip():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
    assert geo.read_drive_times(conn) == {}  # absent → empty
    geo.write_drive_times(conn, {"arlington heights, il": 34})
    assert geo.read_drive_times(conn) == {"arlington heights, il": 34}


def test_resolve_returns_label_and_coords():
    hit = geo.resolve("Evanston, IL")
    assert hit["label"] == "Evanston, IL"
    assert round(hit["lat"], 2) == 42.05
    assert geo.resolve("Nowhereville") is None
