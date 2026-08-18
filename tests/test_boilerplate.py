"""boilerplate.py — shared-block fingerprinting and prompt-time stripping."""

from jshq.scoring import boilerplate as bp

# One shared block comfortably over MIN_BLOCK_CHARS…
SHARED = (
    "Our competencies are Human-Centered, Business-Focused, Problem-Solving, "
    "Collaboration and Communication; we celebrate an inclusive culture across "
    "every team, location and level of the organization."
)
# …and per-role unique text comfortably over MIN_KEEP_CHARS after stripping.
def unique(n):
    return f"Role {n}: " + f"lead squad {n} through discovery and delivery. " * 25


def jd(n):
    return f"{unique(n)}\n\n{SHARED}"


def test_shared_at_exactly_min_siblings():
    keys = bp.shared_block_keys([jd(1), jd(2), jd(3)])
    assert keys == {bp._key(SHARED)}


def test_not_shared_below_min_siblings():
    assert bp.shared_block_keys([jd(1), jd(2)]) == set()


def test_short_shared_blocks_never_fingerprinted():
    short = "Basic Qualifications"  # < MIN_BLOCK_CHARS — headings must survive
    texts = [f"{unique(n)}\n\n{short}" for n in range(3)]
    assert bp.shared_block_keys(texts) == set()


def test_repeats_within_one_text_do_not_self_promote():
    one = f"{SHARED}\n\n{SHARED}\n\n{SHARED}"
    assert bp.shared_block_keys([one, unique(1)]) == set()


def test_strip_removes_block_and_appends_marker_once():
    shared = bp.shared_block_keys([jd(1), jd(2), jd(3)])
    out = bp.strip_shared(jd(1), shared)
    assert SHARED not in out
    assert out.count(bp.MARKER) == 1
    assert "Role 1:" in out


def test_strip_falls_back_when_survivor_too_short():
    # Three near-identical postings (one role, three locations): stripping
    # would leave almost nothing — return the original instead.
    texts = [f"Short intro {n}.\n\n{SHARED}" for n in range(3)]
    shared = bp.shared_block_keys(texts)
    assert bp.strip_shared(texts[0], shared) == texts[0]


def test_whitespace_variant_copies_still_match():
    reflowed = SHARED.replace("; ", ";\n  ").replace(", ", ",  ")
    keys = bp.shared_block_keys([jd(1), jd(2), f"{unique(3)}\n\n{reflowed}"])
    assert keys == {bp._key(SHARED)}


def test_none_and_untouched_passthrough():
    assert bp.strip_shared(None, {"anything"}) is None
    assert bp.strip_shared("no shared blocks here", set()) == "no shared blocks here"
    # text containing none of the shared keys comes back unmodified (no marker)
    assert bp.strip_shared(unique(1), {bp._key(SHARED)}) == unique(1)
