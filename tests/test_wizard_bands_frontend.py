"""Two fresh-install wizard bugs on the "Hard limits" step (target-seniority grid):

1. The native checkboxes inherited the `.form-field input` text-input sizing
   (~40px tall, stretched to the flex cell at different widths per label). A
   reset pins them back to the intrinsic control box.
2. The picker rendered `level_bands` in the criteria doc's *match* order
   (first-hit-wins pulls IC/Junior to the top), which reads as nonsense in a
   seniority picker. `levelBands()` now sorts into a display order, most-senior
   first with IC/Junior last, decoupled from the scoring match-order.

Source-scan guards, matching the other *_frontend.py tests.
"""

import re

from jshq import paths

FRONTEND = paths.FRONTEND_DIR


def _read(rel):
    return (FRONTEND / rel).read_text(encoding="utf-8")


def test_form_field_checkboxes_reset_to_native_size():
    css = _read("css/app.css")
    m = re.search(r'\.form-field input\[type="checkbox"\][^{]*\{([^}]*)\}', css)
    assert m, "no native-checkbox reset rule for .form-field"
    body = m.group(1)
    assert "width: auto" in body and "height: auto" in body
    assert "border: 0" in body  # drop the text-input border/padding box


def test_level_bands_picker_sorts_into_display_order_ic_junior_last():
    js = _read("js/lib/vocab.js")
    assert "DISPLAY_ORDER" in js
    # levelBands() sorts by the display order rather than returning raw doc order.
    body = js[js.index("export function levelBands"):]
    assert ".sort(" in body and "DISPLAY_ORDER" in body
    # The canonical display order (FALLBACK.level_bands) is most-senior-first and
    # must end with IC then Junior — the match-order puts them first, the bug.
    values = re.findall(r'value:\s*"([a-z_]+)"', js)
    assert values[-2:] == ["ic", "junior"], values[-2:]
