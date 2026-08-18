"""fetch_drive_times.py — candidate prefilter + populate wiring.

The routing call is injected, so these tests never touch the network (mirrors
"ATS adapters test against recorded fixtures, never live endpoints")."""

import importlib.util
import sqlite3
from pathlib import Path

from jshq.scoring import geo

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "fetch_drive_times.py"
_spec = importlib.util.spec_from_file_location("fetch_drive_times", _SCRIPT)
fdt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fdt)

EVANSTON = {"lat": 42.046391, "lng": -87.694352}


def test_candidate_towns_prefilters_by_straight_line():
    keys = {key for key, _lat, _lng in fdt.candidate_towns(EVANSTON, 25)}
    assert "evanston, il" in keys
    assert "arlington heights, il" in keys  # ~15 straight-mi, inside 25
    assert "milwaukee, wi" not in keys  # ~72 straight-mi, outside


def test_candidate_town_keys_match_the_gate():
    # The script and geo.town_key() must agree, or the override never hits.
    keys = {key for key, _lat, _lng in fdt.candidate_towns(EVANSTON, 25)}
    assert geo.town_key("Arlington Heights, IL") in keys


def test_populate_writes_measured_times_via_injected_fetcher():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
    towns = [("arlington heights, il", 42.08, -87.98), ("oak park, il", 41.89, -87.79)]

    def stub_fetcher(center, town_list):  # no network
        return {key: 30 for key, _lat, _lng in town_list}

    report = fdt.populate(conn, EVANSTON, towns, stub_fetcher)
    assert report == {"candidates": 2, "measured": 2}
    assert geo.read_drive_times(conn) == {"arlington heights, il": 30, "oak park, il": 30}


def test_default_max_miles_scales_from_minutes():
    cfg = {"radius_minutes": 30, "estimate": {"detour_factor": 1.4, "avg_mph": 33}}
    assert 15 <= fdt._default_max_miles(cfg) <= 30
