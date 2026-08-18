"""Assert the built wheel actually ships the package data the app needs.

hatchling packages everything under src/jshq implicitly — nothing in
pyproject names frontend/ or defaults/, so a packaging regression would
only surface as a broken install. CI runs this after `python -m build`;
locally: python -m build && python scripts/check_wheel.py dist/*.whl

Exits non-zero listing anything missing (or any .DS_Store that snuck in).
"""

import sys
import zipfile

REQUIRED = [
    "jshq/schema.sql",
    "jshq/frontend/index.html",
    "jshq/frontend/favicon.ico",
    "jshq/frontend/fonts/geist-latin.woff2",
    "jshq/frontend/fonts/OFL-Geist.txt",
    "jshq/frontend/js/app.js",
    "jshq/frontend/css/tokens.css",
    "jshq/defaults/fit_criteria.md",
    "jshq/defaults/fit_criteria.starter.md",
    "jshq/defaults/voice_guide.md",
    "jshq/defaults/voice_guide.starter.md",
    "jshq/defaults/resume/content.starter.json",
    "jshq/defaults/user-manual.md",
    "jshq/defaults/AI-TELLS.md",
    "jshq/resume/template.html",
    "jshq/resume/fonts/Carlito-Regular.ttf",
    "jshq/resume/fonts/OFL.txt",
    "jshq/scoring/us_places.tsv",
]


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: check_wheel.py <wheel>")
        return 2
    names = set(zipfile.ZipFile(sys.argv[1]).namelist())
    missing = [n for n in REQUIRED if n not in names]
    strays = [n for n in names if ".DS_Store" in n]
    for n in missing:
        print(f"MISSING from wheel: {n}")
    for n in strays:
        print(f"stray junk in wheel: {n}")
    if not missing and not strays:
        print(f"wheel ok: {len(names)} files, all {len(REQUIRED)} required present")
    return 1 if (missing or strays) else 0


if __name__ == "__main__":
    raise SystemExit(main())
