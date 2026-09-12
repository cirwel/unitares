"""Render docs/assets/social-preview.png from SocialCard.dc.html.

Headless Google Chrome paints the artboard at 2x, Pillow downsamples to the
1280x640 GitHub social-preview size so text edges stay crisp. Maintainer tool:
nothing in the install or CI path depends on it.

Run from anywhere:  python3 scripts/dev/brand/render_social_preview.py
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
CARD = HERE / "SocialCard.dc.html"
OUT = HERE.parents[2] / "docs" / "assets" / "social-preview.png"
CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "google-chrome",
    "chromium",
)


def chrome() -> str:
    for c in CHROME_CANDIDATES:
        if pathlib.Path(c).is_file() or shutil.which(c):
            return c
    sys.exit(
        "no Chrome/Chromium found; install one or set the path in CHROME_CANDIDATES"
    )


def main() -> None:
    from PIL import (
        Image,
    )  # local import: Pillow is a dev extra, not a runtime dependency

    if not CARD.is_file():
        sys.exit(f"{CARD} missing; run build_artboards.py first")
    raw = HERE / "social-preview-2x.png"
    subprocess.run(
        [
            chrome(),
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--force-device-scale-factor=2",
            "--window-size=1280,640",
            f"--screenshot={raw}",
            CARD.as_uri(),
        ],
        check=True,
        capture_output=True,
    )
    Image.open(raw).convert("RGB").resize((1280, 640), Image.LANCZOS).save(
        OUT, optimize=True
    )
    raw.unlink()
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
