"""Normalization helpers for ATS ingestion (Phase 3b).

Pure functions only — no I/O, mirroring patterns.py. Adapters return
NormalizedJob records; the refresh pipeline computes dedupe keys and level
bands and owns all DB writes.
"""

import html
import re
from dataclasses import dataclass
from html.parser import HTMLParser


@dataclass
class NormalizedJob:
    external_id: str | None
    title: str
    url: str | None
    location: str | None
    remote_type: str  # remote/hybrid/onsite/unknown
    salary_min: int | None
    salary_max: int | None
    salary_stated: bool
    description_text: str | None


class AdapterError(Exception):
    """An ATS fetch failed in a way the pipeline should log per-company."""


# --- HTML -> text -----------------------------------------------------------

_BLOCK_TAGS = {
    "p", "div", "li", "ul", "ol", "br", "h1", "h2", "h3", "h4", "h5", "h6",
    "tr", "table", "section", "article", "blockquote",
}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def strip_html(markup: str | None) -> str | None:
    """HTML (possibly entity-escaped, as Greenhouse returns it) -> plain text."""
    if not markup:
        return None
    if "&lt;" in markup and "<" not in markup:
        markup = html.unescape(markup)
    parser = _TextExtractor()
    parser.feed(markup)
    text = html.unescape("".join(parser.parts))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() or None


# --- dedupe -----------------------------------------------------------------


