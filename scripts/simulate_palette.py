"""Colour-vision + contrast checks for palette work. Stdlib only.

Why this exists as a committed script rather than an ad-hoc calculation: the
P4 decision table's deuteranope column could not be reproduced (CLAUDE.md
records it as known bad — normal-vision figures reproduce exactly, the
simulated ones do not), and VISUAL-TARGET.md instructs re-simulating rather
than citing them. An ad-hoc number nobody can re-run is how that happened.

The palette must stay legible under deuteranopia, and it is warm, so two
states that differ by HUE alone can be invisible. The gate every pair must clear is
therefore two-sided: a dE2000 measured on the Vienot-projected pair (not the
normal-vision one), and a raw dL* floor, because dE2000 can be inflated by
chroma terms that the projection has already collapsed.

METHOD NOTE — the likely source of the bad P4 figures. Vienot 1999 operates on
LINEAR RGB. Applying it to gamma-encoded sRGB is a common implementation slip
and it systematically understates the collapse, which is the direction the P4
table was wrong in. This module removes gamma first (srgb_to_linear) and the
round-trip is asserted by --selftest.

  Vienot, Brettel & Mollon (1999), "Digital video colourmaps for checking the
  legibility of displays by dichromats", Color Research & Application 24(4).

Usage
  scripts/simulate_palette.py PAIR [PAIR ...]   PAIR is "#hex/#hex[:label]"
  scripts/simulate_palette.py --contrast FG/BG [...]
  scripts/simulate_palette.py --selftest
"""

from __future__ import annotations

import sys

# --- matrices ---------------------------------------------------------------

# Hunt-Pointer-Estevez LMS, as used by Vienot 1999 (applied to LINEAR rgb).
RGB_TO_LMS = (
    (17.8824, 43.5161, 4.11935),
    (3.45565, 27.1554, 3.86714),
    (0.0299566, 0.184309, 1.46709),
)

# Dichromat projections onto the single plane spanned by the remaining cones.
# The missing cone's response is reconstructed from the other two.
DEUTERAN = ((1.0, 0.0, 0.0), (0.494207, 0.0, 1.24827), (0.0, 0.0, 1.0))
PROTAN = ((0.0, 2.02344, -2.52581), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))

# sRGB -> XYZ, D65 (IEC 61966-2-1), applied to linear rgb.
RGB_TO_XYZ = (
    (0.4124564, 0.3575761, 0.1804375),
    (0.2126729, 0.7151522, 0.0721750),
    (0.0193339, 0.1191920, 0.9503041),
)
D65 = (0.95047, 1.0, 1.08883)


def _mul(m, v):
    return tuple(sum(m[r][c] * v[c] for c in range(3)) for r in range(3))


def _inv(m):
    """3x3 inverse. Computed rather than transcribed — the published inverse
    LMS matrix is where copy errors live."""
    a, b, c = m[0]
    d, e, f = m[1]
    g, h, i = m[2]
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    if abs(det) < 1e-12:
        raise ValueError("singular matrix")
    return (
        ((e * i - f * h) / det, (c * h - b * i) / det, (b * f - c * e) / det),
        ((f * g - d * i) / det, (a * i - c * g) / det, (c * d - a * f) / det),
        ((d * h - e * g) / det, (b * g - a * h) / det, (a * e - b * d) / det),
    )


LMS_TO_RGB = _inv(RGB_TO_LMS)


# --- colour conversions -----------------------------------------------------


def parse_hex(s: str) -> tuple[float, float, float]:
    s = s.strip().lstrip("#")
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    if len(s) != 6:
        raise ValueError(f"not a hex colour: {s!r}")
    return tuple(int(s[i : i + 2], 16) / 255.0 for i in (0, 2, 4))


def to_hex(rgb) -> str:
    return "#" + "".join(f"{min(255, max(0, round(c * 255))):02x}" for c in rgb)


def srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def linear_to_srgb(c: float) -> float:
    c = min(1.0, max(0.0, c))
    return c * 12.92 if c <= 0.0031308 else 1.055 * (c ** (1 / 2.4)) - 0.055


def simulate(rgb, kind: str = "deuteranope"):
    """Vienot 1999 dichromat projection. Input/output are gamma-encoded sRGB in
    0..1; the projection itself happens in linear light."""
    proj = DEUTERAN if kind.startswith("deuter") else PROTAN
    lin = tuple(srgb_to_linear(c) for c in rgb)
    lms = _mul(RGB_TO_LMS, lin)
    out_lin = _mul(LMS_TO_RGB, _mul(proj, lms))
    return tuple(linear_to_srgb(c) for c in out_lin)


