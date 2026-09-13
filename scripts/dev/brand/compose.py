import json, math
import os

os.chdir(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join("..", "..", "..", "docs", "assets")

g = json.load(open("glyphs.json"))
G = g["glyphs"]
OX, INK, CREAM, SEPIA, STONE = "#7A1F1F", "#1A1612", "#F5F1E8", "#C9C0AE", "#5C544A"


def glyph_path(ch, em, x, y, fill):
    d = G[ch]["d"]
    return f'<path transform="translate({x:.2f} {y:.2f}) scale({em:.4f})" d="{d}" fill="{fill}"/>'


def hexagon(cx, cy, r):
    pts = [
        (cx + r * math.cos(math.radians(a)), cy + r * math.sin(math.radians(a)))
        for a in range(0, 360, 60)
    ]
    return " ".join(f"{x:.2f},{y:.2f}" for x, y in pts)


# ---- standalone mark: U in a flat-top hexagon, 200x200 ----
def mark(u_fill, hex_stroke, size=200, stroke_w=3.0):
    cx = cy = size / 2
    r = size * 0.44
    em = size * 0.66  # cap height 0.75em -> ~0.5*size
    b = G["U"]["bounds"]  # [xmin, ymin(=-overshoot), xmax, ymax(cap)]
    gw = (b[2] - b[0]) * em
    gx = cx - gw / 2 - b[0] * em
    cap = b[3] * em
    baseline = cy + cap / 2
    return (
        f'<polygon points="{hexagon(cx, cy, r)}" fill="none" stroke="{hex_stroke}" stroke-width="{stroke_w}" stroke-linejoin="round"/>'
        + glyph_path("U", em, gx, baseline, u_fill)
    )


def svg(w, h, body, label):
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" aria-label="{label}">\n'
        f"<title>{label}</title>\n{body}\n</svg>\n"
    )


open(os.path.join(ASSETS, "unitares-mark.svg"), "w").write(
    svg(200, 200, mark(OX, INK), "UNITARES mark")
)
open(os.path.join(ASSETS, "unitares-mark-dark.svg"), "w").write(
    svg(200, 200, mark(CREAM, SEPIA), "UNITARES mark")
)
# favicon-scale: heavier hex so it survives 16-32px
open(os.path.join(ASSETS, "unitares-favicon.svg"), "w").write(
    svg(
        64,
        64,
        f'<rect width="64" height="64" rx="8" fill="{CREAM}"/>'
        + mark(OX, OX, size=64, stroke_w=2.2),
        "UNITARES",
    )
)


# ---- wordmark: "Unitares" in Bodoni Moda small caps ----
def wordmark(em, x, baseline, fill, track=0.045):
    parts, cur = [], x
    seq = ["U", "n", "i", "t", "a", "r", "e", "s"]
    kern = {("t", "a"): -0.045, ("U", "n"): -0.01}
    for i, ch in enumerate(seq):
        parts.append(glyph_path(ch, em, cur, baseline, fill))
        adv = G[ch]["adv"] * em + track * em
        if i + 1 < len(seq):
            adv += kern.get((ch, seq[i + 1]), 0) * em
        cur += adv
    return "\n".join(parts), cur - x - track * em


# lockup: mark (96px) + wordmark, height 96
H = 96
em = 76.0  # cap U = 57px, small caps = 38.8px
wm, wmw = wordmark(em, 124, 76.5, INK)
W = math.ceil(124 + wmw + 4)


def lockup(u_fill, hex_stroke, wm_fill):
    body = (
        f'<g transform="translate(0 0) scale({H / 200:.4f})">{mark(u_fill, hex_stroke, stroke_w=4.5)}</g>\n'
        + wordmark(em, 124, 76.5, wm_fill)[0]
    )
    return body


open(os.path.join(ASSETS, "unitares-lockup.svg"), "w").write(
    svg(W, H, lockup(OX, INK, INK), "UNITARES")
)
open(os.path.join(ASSETS, "unitares-lockup-dark.svg"), "w").write(
    svg(W, H, lockup(CREAM, SEPIA, CREAM), "UNITARES")
)
print("lockup size", W, H, "wordmark width", round(wmw, 1))
