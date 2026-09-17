#!/usr/bin/env python3
"""Check the GitHub repository description against the canonical tagline.

The repository description is a reader-facing positioning surface -- it is the
one line shown under the repo name, in search results, and in social unfurls --
but it is not a file, so no repository-scanning check can see it. It drifted to
a retired tagline and stayed there through several positioning passes because
nothing looked.

This is deliberately a separate script rather than a check inside
``check_doc_health.py``: it needs network and GitHub auth, and the doc-health
gate must keep working offline and in CI without a token.

Exit codes:
    0  matches, or the check could not run (no ``gh``, no auth, no network)
    1  the description is set and does not carry the canonical tagline

Not-run is deliberately *not* a failure. A check that fails closed on a missing
token would be red on every fork and in every offline install, which trains
people to ignore it. It prints what it did so a skip is visible rather than
silent -- an unverified surface reports as unverified, never as passing.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _canonical_tagline() -> str:
    """Read the tagline from the single place that declares it."""
    spec = importlib.util.spec_from_file_location(
        "_check_doc_drift_tagline",
        REPO_ROOT / "scripts" / "diagnostics" / "check_doc_drift.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _label, alternatives = module.CANONICAL_TAGLINE
    return alternatives[0]


def _run(args: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return proc.returncode, (proc.stdout or proc.stderr).strip()


def main() -> int:
    tagline = _canonical_tagline()

    if shutil.which("gh") is None:
        print("SKIP  repo description: the GitHub CLI is not installed")
        return 0

    code, out = _run(["gh", "repo", "view", "--json", "description,nameWithOwner"])
    if code != 0:
        first_line = out.splitlines()[0] if out else "unknown error"
        print(f"SKIP  repo description: could not reach GitHub ({first_line})")
        return 0

    try:
        payload = json.loads(out)
    except json.JSONDecodeError:
        print("SKIP  repo description: unexpected response from the GitHub CLI")
        return 0

    name = payload.get("nameWithOwner") or "this repository"
    description = (payload.get("description") or "").strip()

    if not description:
        print(f"SKIP  repo description: {name} has no description set")
        return 0

    if tagline.casefold() in " ".join(description.split()).casefold():
        print(f"OK    repo description: {name} carries the canonical tagline")
        return 0

    print(f"FAIL  repo description: {name} does not carry the canonical tagline")
    print(f"      expected to contain: {tagline}")
    print(f"      actually reads:      {description}")
    print("      fix with:")
    print(f'        gh repo edit {name} --description "{tagline}. ..."')
    return 1


if __name__ == "__main__":
    sys.exit(main())