def to_lab(rgb):
    lin = tuple(srgb_to_linear(c) for c in rgb)
    x, y, z = _mul(RGB_TO_XYZ, lin)
    x, y, z = x / D65[0], y / D65[1], z / D65[2]

    def f(t):
        return t ** (1 / 3) if t > 216 / 24389 else (841 / 108) * t + 4 / 29

    fx, fy, fz = f(x), f(y), f(z)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def delta_e_2000(lab1, lab2) -> float:
    """CIEDE2000. Sharma, Wu & Dalal (2005) formulation."""
    import math

    l1, a1, b1 = lab1
    l2, a2, b2 = lab2
    kl = kc = kh = 1.0

    c1 = math.hypot(a1, b1)
    c2 = math.hypot(a2, b2)
    cbar = (c1 + c2) / 2
    g = 0.5 * (1 - math.sqrt(cbar**7 / (cbar**7 + 25**7))) if cbar > 0 else 0.0

    a1p, a2p = (1 + g) * a1, (1 + g) * a2
    c1p, c2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    h1p = math.degrees(math.atan2(b1, a1p)) % 360 if (a1p or b1) else 0.0
    h2p = math.degrees(math.atan2(b2, a2p)) % 360 if (a2p or b2) else 0.0

    dlp = l2 - l1
    dcp = c2p - c1p
    if c1p * c2p == 0:
        dhp = 0.0
    elif abs(h2p - h1p) <= 180:
        dhp = h2p - h1p
    elif h2p - h1p > 180:
        dhp = h2p - h1p - 360
    else:
        dhp = h2p - h1p + 360
    dHp = 2 * math.sqrt(c1p * c2p) * math.sin(math.radians(dhp) / 2)

    lbar = (l1 + l2) / 2
    cbarp = (c1p + c2p) / 2
    if c1p * c2p == 0:
        hbarp = h1p + h2p
    elif abs(h1p - h2p) <= 180:
        hbarp = (h1p + h2p) / 2
    elif h1p + h2p < 360:
        hbarp = (h1p + h2p + 360) / 2
    else:
        hbarp = (h1p + h2p - 360) / 2

    t = (
        1
        - 0.17 * math.cos(math.radians(hbarp - 30))
        + 0.24 * math.cos(math.radians(2 * hbarp))
        + 0.32 * math.cos(math.radians(3 * hbarp + 6))
        - 0.20 * math.cos(math.radians(4 * hbarp - 63))
    )
    dtheta = 30 * math.exp(-(((hbarp - 275) / 25) ** 2))
    rc = 2 * math.sqrt(cbarp**7 / (cbarp**7 + 25**7)) if cbarp > 0 else 0.0
    sl = 1 + (0.015 * (lbar - 50) ** 2) / math.sqrt(20 + (lbar - 50) ** 2)
    sc = 1 + 0.045 * cbarp
    sh = 1 + 0.015 * cbarp * t
    rt = -math.sin(math.radians(2 * dtheta)) * rc

    return math.sqrt(
        (dlp / (kl * sl)) ** 2
        + (dcp / (kc * sc)) ** 2
        + (dHp / (kh * sh)) ** 2
        + rt * (dcp / (kc * sc)) * (dHp / (kh * sh))
    )


