"""Strip shared employer boilerplate from JDs before the model pass.

Employers repeat identical multi-paragraph blocks (competency lists, culture
blurbs, legal tails) across every posting on their board. At temperature 0 the
scorer then reads mostly-shared text and collapses similar roles to identical
verdicts — and every posting re-pays input tokens for the same paragraphs.

Pure functions, no I/O (tier1.py's contract). description_text in the DB is
never modified; run_scoring strips at prompt-build time only. The unit is the
blank-line-delimited block (strip_html emits block-tag boundaries as newlines
and collapses runs to at most two), matched exactly after whitespace/case
normalization — paraphrased boilerplate is deliberately NOT chased; temp-0
collapse only needs the verbatim blocks gone.
"""

import re

MIN_BLOCK_CHARS = 100  # normalized length; shorter blocks are never fingerprinted
MIN_SIBLINGS = 3  # block must appear in >= this many of the company's JDs
MIN_KEEP_CHARS = 800  # stripping that leaves less than this falls back to the original
MARKER = "[shared company boilerplate removed]"


def _blocks(text: str) -> list[str]:
    return [b for b in re.split(r"\n{2,}", text) if b.strip()]


def _key(block: str) -> str:
    return re.sub(r"\s+", " ", block).strip().lower()


def shared_block_keys(texts: list[str]) -> set[str]:
    """Keys of blocks >= MIN_BLOCK_CHARS chars appearing in >= MIN_SIBLINGS
    distinct texts. Each key counts at most once per text, so a block repeated
    inside ONE posting can't self-promote to boilerplate."""
    counts: dict[str, int] = {}
    for text in texts:
        if not text:
            continue
        seen = {k for k in (_key(b) for b in _blocks(text)) if len(k) >= MIN_BLOCK_CHARS}
        for k in seen:
            counts[k] = counts.get(k, 0) + 1
    return {k for k, n in counts.items() if n >= MIN_SIBLINGS}


def strip_shared(text: str | None, shared: set[str]) -> str | None:
    """Drop shared blocks and append one MARKER line so the model knows the JD
    is partial. If the survivor would be under MIN_KEEP_CHARS (e.g. one role
    posted in three locations — near-identical siblings), return the original:
    duplicate boilerplate beats an empty JD."""
    if not text or not shared:
        return text
    kept = [b for b in _blocks(text) if _key(b) not in shared]
    if len(kept) == len(_blocks(text)):
        return text
    stripped = "\n\n".join(kept)
    if len(stripped) < MIN_KEEP_CHARS:
        return text
    return f"{stripped}\n\n{MARKER}"
