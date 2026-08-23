"""Regenerate the user manual's "Error codes" appendix from the registry.

Splices errors.manual_appendix() between the APPENDIX_START/END markers in
src/jshq/defaults/user-manual.md, appending the whole block at the end of the
file on first run. tests/test_errors.py fails whenever the shipped section
and the registry drift, so: add or edit a code in src/jshq/errors.py, run
this, commit both.

Usage: .venv/bin/python scripts/gen_error_appendix.py
"""

from jshq import paths
from jshq.errors import APPENDIX_END, APPENDIX_START, manual_appendix

MANUAL = paths.DEFAULTS_DIR / "user-manual.md"


def main() -> None:
    text = MANUAL.read_text(encoding="utf-8")
    block = f"{APPENDIX_START}\n{manual_appendix()}{APPENDIX_END}\n"
    if APPENDIX_START in text:
        head, rest = text.split(APPENDIX_START, 1)
        _, tail = rest.split(APPENDIX_END, 1)
        text = head + block + tail.lstrip("\n")
    else:
        text = text.rstrip("\n") + "\n\n" + block
    MANUAL.write_text(text, encoding="utf-8")
    print(f"wrote {MANUAL}")


if __name__ == "__main__":
    main()
