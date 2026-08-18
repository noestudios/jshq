#!/usr/bin/env python3
"""Render the resume: data/resume/content.json → resume.html + resume.pdf.

Usage:
  .venv/bin/python scripts/render_resume.py [--out data/resume/resume.pdf] [--open]

--open opens the rendered PDF for a visual check in the OS default viewer.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

from jshq.resume import render  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--content", type=Path, default=render.CONTENT_PATH)
    parser.add_argument("--out", type=Path,
                        default=REPO_ROOT / "data" / "resume" / "resume.pdf")
    parser.add_argument("--open", action="store_true",
                        help="open the rendered PDF in the default viewer")
    args = parser.parse_args()

    try:
        content = render.load_content(args.content)
        out_pdf = render.render_pdf(render.build_html(content), args.out)
    except render.ResumeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {out_pdf} (+ {out_pdf.with_suffix('.html').name})")

    if args.open:
        if sys.platform == "win32":
            os.startfile(out_pdf)  # Windows-only API
        else:
            opener = "open" if sys.platform == "darwin" else "xdg-open"
            subprocess.run([opener, str(out_pdf)], check=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
