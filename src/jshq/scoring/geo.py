"""Offline geocoder for the Tier 1 location-radius gate (Phase 7i).

Resolves a free-text job location ("Evanston, IL", "Remote - Oak Park, IL") to a
coordinate using a bundled, public-domain US place table — no network, no API
key, no rate limits. Distance is a hand-rolled haversine; the radius check in
tier1.py is `allowlist match OR within_radius(...)`, so this can only ever turn
a Tier 1 location `fail` into a `pass` (it loosens, never tightens).

Resolution is deliberately conservative: a location with no recognizable US
state (USPS code or name) returns None rather than guessing — a wrong town would
wrongly pass a far job. Bare town strings without a state still fall through to
tier1.py's existing allowlist matcher.

Data: backend/app/scoring/us_places.tsv — US Census Bureau 2023 Gazetteer
Places national file (public domain), trimmed to NAME/state/lat/lng with the
LSAD descriptor (city/town/CDP/…) stripped. See that file's header.
"""

import json
import re
import sqlite3
from math import asin, cos, radians, sin, sqrt
from pathlib import Path

_DATA_PATH = Path(__file__).with_name("us_places.tsv")

# Full state/territory names + USPS codes + the AP-style short forms tier1.py
# recognizes, all mapped to the USPS code the place table is keyed on. (geo.py
# cannot import tier1's lexicon — tier1 imports geo, which would cycle.)
_STATE_TO_USPS = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC", "washington dc": "DC",
    "puerto rico": "PR", "guam": "GU", "virgin islands": "VI",
    # AP-style spelled-short forms (mirrors tier1._US_STATE_ABBR)
    "calif": "CA", "cali": "CA", "ariz": "AZ", "ark": "AR", "colo": "CO",
    "conn": "CT", "fla": "FL", "ill": "IL", "ind": "IN", "kans": "KS",
    "mass": "MA", "mich": "MI", "minn": "MN", "miss": "MS", "mont": "MT",
    "nebr": "NE", "nev": "NV", "okla": "OK", "ore": "OR", "oreg": "OR",
    "penn": "PA", "penna": "PA", "tenn": "TN", "tex": "TX", "wash": "WA",
    "wis": "WI", "wisc": "WI", "wyo": "WY",
}
# USPS two-letter codes (self-mapping) for the comma-less "City ST" tail case.
_USPS_CODES = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id",
    "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms",
    "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok",
    "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv",
    "wi", "wy", "dc", "pr", "gu", "vi",
}
for _c in _USPS_CODES:
    _STATE_TO_USPS.setdefault(_c, _c.upper())

# Noise tokens dropped before parsing a "City, ST" out of a job location string.
_NOISE_RE = re.compile(
    r"\b(?:remote|hybrid|onsite|on-site|usa|u\.s\.a|u\.s|united states|"
    r"of america|metro(?:politan)? area|metro area)\b"
)
_CITY_LEAD_RE = re.compile(r"^(?:greater|metro|metropolitan|the)\s+")
_CITY_TAIL_RE = re.compile(r"\s+(?:area|region|metro|metropolitan)$")

_EARTH_RADIUS_MI = 3958.7613


