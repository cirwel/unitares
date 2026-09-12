import re


def svg_body(path, width=None, cls=""):
    s = open(path).read()
    s = re.sub(r"<\?xml[^>]*>", "", s)
    if width:
        s = re.sub(
            r'width="\d+" height="\d+"',
            f'width="{width}" style="height:auto;display:block"',
            s,
            count=1,
        )
    return s.strip()


LOCK_L = svg_body("unitares-lockup.svg", 420)
LOCK_D = svg_body("unitares-lockup-dark.svg", 420)
MARK_L = svg_body("unitares-mark.svg", 200)
MARK_D = svg_body("unitares-mark-dark.svg", 200)
FAV = svg_body("unitares-favicon.svg", 64)
WM_ONLY = open("unitares-lockup.svg").read()

FONTS = '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=EB+Garamond:ital,wght@0,400;0,500;1,400&family=JetBrains+Mono:wght@400;500&family=Bodoni+Moda:opsz,wght@6..96,400;6..96,500&display=swap">'


def page(style, body, extra_head=""):
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <script src="./support.js"></script>
</head>
<body>
<x-dc>
<helmet>
  {extra_head}
  <style>
{style}
  </style>
</helmet>
{body}
</x-dc>
</body>
</html>
"""


# ---------------- GitHub README header, light + dark ----------------
def readme(theme):
    d = theme == "dark"
    bg = "#0d1117" if d else "#ffffff"
    fg = "#f0f6fc" if d else "#1f2328"
    muted = "#9198a1" if d else "#59636e"
    line = "#3d444d" if d else "#d1d9e0"
    link = "#4493f8" if d else "#0969da"
    boxbg = "#0d1117" if d else "#ffffff"
    tabbg = "#151b23" if d else "#f6f8fa"
    code = "#2a313c" if d else "#eff1f3"
    zebra = "#151b23" if d else "#f6f8fa"
    lock = LOCK_D if d else LOCK_L
    style = f"""
