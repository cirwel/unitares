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
MIN_FONT_BYTES = (
    100_000  # the variable TTF is ~162 KB; anything smaller is a truncated download
)


def _fetch(url: str, dest: pathlib.Path) -> None:
    # curl, not urllib: the python.org interpreter ships no CA bundle on this platform.
    # Download beside the target and rename, so an interrupted transfer never
    # leaves a truncated file that a later run would mistake for the font.
    part = dest.with_suffix(dest.suffix + ".part")
    try:
        subprocess.run(
            ["curl", "-sSfL", "--max-time", "60", url, "-o", str(part)], check=True
        )
        part.replace(dest)
    finally:
        part.unlink(missing_ok=True)


def ensure_font() -> pathlib.Path:
    """Return the path to Bodoni Moda, downloading it (with its licence) if absent or truncated."""
    if not FONT.is_file() or FONT.stat().st_size < MIN_FONT_BYTES:
        FONT.parent.mkdir(parents=True, exist_ok=True)
        _fetch(BASE + "BodoniModa%5Bopsz%2Cwght%5D.ttf", FONT)
        _fetch(BASE + "OFL.txt", LICENSE)
    return FONT
