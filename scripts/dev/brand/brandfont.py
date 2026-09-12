"""Fetch the OFL Bodoni Moda variable font used to cut the UNITARES mark.

The TTF is not committed (162 KB binary, and the OFL text travels with it);
it is downloaded from the google/fonts repository on first use into
scripts/dev/brand/fonts/, which is gitignored. No metered service is involved.
"""

from __future__ import annotations

import pathlib
import subprocess

HERE = pathlib.Path(__file__).resolve().parent
FONT = HERE / "fonts" / "BodoniModa.ttf"
LICENSE = HERE / "fonts" / "OFL.txt"
BASE = "https://raw.githubusercontent.com/google/fonts/main/ofl/bodonimoda/"


def ensure_font() -> pathlib.Path:
    """Return the path to Bodoni Moda, downloading it (with its licence) if absent."""
    if not FONT.is_file():
        FONT.parent.mkdir(parents=True, exist_ok=True)
        # curl, not urllib: the python.org interpreter ships no CA bundle on this platform.
        for url, dest in (
            (BASE + "BodoniModa%5Bopsz%2Cwght%5D.ttf", FONT),
            (BASE + "OFL.txt", LICENSE),
        ):
            subprocess.run(
                ["curl", "-sSfL", "--max-time", "60", url, "-o", str(dest)], check=True
            )
    return FONT