def relative_luminance(rgb) -> float:
    lin = [srgb_to_linear(c) for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast_ratio(fg, bg) -> float:
    a, b = relative_luminance(fg), relative_luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


# --- reporting --------------------------------------------------------------

# Gates. dL* is the one that matters most: dE2000 on a projected pair
# can still be carried by residual b*-axis difference that is not actionable at
# a 4px dot size, so the lightness floor is checked independently.
MIN_DE_SIM = 3.0
MIN_DL = 3.0


def report_pair(h1: str, h2: str, label: str = "") -> dict:
    r1, r2 = parse_hex(h1), parse_hex(h2)
    lab1, lab2 = to_lab(r1), to_lab(r2)
    s1, s2 = simulate(r1), simulate(r2)
    slab1, slab2 = to_lab(s1), to_lab(s2)
    return {
        "label": label,
        "a": h1,
        "b": h2,
        "L_a": lab1[0],
        "L_b": lab2[0],
        "dL": abs(lab1[0] - lab2[0]),
        "de_normal": delta_e_2000(lab1, lab2),
        "de_deut": delta_e_2000(slab1, slab2),
        "sim_a": to_hex(s1),
        "sim_b": to_hex(s2),
    }


def print_table(rows) -> bool:
    hdr = f"{'pair':<34}{'L*a':>7}{'L*b':>7}{'dL*':>7}{'dE norm':>9}{'dE deut':>9}  gate"
    print(hdr)
    print("-" * len(hdr))
    ok = True
    for r in rows:
        passes = r["de_deut"] >= MIN_DE_SIM and r["dL"] >= MIN_DL
        ok = ok and passes
        name = r["label"] or f"{r['a']} vs {r['b']}"
        print(
            f"{name[:33]:<34}{r['L_a']:>7.1f}{r['L_b']:>7.1f}{r['dL']:>7.2f}"
            f"{r['de_normal']:>9.2f}{r['de_deut']:>9.2f}  {'PASS' if passes else 'FAIL'}"
        )
    print(f"\ngates: deuteranope dE2000 >= {MIN_DE_SIM}, dL* >= {MIN_DL}")
    return ok


def selftest() -> bool:
    ok = True

    def check(name, got, want, tol):
        nonlocal ok
        good = abs(got - want) <= tol
        ok = ok and good
        print(f"  {'ok ' if good else 'FAIL'} {name}: {got:.4f} (want {want} +/- {tol})")

    print("selftest")
    # Gamma round-trip.
    worst = max(abs(linear_to_srgb(srgb_to_linear(i / 255)) - i / 255) for i in range(256))
    check("gamma round-trip max error", worst, 0.0, 1e-9)
    # Matrix inverse round-trip.
    v = (0.2, 0.5, 0.9)
    back = _mul(LMS_TO_RGB, _mul(RGB_TO_LMS, v))
    check("LMS round-trip max error", max(abs(back[i] - v[i]) for i in range(3)), 0.0, 1e-9)
    # Known Lab values.
    check("L* of #ffffff", to_lab(parse_hex("#ffffff"))[0], 100.0, 0.01)
    check("L* of #000000", to_lab(parse_hex("#000000"))[0], 0.0, 0.01)
    check("L* of #808080", to_lab(parse_hex("#808080"))[0], 53.585, 0.01)
    # Sharma CIEDE2000 reference pair 1.
    check(
        "dE2000 Sharma pair 1",
        delta_e_2000((50.0, 2.6772, -79.7751), (50.0, 0.0, -82.7485)),
        2.0425,
        0.001,
    )
    # Sharma reference pair 14 (large hue rotation).
    check(
        "dE2000 Sharma pair 14",
        delta_e_2000((50.0, 2.5, 0.0), (50.0, 0.0, -2.5)),
        4.3065,
        0.001,
    )
    # WCAG anchor.
    check("contrast #000 on #fff", contrast_ratio(parse_hex("#000"), parse_hex("#fff")), 21.0, 0.01)
    # A dichromat invariant: greys are unchanged by the projection.
    g = parse_hex("#808080")
    check("grey is projection-invariant", delta_e_2000(to_lab(g), to_lab(simulate(g))), 0.0, 0.35)
    # Red/green of equal luminance must collapse for a deuteranope while
    # remaining far apart in normal vision — the whole point of the gate.
    r, gr = parse_hex("#c1553a"), parse_hex("#6b8f3a")
    print(
        f"  info red/green dE normal {delta_e_2000(to_lab(r), to_lab(gr)):.2f}"
        f" -> deut {delta_e_2000(to_lab(simulate(r)), to_lab(simulate(gr))):.2f}"
    )
    return ok


def main(argv) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] == "--selftest":
        return 0 if selftest() else 1
    if argv[0] == "--contrast":
        for spec in argv[1:]:
            spec, _, label = spec.partition(":")
            fg, _, bg = spec.partition("/")
            print(f"{label or spec:<34}{contrast_ratio(parse_hex(fg), parse_hex(bg)):>7.2f}:1")
        return 0
    rows = []
    for spec in argv:
        spec, _, label = spec.partition(":")
        a, _, b = spec.partition("/")
        rows.append(report_pair(a, b, label))
    return 0 if print_table(rows) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
