import math
import os

os.chdir(os.path.dirname(os.path.abspath(__file__)))
from brandfont import ensure_font  # noqa: E402
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.pens.boundsPen import BoundsPen

inst = instantiateVariableFont(
    TTFont(ensure_font()), {"opsz": 11, "wght": 800}
)  # text optical size: sturdier hairlines
gs = inst.getGlyphSet()
upm = inst["head"].unitsPerEm
g = gs[inst.getBestCmap()[ord("U")]]
pen = SVGPathPen(gs)
g.draw(TransformPen(pen, (1 / upm, 0, 0, -1 / upm, 0, 0)))
bp = BoundsPen(gs)
g.draw(bp)
b = [v / upm for v in bp.bounds]
OX, INK, CREAM = "#7A1F1F", "#1A1612", "#F5F1E8"


def hexagon(cx, cy, r):
    return " ".join(
        f"{cx + r * math.cos(math.radians(a)):.2f},{cy + r * math.sin(math.radians(a)):.2f}"
        for a in range(0, 360, 60)
    )


size = 64
cx = cy = 32
r = 27.5
em = size * 0.62
gw = (b[2] - b[0]) * em
gx = cx - gw / 2 - b[0] * em
cap = b[3] * em
base = cy + cap / 2
body = (
    f'<rect width="64" height="64" rx="8" fill="{CREAM}"/>'
    f'<polygon points="{hexagon(cx, cy, r)}" fill="none" stroke="{INK}" stroke-width="2.6" stroke-linejoin="round"/>'
    f'<path transform="translate({gx:.2f} {base:.2f}) scale({em:.3f})" d="{pen.getCommands()}" fill="{OX}"/>'
)
open(
    os.path.join("..", "..", "..", "docs", "assets", "unitares-favicon.svg"), "w"
).write(
    f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64" role="img" aria-label="UNITARES"><title>UNITARES</title>{body}</svg>\n'
)
# the public docs site uses the same tile as its favicon
import shutil  # noqa: E402

shutil.copyfile(
    os.path.join("..", "..", "..", "docs", "assets", "unitares-favicon.svg"),
    os.path.join("..", "..", "..", "docs", "public-site", "favicon.svg"),
)
