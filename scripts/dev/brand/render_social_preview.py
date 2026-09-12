"""Render docs/assets/social-preview.png, the 1280x640 GitHub share card.

The card is built here as one self-contained HTML page around the lockup in
docs/assets/unitares-lockup.svg, painted by headless Google Chrome at 2x, and
downsampled by Pillow so text edges stay crisp. It carries the README's
tagline and no counts. Maintainer tool: nothing in the install or CI path
depends on it.

Run from anywhere:  python3 scripts/dev/brand/render_social_preview.py
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
ASSETS = HERE.parents[2] / "docs" / "assets"
LOCKUP = ASSETS / "unitares-lockup.svg"
OUT = ASSETS / "social-preview.png"
CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "google-chrome",
    "chromium",
)

TAGLINE = "A federation kernel for accountable AI agents."
SUBLINE = (
    "Identity, claims and evidence, review, outcomes, and reconstruction "
    "across agent runtimes."
)

PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=EB+Garamond:ital,wght@0,400;1,400&family=JetBrains+Mono:wght@400&display=swap">
<style>
  body {{ margin:0; background:#F5F1E8; color:#1A1612; font-family:"EB Garamond",Garamond,Georgia,serif; -webkit-font-smoothing:antialiased; }}
  .card {{ width:1280px; height:640px; box-sizing:border-box; padding:56px 120px 48px; display:flex; flex-direction:column; justify-content:space-between; }}
  .mono {{ font-family:"JetBrains Mono",ui-monospace,Menlo,monospace; font-size:15px; letter-spacing:.18em; text-transform:uppercase; color:#5C544A; }}
  .rule {{ height:1px; background:#C9C0AE; flex:1; }}
  .row {{ display:flex; align-items:center; gap:24px; }}
  .mid {{ display:flex; flex-direction:column; align-items:center; gap:28px; }}
  .tag {{ font-size:44px; line-height:1.2; text-align:center; max-width:900px; }}
  .sub {{ font-size:24px; line-height:1.3; text-align:center; color:#5C544A; font-style:italic; }}
  .foot {{ display:flex; justify-content:space-between; }}
  .foot .mono {{ letter-spacing:.08em; text-transform:none; }}
</style></head>
<body><div class="card">
  <div class="row"><div class="rule"></div><div class="mono">Cirwel Systems · Self-hosted · Apache-2.0</div><div class="rule"></div></div>
  <div class="mid">{lockup}<div class="tag">{tagline}</div><div class="sub">{subline}</div></div>
  <div style="display:flex; flex-direction:column; gap:14px;">
    <div class="rule" style="flex:none;"></div>
    <div class="foot"><div class="mono">github.com/cirwel/unitares</div><div class="mono">doi 10.5281/zenodo.19647159</div><div class="mono">running since November 2025</div></div>
  </div>
</div></body></html>
"""


def chrome() -> str:
    for c in CHROME_CANDIDATES:
        if pathlib.Path(c).is_file() or shutil.which(c):
            return c
    sys.exit(
        "no Chrome/Chromium found; install one or add its path to CHROME_CANDIDATES"
    )


def build_page() -> str:
    svg = LOCKUP.read_text()
    svg = re.sub(
        r'width="\d+" height="\d+"',
        'width="620" style="height:auto;display:block"',
        svg,
        count=1,
    )
    return PAGE.format(lockup=svg, tagline=TAGLINE, subline=SUBLINE)


def main() -> None:
    from PIL import Image  # Pillow is a dev extra, not a runtime dependency

    if not LOCKUP.is_file():
        sys.exit(f"{LOCKUP} missing; run compose.py first")
    page = HERE / "social-card.html"
    raw = HERE / "social-preview-2x.png"
    try:
        page.write_text(build_page())
        subprocess.run(
            [
                chrome(),
                "--headless=new",
                "--disable-gpu",
                "--hide-scrollbars",
                "--force-device-scale-factor=2",
                "--window-size=1280,640",
                "--virtual-time-budget=8000",  # let the web fonts arrive before the paint
                f"--screenshot={raw}",
                page.as_uri(),
            ],
            check=True,
            capture_output=True,
        )
        Image.open(raw).convert("RGB").resize((1280, 640), Image.LANCZOS).save(
            OUT, optimize=True
        )
    finally:
        for tmp in (raw, page):
            tmp.unlink(missing_ok=True)
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