body {{ margin:0; background:{bg}; color:{fg}; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans",Helvetica,Arial,sans-serif; font-size:16px; line-height:1.5; -webkit-font-smoothing:antialiased; }}
a {{ color:{link}; text-decoration:none; }} a:hover {{ text-decoration:underline; }}
.box {{ border:1px solid {line}; border-radius:6px; background:{boxbg}; }}
.tabs {{ display:flex; align-items:center; gap:8px; height:46px; padding:0 8px; border-bottom:1px solid {line}; background:{tabbg}; border-radius:6px 6px 0 0; font-size:14px; }}
.tab {{ display:flex; align-items:center; gap:8px; padding:0 8px; height:100%; color:{muted}; border-bottom:2px solid transparent; }}
.tab.on {{ color:{fg}; border-bottom-color:#f78166; font-weight:600; }}
.md {{ padding:32px; }}
.md p {{ margin:0 0 16px; }}
.md h3 {{ font-size:1.25em; font-weight:600; line-height:1.25; margin:24px 0 16px; }}
.md h2 {{ font-size:1.5em; font-weight:600; line-height:1.25; margin:24px 0 16px; padding-bottom:.3em; border-bottom:1px solid {line}; }}
.md hr {{ height:.25em; padding:0; margin:24px 0; background:{line}; border:0; }}
.md code {{ background:{code}; padding:.2em .4em; font-size:85%; border-radius:6px; font-family:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace; }}
.md table {{ border-collapse:collapse; width:100%; font-size:16px; margin:0 0 16px; }}
.md th, .md td {{ border:1px solid {line}; padding:6px 13px; text-align:left; vertical-align:top; }}
.md th {{ font-weight:600; }} .md tr:nth-child(2n) td {{ background:{zebra}; }}
.center {{ display:flex; flex-direction:column; align-items:center; text-align:center; gap:0; }}
.badges {{ display:flex; gap:6px; justify-content:center; flex-wrap:wrap; margin:0 0 12px; }}
.badge {{ display:flex; height:20px; font:11px Verdana,Geneva,"DejaVu Sans",sans-serif; line-height:20px; color:#fff; }}
.badge span {{ padding:0 5px; }}
.badge .k {{ background:#1A1612; }} .badge .v {{ background:#5C544A; }}
.badge.ci .k {{ background:#555; }} .badge.ci .v {{ background:#4c1; }}
.badge.doi .v {{ background:#7A1F1F; }}
.nav {{ display:flex; gap:10px; justify-content:center; margin:0 0 16px; }}
.nav .dot {{ color:{muted}; }}
"""
    body = f"""
<div style="width:960px; min-height:820px; padding:24px 0 0 0; background:{bg}; box-sizing:border-box;">
<div class="box" style="margin:0 24px;">
  <div class="tabs">
    <div class="tab on"><svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor"><path d="M0 1.75A.75.75 0 0 1 .75 1h4.253c1.227 0 2.317.59 3 1.501A3.743 3.743 0 0 1 11.006 1h4.245a.75.75 0 0 1 .75.75v10.5a.75.75 0 0 1-.75.75h-4.507a2.25 2.25 0 0 0-1.591.659l-.622.621a.75.75 0 0 1-1.06 0l-.622-.621A2.25 2.25 0 0 0 5.258 13H.75a.75.75 0 0 1-.75-.75Zm7.251 10.324.004-5.073-.002-2.253A2.25 2.25 0 0 0 5.003 2.5H1.5v9h3.757a3.75 3.75 0 0 1 1.994.574ZM8.755 4.75l-.004 7.322a3.752 3.752 0 0 1 1.992-.572H14.5v-9h-3.495a2.25 2.25 0 0 0-2.25 2.25Z"></path></svg>README</div>
    <div class="tab"><svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor"><path d="M8.75.75V2h.985c.304 0 .603.08.867.231l1.29.736c.038.022.08.033.124.033h2.234a.75.75 0 0 1 0 1.5h-.427l2.111 4.692a.75.75 0 0 1-.154.838l-.53-.53.529.531-.001.002-.002.002-.006.006-.006.005-.01.01-.045.04c-.21.176-.441.327-.686.45C14.556 10.78 13.88 11 13 11a4.498 4.498 0 0 1-2.023-.454 3.544 3.544 0 0 1-.686-.45l-.045-.04-.016-.015-.006-.006-.004-.004v-.001a.75.75 0 0 1-.154-.838L12.178 4.5h-.162c-.305 0-.604-.079-.868-.231l-1.29-.736a.245.245 0 0 0-.124-.033H8.75V13h2.5a.75.75 0 0 1 0 1.5h-6.5a.75.75 0 0 1 0-1.5h2.5V3.5h-.984a.245.245 0 0 0-.124.033l-1.289.737c-.265.15-.564.23-.869.23h-.162l2.112 4.692a.75.75 0 0 1-.154.838l-.53-.53.529.531-.001.002-.002.002-.006.006-.016.015-.045.04c-.21.176-.441.327-.686.45C4.556 10.78 3.88 11 3 11a4.498 4.498 0 0 1-2.023-.454 3.544 3.544 0 0 1-.686-.45l-.045-.04-.016-.015-.006-.006-.004-.004v-.001a.75.75 0 0 1-.154-.838L2.178 4.5H1.75a.75.75 0 0 1 0-1.5h2.234a.249.249 0 0 0 .125-.033l1.288-.737c.265-.15.564-.23.869-.23h.984V.75a.75.75 0 0 1 1.5 0Z"></path></svg>Apache-2.0 license</div>
  </div>
  <div class="md">
    <div class="center">
      {lock}
      <h3>A federation kernel for accountable AI agents.</h3>
      <p>Identity, claims and evidence, review, outcomes, and reconstruction across agent runtimes.</p>
    </div>
    <p>UNITARES is a self-hosted MCP server that gives agents a shared, attributed record while they keep their own reasoning loops and tools. A new process can recover durable claims earlier processes stored, inspect the retained evidence and disagreement, and continue the work with its own identity.</p>
    <p><strong>Status:</strong> v2.22.0. Running continuously since November 2025.</p>
    <div class="badges">
      <div class="badge ci"><span class="k">tests</span><span class="v">passing</span></div>
      <div class="badge"><span class="k">python</span><span class="v">3.12+</span></div>
      <div class="badge"><span class="k">license</span><span class="v">Apache 2.0</span></div>
      <div class="badge doi"><span class="k">DOI</span><span class="v">10.5281/zenodo.19647159</span></div>
    </div>
    <div class="nav"><a href="#">Quickstart</a><span class="dot">·</span><a href="#">Evidence and limits</a><span class="dot">·</span><a href="#">Docs</a><span class="dot">·</span><a href="#">Reviewer Guide</a></div>
    <hr>
    <h2>What it does</h2>
    <table>
      <tr><th style="width:38%">What you get</th><th>The mechanism</th></tr>
      <tr><td><strong>Identity</strong> — who made the claim?</td><td><code>start_session(force_new=true)</code> binds a fresh process; retain <code>client_session_id</code> for later calls. Real lineage links inherited work, not authority or sameness.</td></tr>
      <tr><td><strong>Claims and evidence</strong> — what was asserted, and what supports it?</td><td><code>sync_state</code> submits a work report from which durable state is derived. <code>store_finding</code> and <code>update_finding</code> retain durable attributed claims and corrections.</td></tr>
    </table>
  </div>
</div>
</div>
"""
    return page(style, body)


open("Main.dc.html", "w").write(readme("light"))
open("ReadmeDark.dc.html", "w").write(readme("dark"))

# ---------------- Social card 1280x640 ----------------
card_style = """
body { margin:0; background:#F5F1E8; color:#1A1612; font-family:"EB Garamond",Garamond,Georgia,serif; -webkit-font-smoothing:antialiased; }
a { color:#7A1F1F; } a:hover { color:#5a1414; }
.mono { font-family:"JetBrains Mono",ui-monospace,Menlo,monospace; font-size:15px; letter-spacing:.18em; text-transform:uppercase; color:#5C544A; }
.rule { height:1px; background:#C9C0AE; flex:1; }
"""
card_body = f"""
<div style="width:1280px; height:640px; box-sizing:border-box; padding:56px 120px 48px; display:flex; flex-direction:column; justify-content:space-between; background:#F5F1E8;">
  <div style="display:flex; align-items:center; gap:24px;">
    <div class="rule"></div>
    <div class="mono">Cirwel Research · Self-hosted · Apache-2.0</div>
    <div class="rule"></div>
  </div>
  <div style="display:flex; flex-direction:column; align-items:center; gap:28px;">
    {svg_body("unitares-lockup.svg", 620)}
    <div style="font-size:44px; line-height:1.2; text-align:center; max-width:900px; text-wrap:pretty;">A federation kernel for accountable AI agents.</div>
    <div style="font-size:24px; line-height:1.3; text-align:center; color:#5C544A; font-style:italic;">Identity, claims and evidence, review, outcomes, and reconstruction across agent runtimes.</div>
  </div>
  <div style="display:flex; flex-direction:column; gap:14px;">
    <div class="rule" style="flex:none;"></div>
    <div style="display:flex; justify-content:space-between;" class="mono">
      <div style="letter-spacing:.08em; text-transform:none;">github.com/cirwel/unitares</div>
      <div style="letter-spacing:.08em; text-transform:none;">doi 10.5281/zenodo.19647159</div>
      <div style="letter-spacing:.08em; text-transform:none;">running since November 2025</div>
    </div>
  </div>
</div>
"""
open("SocialCard.dc.html", "w").write(page(card_style, card_body, FONTS))

# ---------------- Mark directions ----------------
cap_style = """
body { margin:0; background:#F5F1E8; color:#1A1612; font-family:"EB Garamond",Garamond,Georgia,serif; -webkit-font-smoothing:antialiased; }
a { color:#7A1F1F; } a:hover { color:#5a1414; }
.stage { height:260px; display:flex; align-items:center; justify-content:center; }
.name { font-family:"JetBrains Mono",ui-monospace,Menlo,monospace; font-size:12px; letter-spacing:.16em; text-transform:uppercase; color:#5C544A; }
.t { font-size:22px; line-height:1.2; }
.p { font-size:16px; line-height:1.4; color:#1A1612; }
.p em { color:#5C544A; }
"""


def direction(name, title, stage, why, tradeoff):
    body = f"""
<div style="width:420px; height:520px; box-sizing:border-box; padding:28px 32px; background:#F5F1E8; display:flex; flex-direction:column; gap:14px;">
  <div class="name">{name}</div>
  <div class="stage">{stage}</div>
  <div class="t">{title}</div>
  <div class="p">{why}</div>
  <div class="p"><em>Tradeoff.</em> {tradeoff}</div>
</div>
"""
    return page(cap_style, body, FONTS)


stage_a = f'<div style="display:flex; gap:28px; align-items:flex-end;">{MARK_L}<div style="display:flex; flex-direction:column; gap:10px; align-items:center;">{FAV}<div class="name" style="font-size:10px;">favicon cut</div></div></div>'

import math


def hexpts(cx, cy, r):
    return " ".join(
        f"{cx + r * math.cos(math.radians(a)):.2f},{cy + r * math.sin(math.radians(a)):.2f}"
        for a in range(0, 360, 60)
    )


# Direction B: kernel hex with five distinct runtimes on hairlines
shapes = []
for i, kind in enumerate(["circle", "square", "tri", "diamond", "hex"]):
    a = math.radians(-90 + i * 72)
    x = 100 + 78 * math.cos(a)
    y = 100 + 78 * math.sin(a)
    shapes.append(
        f'<line x1="100" y1="100" x2="{x:.1f}" y2="{y:.1f}" stroke="#C9C0AE" stroke-width="1.5"/>'
    )
for i, kind in enumerate(["circle", "square", "tri", "diamond", "hex"]):
    a = math.radians(-90 + i * 72)
    x = 100 + 78 * math.cos(a)
    y = 100 + 78 * math.sin(a)
    s = 9
    if kind == "circle":
        shapes.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{s}" fill="#F5F1E8" stroke="#1A1612" stroke-width="2"/>'
        )
    elif kind == "square":
        shapes.append(
            f'<rect x="{x - s:.1f}" y="{y - s:.1f}" width="{2 * s}" height="{2 * s}" fill="#F5F1E8" stroke="#1A1612" stroke-width="2"/>'
        )
    elif kind == "tri":
        shapes.append(
            f'<polygon points="{x:.1f},{y - s - 2:.1f} {x + s + 1:.1f},{y + s - 1:.1f} {x - s - 1:.1f},{y + s - 1:.1f}" fill="#F5F1E8" stroke="#1A1612" stroke-width="2" stroke-linejoin="round"/>'
        )
    elif kind == "diamond":
        shapes.append(
            f'<polygon points="{x:.1f},{y - s - 2:.1f} {x + s + 2:.1f},{y:.1f} {x:.1f},{y + s + 2:.1f} {x - s - 2:.1f},{y:.1f}" fill="#F5F1E8" stroke="#1A1612" stroke-width="2" stroke-linejoin="round"/>'
        )
    else:
        shapes.append(
            f'<polygon points="{hexpts(x, y, s + 2)}" fill="#F5F1E8" stroke="#1A1612" stroke-width="2" stroke-linejoin="round"/>'
        )
stage_b = f'<svg width="200" height="200" viewBox="0 0 200 200">{"".join(shapes)}<polygon points="{hexpts(100, 100, 30)}" fill="#7A1F1F"/><polygon points="{hexpts(100, 100, 38)}" fill="none" stroke="#7A1F1F" stroke-width="1.5"/></svg>'

# Direction C: wordmark only between hairline rules (use the wordmark part of the lockup: crop viewBox)
wm = re.sub(
    r'viewBox="0 0 493 96" width="493" height="96"',
    'viewBox="118 0 375 96" width="340" style="height:auto;display:block"',
    open("unitares-lockup.svg").read(),
)
wm = re.sub(r'<g transform="translate\(0 0\).*?</g>', "", wm, flags=re.S)
stage_c = f'<div style="display:flex; flex-direction:column; gap:18px; align-items:center; width:100%;"><div style="height:1px; background:#C9C0AE; width:100%;"></div>{wm}<div style="height:1px; background:#C9C0AE; width:100%;"></div></div>'

open("DirectionA.dc.html", "w").write(
    direction(
        "Direction A · leading",
        "Family mark: the U in the CIRWEL hex",
        stage_a,
        "The hex is the publisher's device already on cirwel.org; the U is the product initial, cut from Bodoni Moda like the illuminated C. Typographic, not illustrative, and wordless, so it outlives the tagline.",
        "Bodoni's hairline stem vanishes below about 24px, so the favicon is a separate heavier cut (shown).",
    )
)
open("DirectionB.dc.html", "w").write(
    direction(
        "Direction B",
        "Kernel and runtimes",
        stage_b,
        "Draws the federation claim literally: one operator-controlled kernel, five independent runtimes of different shapes attached by hairlines.",
        "Reads as a generic hub-and-spoke network icon, and it is an illustration, which the house rule (ornament must carry information) discourages.",
    )
)
open("DirectionC.dc.html", "w").write(
    direction(
        "Direction C",
        "Wordmark only",
        stage_c,
        "The site's apparatus-over-illustration stance taken literally: the name in small caps between hairline rules, no device at all. Nothing to age.",
        "No favicon or avatar. Nothing to recognise at small sizes or beside other projects.",
    )
)

import json

json.dump(
    {
        "artboards": [
            {
                "file": "Main.dc.html",
                "title": "README header · GitHub light",
                "x": 0,
                "y": 0,
                "w": 960,
                "h": 820,
            },
            {
                "file": "ReadmeDark.dc.html",
                "title": "README header · GitHub dark",
                "x": 1060,
                "y": 0,
                "w": 960,
                "h": 820,
            },
            {
                "file": "SocialCard.dc.html",
                "title": "Social preview · 1280×640",
                "x": 0,
                "y": 980,
                "w": 1280,
                "h": 640,
            },
            {"file": "DirectionA.dc.html", "x": 0, "y": 1780, "w": 420, "h": 520},
            {"file": "DirectionB.dc.html", "x": 520, "y": 1780, "w": 420, "h": 520},
            {"file": "DirectionC.dc.html", "x": 1040, "y": 1780, "w": 420, "h": 520},
        ],
        "annotations": [
            {
                "id": "brief",
                "x": 0,
                "y": -190,
                "w": 620,
                "text": "UNITARES mark and README header.\\nThe decision: one wordless mark in the CIRWEL house register (cream, oxblood, Bodoni Moda small caps), the tagline kept as text in the README so it can change without a new image. The live GitHub social card is the June one and no longer matches the README.",
            }
        ],
        "launch": {"view": "canvas"},
    },
    open("canvas.json", "w"),
    indent=1,
)
print("artboards written")
