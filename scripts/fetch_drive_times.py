"""Populate measured per-town drive-times for the location-radius commute gate (7i).

Routes from the location_radius center to every US place within a
straight-line prefilter and writes {town_key: minutes} to the
location_drive_times setting, which evaluate_tier1() prefers over its offline
estimate. Default router is OSRM's public demo (no key); --ors uses
OpenRouteService with ORS_API_KEY (read here only, never by the backend). Both
return free-flow car times.

This is the ONLY networked step — scoring and the test-suite never hit a router.
One-time / occasional; re-runnable; idempotent.

Usage:
  .venv/bin/python scripts/fetch_drive_times.py [--max-miles N] [--ors] [--dry-run]
"""

import argparse
import os
import sys
import time
from pathlib import Path


import httpx  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from jshq import db  # noqa: E402
from jshq.scoring import geo  # noqa: E402
from jshq.scoring.criteria import load_criteria  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(REPO_ROOT / ".env")

OSRM_URL = "https://router.project-osrm.org"
OSRM_MAX = 100  # coords per /table request (center + 99 towns)
ORS_URL = "https://api.openrouteservice.org/v2/matrix/driving-car"
ORS_MAX = 50


def candidate_towns(center: dict, max_miles: float) -> list[tuple[str, float, float]]:
    """(town_key, lat, lng) for every place within max_miles straight-line of
    center. town_key matches geo.town_key() so the gate finds the measured time."""
    out = []
    for (_city, usps), (lat, lng, name) in geo._load().items():
        if geo.haversine(center["lat"], center["lng"], lat, lng) <= max_miles:
            out.append((f"{name}, {usps}".lower(), lat, lng))
    return out


def fetch_osrm(center: dict, towns: list, base_url: str = OSRM_URL, sleep: float = 1.0) -> dict:
    """{town_key: minutes} via OSRM /table (source 0 = center), chunked."""
    result: dict[str, int] = {}
    with httpx.Client(timeout=60) as client:
        for i in range(0, len(towns), OSRM_MAX - 1):
            chunk = towns[i : i + OSRM_MAX - 1]
            pts = [(center["lng"], center["lat"])] + [(lng, lat) for _, lat, lng in chunk]
            coordstr = ";".join(f"{lng},{lat}" for lng, lat in pts)
            resp = client.get(
                f"{base_url}/table/v1/driving/{coordstr}",
                params={"sources": 0, "annotations": "duration"},
            )
            resp.raise_for_status()
            durations = resp.json().get("durations", [[]])[0]
            for (key, _lat, _lng), secs in zip(chunk, durations[1:]):
                if secs is not None:
                    result[key] = round(secs / 60)
            if i + OSRM_MAX - 1 < len(towns):
                time.sleep(sleep)
    return result


def fetch_ors(center: dict, towns: list, api_key: str, sleep: float = 1.0) -> dict:
    """{town_key: minutes} via OpenRouteService matrix (source 0 = center)."""
    result: dict[str, int] = {}
    with httpx.Client(timeout=60) as client:
        for i in range(0, len(towns), ORS_MAX):
            chunk = towns[i : i + ORS_MAX]
            locations = [[center["lng"], center["lat"]]] + [[lng, lat] for _, lat, lng in chunk]
            resp = client.post(
                ORS_URL,
                headers={"Authorization": api_key},
                json={"locations": locations, "sources": [0], "metrics": ["duration"]},
            )
            resp.raise_for_status()
            durations = resp.json()["durations"][0]
            for (key, _lat, _lng), secs in zip(chunk, durations[1:]):
                if secs is not None:
                    result[key] = round(secs / 60)
            if i + ORS_MAX < len(towns):
                time.sleep(sleep)
    return result


def populate(conn, center: dict, towns: list, fetcher) -> dict:
    """Route center→towns via `fetcher` and store the measured minutes. `fetcher`
    is injectable so tests can stub the network entirely."""
    durations = fetcher(center, towns)
    geo.write_drive_times(conn, durations)
    conn.commit()
    return {"candidates": len(towns), "measured": len(durations)}


def _default_max_miles(cfg: dict) -> float:
    """Straight-line miles whose drive could plausibly be within radius_minutes,
    with a 50% margin — so we only route towns that could actually qualify."""
    est = cfg.get("estimate") or {}
    minutes = cfg.get("radius_minutes", 30)
    miles = minutes * est.get("avg_mph", 33) / 60 / est.get("detour_factor", 1.4) * 1.5
    return max(15.0, miles)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-miles", type=float, default=None, help="straight-line prefilter")
    parser.add_argument("--ors", action="store_true", help="use OpenRouteService (ORS_API_KEY)")
    parser.add_argument("--dry-run", action="store_true", help="list candidates, no routing/write")
    args = parser.parse_args()

    cfg = load_criteria().params.get("location_radius")
    if not cfg or not cfg.get("center"):
        print("no location_radius.center in fit_criteria.md — nothing to route", file=sys.stderr)
        sys.exit(1)
    center = cfg["center"]
    max_miles = args.max_miles if args.max_miles is not None else _default_max_miles(cfg)
    towns = candidate_towns(center, max_miles)
    print(f"center={center.get('label')} max_miles={max_miles:.0f} candidates={len(towns)}")

    if args.dry_run:
        for key, _lat, _lng in towns[:25]:
            print("  ", key)
        if len(towns) > 25:
            print(f"  … +{len(towns) - 25} more")
        return

    if args.ors:
        key = os.environ.get("ORS_API_KEY")
        if not key:
            print("ORS_API_KEY not set", file=sys.stderr)
            sys.exit(1)
        fetcher = lambda c, t: fetch_ors(c, t, key)  # noqa: E731
    else:
        fetcher = fetch_osrm

    db.init_db()
    conn = db.connect()
    try:
        report = populate(conn, center, towns, fetcher)
    finally:
        conn.close()
    print(report)


if __name__ == "__main__":
    main()
