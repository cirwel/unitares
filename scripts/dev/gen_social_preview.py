#!/usr/bin/env python3
"""Render the GitHub social-preview card (1280x640 PNG).

Matches docs/assets/hero.svg: GitHub-dark gradient, EISV diamond motif,
Unitares wordmark. Rendered at 2x and downsampled for crisp edges.

Run:  python3 scripts/dev/gen_social_preview.py
Out:  docs/assets/social-preview.png
"""
from __future__ import annotations

from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

SS = 2  # supersample factor
W, H = 1280 * SS, 640 * SS
OUT = Path(__file__).resolve().parents[2] / "docs/assets/social-preview.png"

FONT_DIR = "/usr/share/fonts/truetype/liberation"
BOLD = f"{FONT_DIR}/LiberationSans-Bold.ttf"
REG = f"{FONT_DIR}/LiberationSans-Regular.ttf"


def font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size * SS)


# palette (GitHub dark)
BG_TOP = (13, 17, 23)      # #0d1117
BG_BOT = (22, 27, 34)      # #161b22
BORDER = (48, 54, 61)      # #30363d
INK = (230, 237, 243)      # #e6edf3
SUBTLE = (139, 148, 158)   # #8b949e
FAINT = (72, 79, 88)       # #484f58
ACCENT = (88, 166, 255)    # #58a6ff
LINE = (61, 70, 85)        # #3d4655
LINE_FAINT = (43, 49, 58)  # #2b313a
NODE = {
    "E": (63, 185, 80),    # #3fb950
    "I": (88, 166, 255),   # #58a6ff
    "S": (210, 153, 34),   # #d29922
    "V": (139, 92, 246),   # #8b5cf6
}


def lerp(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


img = Image.new("RGB", (W, H), BG_TOP)
d = ImageDraw.Draw(img)

# vertical gradient
for y in range(H):
    d.line([(0, y), (W, y)], fill=lerp(BG_TOP, BG_BOT, y / H))

# inset rounded border (card look)
m = 22 * SS
d.rounded_rectangle([m, m, W - m, H - m], radius=20 * SS, outline=BORDER, width=2 * SS)


def tracked(draw, xy, text, fnt, fill, tracking):
    """Draw text with letter-spacing; return total advance width."""
    x, y = xy
    track = tracking * SS
    for ch in text:
        draw.text((x, y), ch, font=fnt, fill=fill)
        x += draw.textlength(ch, font=fnt) + track
    return x - xy[0] - track


def tracked_width(draw, text, fnt, tracking):
    track = tracking * SS
    return sum(draw.textlength(ch, font=fnt) + track for ch in text) - track


# ---- EISV diamond (right) ----
cx, cy = 1010 * SS, 318 * SS
P = {"E": (0, -92), "I": (-100, 30), "S": (100, 30), "V": (0, 122)}
P = {k: (cx + v[0] * SS, cy + v[1] * SS) for k, v in P.items()}
R = 36 * SS

# orbital ring
d.ellipse([cx - 142 * SS, cy - 110 * SS, cx + 142 * SS, cy + 138 * SS],
          outline=BORDER, width=1 * SS)
# cross axes (faint)
d.line([P["I"], P["S"]], fill=LINE_FAINT, width=1 * SS)
d.line([P["E"], P["V"]], fill=LINE_FAINT, width=1 * SS)
# diamond edges
for a, b in (("E", "I"), ("E", "S"), ("I", "V"), ("S", "V")):
    d.line([P[a], P[b]], fill=LINE, width=2 * SS)

node_lbl = font(BOLD, 30)
cap = font(BOLD, 11)
caption = {"E": "ENERGY", "I": "INTEGRITY", "S": "ENTROPY", "V": "VALENCE"}
for k, (px, py) in P.items():
    d.ellipse([px - R, py - R, px + R, py + R], fill=NODE[k],
              outline=BG_TOP, width=3 * SS)
    bb = d.textbbox((0, 0), k, font=node_lbl)
    d.text((px - (bb[2] - bb[0]) / 2, py - (bb[3] - bb[1]) / 2 - bb[1]), k,
           font=node_lbl, fill=(255, 255, 255))
    label = caption[k]
    lw = tracked_width(d, label, cap, 2)
    ly = py - R - 22 * SS if k == "E" else py + R + 10 * SS
    tracked(d, (px - lw / 2, ly), label, cap, SUBTLE, 2)

# ---- left text block ----
x0 = 80 * SS

# eyebrow
tracked(d, (x0, 138 * SS), "RUNTIME GOVERNANCE FOR AI AGENTS",
        font(BOLD, 19), ACCENT, 3)

# wordmark
tracked(d, (x0, 178 * SS), "UNITARES", font(BOLD, 92), INK, 2)

# tagline (two lines)
tl = font(REG, 36)
d.text((x0, 312 * SS), "Catch an agent going off the rails", font=tl, fill=INK)
d.text((x0, 360 * SS), "— before anything visibly breaks.", font=tl, fill=SUBTLE)

# feature chips (wrap within max width)
chip_f = font(BOLD, 20)
chips = ["Self-relative drift", "Outcome-calibrated confidence", "MCP + HTTP"]
cxp, cyp = x0, 432 * SS
pad = 16 * SS
gap = 12 * SS
maxx = 830 * SS
for c in chips:
    w = d.textlength(c, font=chip_f)
    cw = w + pad * 2
    if cxp + cw > maxx:
        cxp = x0
        cyp += 50 * SS
    h = 38 * SS
    d.rounded_rectangle([cxp, cyp, cxp + cw, cyp + h], radius=h // 2,
                        outline=BORDER, width=2 * SS)
    d.text((cxp + pad, cyp + (h - (chip_f.getbbox(c)[3])) / 2 - chip_f.getbbox(c)[1] / 2),
           c, font=chip_f, fill=SUBTLE)
    cxp += cw + gap

# credibility strip (bottom)
sy = 552 * SS
dot_r = 6 * SS
d.ellipse([x0, sy + 4 * SS, x0 + dot_r * 2, sy + 4 * SS + dot_r * 2], fill=NODE["E"])
strip_f = font(BOLD, 16)
sx = x0 + dot_r * 2 + 12 * SS
sx += tracked(d, (sx, sy), "LIVE SINCE 2025", strip_f, SUBTLE, 2) + 26 * SS
tracked(d, (sx, sy), "·", strip_f, FAINT, 2)
sx += 26 * SS
sx += tracked(d, (sx, sy), "3.7M+ GOVERNANCE EVENTS", strip_f, SUBTLE, 2) + 26 * SS
tracked(d, (sx, sy), "·", strip_f, FAINT, 2)
sx += 26 * SS
tracked(d, (sx, sy), "APACHE-2.0", strip_f, SUBTLE, 2)

img = img.resize((1280, 640), Image.LANCZOS)
OUT.parent.mkdir(parents=True, exist_ok=True)
img.save(OUT, "PNG")
print(f"wrote {OUT} ({OUT.stat().st_size // 1024} KB)")
