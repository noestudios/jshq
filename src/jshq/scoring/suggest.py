"""Deterministic dismissal learning: repeated same-reason
dismissals on similar titles -> suggest a title_exclude_keyword.

Pure functions, no I/O. Suggestions are one-click accept in the UI and NEVER
auto-applied. A candidate that equals an include keyword is dropped — e.g.
excluding "researcher" outright would kill UX-research roles; that trade-off
is the user's to make by editing title_keywords, not the suggester's.
"""

from collections import defaultdict

from jshq.ats.normalize import norm

# Title words that never identify a role family on their own.
_STOPWORDS = {
    "of", "and", "the", "a", "an", "for", "to", "in", "at", "on",
    "senior", "sr", "staff", "lead", "principal", "associate",
    "i", "ii", "iii", "iv", "1", "2", "3",
}


def _ngrams(title: str) -> set[str]:
    tokens = [t for t in norm(title).split() if t not in _STOPWORDS]
    grams = set(tokens)
    grams.update(f"{a} {b}" for a, b in zip(tokens, tokens[1:]))
    return grams


def suggest_exclusions(
    dismissals: list[dict],
    include_keywords: list[str],
    existing_excludes: list[str],
    ignored: list[str],
    company_names: list[str] = (),
    threshold: int = 3,
) -> list[dict]:
    """dismissals: [{"reason": str, "title": str}, ...] newest-first.

    Returns [{"keyword", "count", "examples"}] sorted by count desc.

    company_names are never suggested: ATS titles embed the brand (e.g.
    "Meridian Loom — Senior Designer"), so a targeted company's name would
    otherwise recur as a qualifying n-gram. Any candidate containing a
    company-name token is dropped (so even a brand-paired bigram like
    "meridian product" goes), leaving the genuine role word to surface.
    """
    blocked = {norm(k) for k in (*include_keywords, *existing_excludes, *ignored)}
    company_tokens: set[str] = set()
    for name in company_names:
        company_tokens.update(norm(name).split())

    by_reason: dict[str, list[dict]] = defaultdict(list)
    for d in dismissals:
        if d.get("reason") and d.get("title"):
            by_reason[d["reason"]].append(d)

    candidates: dict[str, dict] = {}
    for reason, group in by_reason.items():
        counts: dict[str, list[str]] = defaultdict(list)
        for d in group:
            for gram in _ngrams(d["title"]):
                counts[gram].append(f"{d['title']} — {reason}")

        qualifying = {g for g, hits in counts.items() if len(hits) >= threshold}
        # Drop any candidate containing a tracked company-name token *before* the
        # unigram/bigram dedupe, so a genuine role word isn't suppressed as a
        # component of a brand-paired bigram that we then remove.
        if company_tokens:
            qualifying = {g for g in qualifying if not (set(g.split()) & company_tokens)}
        # Prefer the longest qualifying n-gram: drop unigrams that only
        # qualify as parts of a qualifying bigram.
        bigram_parts = {part for g in qualifying if " " in g for part in g.split()}
        for gram in qualifying:
            if " " not in gram and gram in bigram_parts:
                continue
            if gram in blocked:
                continue
            entry = candidates.setdefault(gram, {"keyword": gram, "count": 0, "examples": []})
            entry["count"] += len(counts[gram])
            entry["examples"].extend(counts[gram][:3])
            entry["examples"] = entry["examples"][:3]

    return sorted(candidates.values(), key=lambda c: -c["count"])
