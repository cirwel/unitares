#!/usr/bin/env python3
"""Regenerate the published-server row of docs/COMPATIBILITY.md from the promotion.

`version_manager.py --update` rewrites only the version cell of that row, so the
Promote Release `pin` job used to carry the previous release's description onto
the new version. For v3.0.0 the pin branch called a major release a "Verified
maintenance release ... preserving the v2.22.0 runtime APIs" and linked the
v2.22.1 promotion run; it was caught and corrected by hand in #2553.

The row now states only what the pin job has itself established: which tag was
verified, what the verification covered, and which run did it. Release-specific
prose (what changed, how to upgrade) lives on the release page the row links,
where the release author wrote it, rather than in a cell a job has to guess at.

Usage:
    python scripts/ci/compat_published_row.py --version 3.0.0 \
        --run-url https://github.com/cirwel/unitares/actions/runs/N \
        --release-url https://github.com/cirwel/unitares/releases/tag/v3.0.0
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPATIBILITY = REPO_ROOT / "docs" / "COMPATIBILITY.md"
ROW_PREFIX = "| Published server/container |"
_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_RUN_URL = re.compile(r"^https://\S+/actions/runs/(\d+)$")


def render_row(version: str, run_url: str, release_url: str) -> str:
    if not _VERSION.match(version):
        raise ValueError(f"not a release version: {version!r}")
    run = _RUN_URL.match(run_url)
    if not run:
        raise ValueError(f"not a workflow run URL: {run_url!r}")
    if not release_url.endswith(f"/releases/tag/v{version}"):
        raise ValueError(f"release URL does not name v{version}: {release_url!r}")
    return (
        f"{ROW_PREFIX} `v{version}` | Verified release. The tag, its published "
        "release page, the server and lease-plane images for linux/amd64 and "
        "linux/arm64, their SPDX SBOMs, and provenance bound to the tag's source "
        "commit were verified before GHCR `latest` moved to this release in "
        f"[Promote Release run {run.group(1)}]({run_url}). What changed and how "
        f"to upgrade: [release notes]({release_url}). |"
    )


def rewrite(text: str, row: str) -> str:
    lines = text.split("\n")
    hits = [i for i, line in enumerate(lines) if line.startswith(ROW_PREFIX)]
    if len(hits) != 1:
        raise ValueError(
            f"expected exactly one published-server row, found {len(hits)}"
        )
    lines[hits[0]] = row
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--version", required=True)
    parser.add_argument("--run-url", required=True)
    parser.add_argument("--release-url", required=True)
    parser.add_argument("--path", type=Path, default=COMPATIBILITY)
    args = parser.parse_args(argv)
    try:
        row = render_row(args.version, args.run_url, args.release_url)
        args.path.write_text(rewrite(args.path.read_text(), row))
    except ValueError as exc:
        print(f"[compat-published-row] {exc}", file=sys.stderr)
        return 1
    print(f"[compat-published-row] {args.path.name}: published server row is v{args.version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