def _normplace(text: str) -> str:
    """Lowercase, punctuation→space, collapse whitespace. Keeps multi-word place
    names intact ('selmont-west selmont' → 'selmont west selmont') so the index and
    the query normalize identically."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", text.lower())).strip()


# (city_norm, usps) -> (lat, lng, proper_name). Built once, lazily.
_index: dict[tuple[str, str], tuple[float, float, str]] | None = None


def _load() -> dict[tuple[str, str], tuple[float, float, str]]:
    global _index
    if _index is not None:
        return _index
    idx: dict[tuple[str, str], tuple[float, float, str]] = {}
    with _DATA_PATH.open(encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("#") or not line.strip():
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) != 4:
                continue
            usps, name, lat, lng = parts
            try:
                coord = (float(lat), float(lng), name)
            except ValueError:
                continue
            idx.setdefault((_normplace(name), usps), coord)
    _index = idx
    return _index


def _lookup_city(city: str, usps: str) -> tuple[float, float, str] | None:
    """Resolve a city segment within a state. Tries the cleaned name, then drops
    trailing words ('Naperville Crossings' → 'Naperville') until a hit or one word remains —
    never the leading word, which is the head noun."""
    idx = _load()
    city = _CITY_TAIL_RE.sub("", _CITY_LEAD_RE.sub("", _normplace(city)))
    words = city.split()
    while words:
        hit = idx.get((" ".join(words), usps))
        if hit is not None:
            return (hit[0], hit[1], f"{hit[2]}, {usps}")  # label carries the state
        words = words[:-1]
    return None


def _geocode_full(location: str | None) -> tuple[float, float, str] | None:
    """(lat, lng, 'Proper Name, ST') for a free-text location, else None."""
    if not location:
        return None
    cleaned = _NOISE_RE.sub(" ", re.sub(r"\([^)]*\)", " ", location.lower()))
    segments = [s.strip() for s in re.split(r"[,/|]", cleaned) if s.strip()]
    if not segments:
        return None

    # Comma-less "City ST" / "City State" tail (e.g. "Oak Park IL").
    if len(segments) == 1:
        words = _normplace(segments[0]).split()
        if len(words) >= 2 and words[-1] in _STATE_TO_USPS:
            usps = _STATE_TO_USPS[words[-1]]
            return _lookup_city(" ".join(words[:-1]), usps)
        if len(words) >= 3 and " ".join(words[-2:]) in _STATE_TO_USPS:
            usps = _STATE_TO_USPS[" ".join(words[-2:])]
            return _lookup_city(" ".join(words[:-2]), usps)
        return None

    # "City, ST" — the last meaningful segment is the state, the prior the city.
    state_seg = _normplace(segments[-1])
    usps = _STATE_TO_USPS.get(state_seg)
    if usps is None and state_seg:
        usps = _STATE_TO_USPS.get(state_seg.split()[-1])
    if usps is None:
        return None
    return _lookup_city(segments[-2], usps)


def geocode(location: str | None) -> tuple[float, float] | None:
    """(lat, lng) for a free-text location, or None when unresolvable."""
    hit = _geocode_full(location)
    return (hit[0], hit[1]) if hit else None


def town_key(location: str | None) -> str | None:
    """Normalized "name, st" identity for a location, or None — the shared key
    the measured-drive-time cache and the gate agree on (fetch_drive_times.py
    builds the same key from each place row's name + USPS)."""
    hit = _geocode_full(location)
    return hit[2].lower() if hit else None


def resolve(location: str | None) -> dict | None:
    """{'lat','lng','label'} for the center-resolution endpoint, or None."""
    hit = _geocode_full(location)
    if hit is None:
        return None
    return {"lat": hit[0], "lng": hit[1], "label": hit[2]}


def haversine(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in miles."""
    p1, p2 = radians(lat1), radians(lat2)
    dphi, dlmb = radians(lat2 - lat1), radians(lng2 - lng1)
    a = sin(dphi / 2) ** 2 + cos(p1) * cos(p2) * sin(dlmb / 2) ** 2
    return 2 * _EARTH_RADIUS_MI * asin(sqrt(a))


def estimate_minutes(location: str | None, center: dict, detour_factor, avg_mph) -> float | None:
    """Offline drive-time estimate (minutes) from `center` ({'lat','lng'}) to
    `location`: straight-line miles inflated by a road-detour factor, divided by
    an average speed. The fallback when a town has no measured time. Returns None
    when the location is unresolvable or the model params are invalid."""
    if not center:
        return None
    try:
        clat, clng = float(center["lat"]), float(center["lng"])
        factor, mph = float(detour_factor), float(avg_mph)
    except (KeyError, TypeError, ValueError):
        return None
    if factor <= 0 or mph <= 0:
        return None
    coord = geocode(location)
    if coord is None:
        return None
    road_miles = haversine(clat, clng, coord[0], coord[1]) * factor
    return road_miles / mph * 60


def read_drive_times(conn: sqlite3.Connection) -> dict:
    """Measured per-town drive-times in minutes, keyed by town_key()
    ({'arlington heights, il': 34, ...}). Settings k/v, populated by fetch_drive_times.py;
    {} until then. Lazy, not seeded, not in EDITABLE_SETTINGS — mirrors
    inclusion_rules / scoring_rules."""
    row = conn.execute(
        "SELECT value FROM settings WHERE key = 'location_drive_times'"
    ).fetchone()
    if not row or row["value"] is None:
        return {}
    try:
        data = json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_drive_times(conn: sqlite3.Connection, mapping: dict) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES ('location_drive_times', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (json.dumps(mapping),),
    )
