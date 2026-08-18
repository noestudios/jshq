"""First-run seeding (Phase 4): a fresh data dir gets NEUTRAL *.starter.md
templates, not the shipped Alex example — which stays in DEFAULTS_DIR as the
reference the golden-prompt + calibration read."""

from jshq import paths
from jshq.scoring import criteria as C


def test_seeds_neutral_starters_into_fresh_dir(tmp_path):
    created = paths.seed_data_dir(tmp_path)
    crit_doc = tmp_path / "fit_criteria.md"
    voice = tmp_path / "voice_guide.md"
    assert crit_doc in created and voice in created
    # Seeded from the *.starter.md templates, byte-for-byte (rename-on-copy).
    assert crit_doc.read_bytes() == (paths.DEFAULTS_DIR / "fit_criteria.starter.md").read_bytes()
    assert voice.read_bytes() == (paths.DEFAULTS_DIR / "voice_guide.starter.md").read_bytes()


def test_seeded_criteria_is_valid_and_neutral(tmp_path):
    paths.seed_data_dir(tmp_path)
    crit = C.load_criteria(tmp_path / "fit_criteria.md")
    # Loads without error; a blank slate — no floor, no filters, no wishlist.
    assert crit.params["comp_floor"] == 0
    assert crit.params["location_allowlist"] == []
    assert crit.params["excluded_sectors"] == []
    assert crit.params["target_title_bands"] == []
    assert crit.tier2 == []
    assert crit.persona["display_name"] is None
    # None of Alex's / any personal content may leak into the user's starting doc.
    body = (tmp_path / "fit_criteria.md").read_text(encoding="utf-8").lower()
    for tok in ("evanston", "160000", "185000", "alex", "meridian", "skokie", "chris"):
        assert tok not in body


def test_seeding_never_overwrites(tmp_path):
    paths.seed_data_dir(tmp_path)
    edited = tmp_path / "voice_guide.md"
    edited.write_text("my own voice", encoding="utf-8")
    again = paths.seed_data_dir(tmp_path)
    assert again == []  # nothing re-copied on a second run
    assert edited.read_text(encoding="utf-8") == "my own voice"


def test_shipped_example_remains_alex_and_distinct():
    """Alex stays in DEFAULTS_DIR (golden-prompt + calibration read it); it is a
    different file from the neutral starter now seeded to users."""
    example = (paths.DEFAULTS_DIR / "fit_criteria.md").read_text(encoding="utf-8")
    starter = (paths.DEFAULTS_DIR / "fit_criteria.starter.md").read_text(encoding="utf-8")
    assert "160000" in example  # Alex's comp floor is still the shipped example
    assert example != starter


def test_seeds_resume_starter_into_subdirectory(tmp_path):
    # Phase 5b: before this seed, every tailoring endpoint 500'd on a fresh
    # install (resume/content.json existed nowhere). The seed must create the
    # subdirectory itself and pass render's own validator, or the "friendly
    # starter" would fail the exact call it exists to unblock.
    from jshq.resume import render

    created = paths.seed_data_dir(tmp_path)
    content = tmp_path / "resume" / "content.json"
    assert content in created
    assert content.read_bytes() == (
        paths.DEFAULTS_DIR / "resume" / "content.starter.json"
    ).read_bytes()
    render.validate_content(  # raises on any shape problem
        __import__("json").loads(content.read_text(encoding="utf-8"))
    )
    # idempotent alongside the other seeds
    assert paths.seed_data_dir(tmp_path) == []