def norm(text: str | None) -> str:
    """Lowercase, strip punctuation, collapse whitespace — for dedupe keys."""
    if not text:
        return ""
    text = re.sub(r"[^\w\s]", "", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def make_dedupe_key(company_id: int, job: NormalizedJob) -> str:
    if job.external_id:
        return f"{company_id}:{job.external_id}"
    return f"{company_id}:{norm(job.title)}|{norm(job.location)}"


# --- title filter ------------------------------------------------------------


# What .search() returns when there is no include gate: a real Match, because
# adapters treat the result as truthy-keep / None-drop.
_MATCH_ALL = re.compile("")


@dataclass
class TitleFilter:
    """Include-match AND NOT exclude-match. Exposes .search() so adapters
    (which duck-type on re.Pattern) need no changes."""

    include: re.Pattern | None
    exclude: re.Pattern | None = None

    def search(self, title: str):
        # No include keywords -> NO GATE: everything ingests, excludes still
        # apply. The old reading ("nothing ingests") was invisible while the
        # seed carried design terms and fatal once the seed shipped empty —
        # a fresh install refreshed green with zero jobs stored. Workday is
        # the one remaining ceiling: its API requires a searchText
        # (see adapters/workday.py).
        if self.exclude is not None and self.exclude.search(title):
            return None
        if self.include is None:
            return _MATCH_ALL.search(title)
        return self.include.search(title)


def _word_boundary_pattern(keywords: list[str]) -> re.Pattern | None:
    # (?<!\w)/(?!\w) rather than \b: a user-typed keyword may start or end in
    # punctuation ("c++", ".net"), and \b anchors on a word char INSIDE the
    # keyword — \bc\+\+\b can never match anything. Same posture as
    # compile_level_bands, and identical to \b for word-edged keywords.
    alts = "|".join(re.escape(k.strip()) for k in keywords if k.strip())
    return re.compile(rf"(?<!\w)(?:{alts})(?!\w)", re.I) if alts else None


def compile_title_filter(keywords: list[str], exclude: list[str] = ()) -> TitleFilter:
    """Word-boundary, case-insensitive — 'design' must not match 'Designated'.

    exclude (settings.title_exclude_keywords, fed by the dismissal feedback
    loop) wins over include: an excluded title never ingests.
    """
    return TitleFilter(
        include=_word_boundary_pattern(keywords),
        exclude=_word_boundary_pattern(list(exclude)),
    )


# --- remote classification ---------------------------------------------------


# A location that is ONLY a country/region names no office — it's the ATS
# convention for a location-flexible (remote) posting (Greenhouse publishes
# remote-US roles as bare "United States"; caught live when a tracked remote
# Director role published this way hard-failed the location gate as
# "onsite"). Tier-1
# still decides workability — "Canada" classifies remote here and then fails
# the US-scope check there. Extend as observed; matched after lowercasing.
_COUNTRY_SCOPE_LOCATIONS = {
    "united states", "united states of america", "usa", "u.s.", "u.s.a.", "us",
    "canada", "north america", "americas",
    "global", "worldwide", "anywhere",
    "europe", "emea", "apac", "latam",
}


def classify_remote(location: str | None, hint: str | None = None) -> str:
    """Structured ATS hint wins; else scan the location string only.

    Deliberately never scans JD bodies ("we offer hybrid options" would
    false-positive).
    """
    if hint in ("remote", "hybrid", "onsite"):
        return hint
    loc = (location or "").lower()
    if "remote" in loc:
        return "remote"
    if "hybrid" in loc:
        return "hybrid"
    if loc.strip() in _COUNTRY_SCOPE_LOCATIONS:
        return "remote"
    if loc:
        return "onsite"
    return "unknown"


# --- salary extraction --------------------------------------------------------

# Comma-grouped amounts may carry cents (e.g. iCIMS/Jibe tags8 "$150,000.00 -
# $190,000.00 Yearly"); the optional .dd is consumed so a range isn't truncated
# to its first value at the decimal point. "and" is a separator too — Apple
# phrases ranges as "between $135,400 and $250,600". Plain ungrouped amounts
# (Greenhouse: "$185000 - $242000") match too — the 40k–900k sanity
# bounds and the hourly-nearby check carry the disambiguation a comma used to.
_GROUPED = r"\d{2,3}(?:,\d{3})+(?:\.\d{2})?"
_KAMOUNT = r"\d{2,3}(?:\.\d)?\s?[Kk]"
_PLAIN = r"\d{5,6}(?:\.\d{2})?(?!\d)"  # (?!\d): never truncate-match a 7+ digit figure
_AMOUNT = r"\$\s?(" + _GROUPED + r"|" + _KAMOUNT + r"|" + _PLAIN + r")"
_RANGE_RE = re.compile(
    _AMOUNT + r"\s*(?:-|–|—|to|and)\s*\$?\s?(" + _GROUPED + r"|" + _KAMOUNT + r"|" + _PLAIN + r")"
)
_SINGLE_RE = re.compile(r"\$\s?(" + _GROUPED + r"|" + _PLAIN + r")")
_HOURLY_RE = re.compile(r"(?:/\s*(?:hr|hour)|per\s+hour|hourly|an\s+hour)", re.I)

SALARY_MIN_SANE = 40_000
SALARY_MAX_SANE = 900_000


def _to_annual(amount: str) -> int:
    amount = amount.strip()
    if amount[-1] in "kK":
        return int(float(amount[:-1].strip()) * 1000)
    return int(float(amount.replace(",", "")))  # float() tolerates trailing cents


def _hourly_nearby(text: str, end: int) -> bool:
    return bool(_HOURLY_RE.search(text[end : end + 40]))


def extract_salary(text: str | None) -> tuple[int | None, int | None, bool]:
    """(salary_min, salary_max, stated). Stated only if captured from the
    posting itself — never guess. Hourly rates and out-of-bounds figures are
    rejected rather than stored as annual."""
    if not text:
        return None, None, False
    m = _RANGE_RE.search(text)
    if m and not _hourly_nearby(text, m.end()):
        lo, hi = _to_annual(m.group(1)), _to_annual(m.group(2))
        if SALARY_MIN_SANE <= lo <= hi <= SALARY_MAX_SANE:
            return lo, hi, True
    m = _SINGLE_RE.search(text)
    if m and not _hourly_nearby(text, m.end()):
        val = _to_annual(m.group(1))
        if SALARY_MIN_SANE <= val <= SALARY_MAX_SANE:
            return val, val, True
    return None, None, False


# --- level band ---------------------------------------------------------------

# Checked most-senior-first; first hit wins. The ic phrase leads because an
# explicit "(Individual Contributor)" designation overrides the seniority
# words around it — "Product Design Director (Individual Contributor)" is an
# ic seat, not a director band (2026-07 IC hard-cap verdict). The junior band
# (2026-08, band caps) is split in two: program designations (intern/co-op/
# apprentice) also override seniority words, while junior/jr/associate sit
# BELOW the seniority patterns so "Associate Creative Director" stays a
# director and only unmatched titles like "Junior Product Designer" land here.
# Config shape (Phase 2): PHRASES, not regexes. The band names were already
# config (tier1_params.target_title_bands), but the patterns that produce them
# were not, so a doc could name a band derive_level_band could never emit.
# Phrases are safe to hand-edit — no escaping, no backtracking, no way to write
# a pattern that matches everything.
DEFAULT_LEVEL_BANDS = {
    "bands": [
        {"band": "ic", "label": "IC", "phrases": ["individual contributor"]},
        {
            "band": "junior",
            "label": "Junior",
            "phrases": ["intern", "internship", "co-op", "coop", "apprentice"],
        },
        {
            "band": "vp_plus",
            "label": "VP+",
            "phrases": ["vp", "vice president", "chief", "cdo"],
        },
        {
            "band": "senior_director",
            "label": "Sr Director",
            "phrases": ["senior director", "sr director", "sr. director"],
        },
        {"band": "director", "label": "Director", "phrases": ["director", "head of"]},
        {
            "band": "senior_manager",
            "label": "Sr Manager",
            "phrases": ["senior manager", "sr manager", "sr. manager"],
        },
        {"band": "manager", "label": "Manager", "phrases": ["manager", "lead"]},
        {"band": "junior", "label": "Junior", "phrases": ["junior", "jr", "associate"]},
    ],
    "fallback": "ic",
}


def compile_level_bands(cfg: dict) -> tuple[list[tuple[str, re.Pattern]], str]:
    """(ordered [(band, pattern)], fallback band) from the config shape.

    Each phrase becomes a whole-word pattern: words are escaped and joined with
    [\\s-]+, so "head of" matches "Head-of" too. The anchors are (?<!\\w)/(?!\\w)
    rather than \\b because a phrase may END in punctuation — "sr." escapes to
    "sr\\.", and a \\b after the dot could never match a following space.
    """
    compiled: list[tuple[str, re.Pattern]] = []
    for entry in cfg["bands"]:
        alts = []
        for phrase in entry["phrases"]:
            words = [re.escape(w) for w in re.split(r"[\s-]+", phrase.strip()) if w]
            if words:
                alts.append(r"[\s-]+".join(words))
        if alts:
            compiled.append(
                (entry["band"], re.compile(r"(?<!\w)(?:" + "|".join(alts) + r")(?!\w)", re.I))
            )
    return compiled, cfg.get("fallback", "ic")


_DEFAULT_COMPILED, _DEFAULT_FALLBACK = compile_level_bands(DEFAULT_LEVEL_BANDS)


def derive_level_band(title: str, bands=None, fallback: str | None = None) -> str:
    """Checked most-senior-first, first hit wins. Passing no bands uses the
    shipped defaults, which keeps every legacy caller behaving identically."""
    for band, pat in bands if bands is not None else _DEFAULT_COMPILED:
        if pat.search(title):
            return band
    return fallback or _DEFAULT_FALLBACK
