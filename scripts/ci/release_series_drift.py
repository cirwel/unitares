#!/usr/bin/env python3
"""Report artifacts whose own version series has drifted from the repository.

This repository carries several release series that do not move with `VERSION`:
the Python SDK under `agents/sdk/` has its own `sdk-v*` tags and its own PyPI
version, and `skills/` is mirrored byte-for-byte into the separately tagged
`unitares-governance-plugin`.

`version_manager.py` guards the server version across seven files, thoroughly.
Nothing watched the other series. So `agents/sdk/` accumulated four commits and
a new public method across two server releases while PyPI still served 0.1.0,
and the skills bundle shipped guidance the server had already changed. Neither
was reported, because no check was looking at the seam between a directory and
a tag that lives outside `VERSION`'s reach.

Blocking on a release tree (VERSION not yet tagged), advisory everywhere else:
a drifted series is a decision the release owner should make on purpose, and a
warning nobody is required to read is the failure mode this exists to fix.

Usage:
    python scripts/ci/release_series_drift.py           # gate on a release tree
    python scripts/ci/release_series_drift.py --list    # report, always exit 0
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
VERSION_FILE = REPO_ROOT / "VERSION"

SERIES = [
    {
        "name": "unitares-sdk",
        "paths": ["agents/sdk"],
        "tag_pattern": r"sdk-v\d+\.\d+\.\d+",
        "tag_glob": "refs/tags/sdk-v*",
        "tag_prefix": "sdk-v",
        "version_file": "agents/sdk/pyproject.toml",
        "consumer": "PyPI",
        "resolution": (
            "bump `version` in agents/sdk/pyproject.toml, then tag `sdk-v<new>` "
            "at the merged commit (publish-sdk.yml refuses a mismatched tag)"
        ),
    },
    {
        # The plugin's tags live in another repository, so this check cannot
        # know the mirror's state and does not claim to. What it can say is
        # local and exact: the bundle this tree ships is not the bundle the
        # last server release shipped, so whatever the plugin mirrored then is
        # no longer current. Confirming the re-cut is the reader's job.
        "name": "plugin skills bundle",
        "paths": ["skills"],
        "tag_pattern": r"v\d+\.\d+\.\d+",
        "tag_glob": "refs/tags/v*",
        "tag_prefix": "v",
        "version_file": None,
        "consumer": "unitares-governance-plugin",
        "resolution": (
            "re-run scripts/dev/skills_manifest.py, mirror skills/ plus the "
            "manifest into the plugin, and cut a plugin release carrying it"
        ),
    },
]


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def latest_tag(glob: str, pattern: str) -> str | None:
    out = _git("for-each-ref", "--sort=-creatordate",
               "--format=%(refname:short)", glob)
    for tag in out.splitlines():
        if re.fullmatch(pattern, tag):
            return tag
    return None


def declared_version(relative: str | None) -> str | None:
    if relative is None:
        return None
    path = REPO_ROOT / relative
    if not path.exists():
        return None
    with path.open("rb") as handle:
        return tomllib.load(handle)["project"]["version"]


def is_release_tree() -> bool:
    """True when VERSION names a release that has not been tagged yet."""
    version = VERSION_FILE.read_text(encoding="utf-8").strip()
    tags = _git("for-each-ref", "--format=%(refname:short)", "refs/tags/v*").splitlines()
    return f"v{version}" not in tags


def inspect(series: dict) -> dict | None:
    tag = latest_tag(series["tag_glob"], series["tag_pattern"])
    if tag is None:
        return None

    commits = _git("log", "--no-merges", "--format=%h %s",
                   f"{tag}..HEAD", "--", *series["paths"]).splitlines()
    if not commits:
        return None

    declared = declared_version(series["version_file"])
    tagged = tag[len(series["tag_prefix"]):] if series["version_file"] else None

    return {
        "series": series,
        "tag": tag,
        "commits": commits,
        "declared": declared,
        # A declared version still equal to the published tag is the blocking
        # shape: the code moved and the number did not, so consumers cannot
        # reach the change at all. A version already bumped is just waiting for
        # its tag, which is the normal mid-release state.
        "unreachable": declared is not None and declared == tagged,
    }


def main() -> int:
    list_only = "--list" in sys.argv
    release = is_release_tree()

    findings = [f for f in (inspect(s) for s in SERIES) if f]
    if not findings:
        print("[series-drift] every tracked series is level with its tag")
        return 0

    blocking = 0
    for finding in findings:
        series = finding["series"]
        print()
        print(f"[series-drift] {series['name']}: {len(finding['commits'])} "
              f"commit(s) since {finding['tag']}")
        for line in finding["commits"]:
            print(f"    {line}")
        if finding["declared"] is not None:
            state = "UNCHANGED" if finding["unreachable"] else "already bumped"
            print(f"    declared version: {finding['declared']} ({state})")
        if finding["unreachable"]:
            print(f"    {series['consumer']} cannot reach these changes: the "
                  f"version has not moved")
            print(f"    resolution: {series['resolution']}")
            blocking += 1
        elif series["version_file"] is None:
            print(f"    this tree ships a different bundle than {finding['tag']} did — "
                  f"confirm {series['consumer']} was re-cut after these commits")
            print(f"    resolution: {series['resolution']}")
            blocking += 1

    print()
    if not blocking:
        print("[series-drift] advisory only — every drifted series has already "
              "moved its version")
        return 0

    if not release:
        print(f"[series-drift] advisory: {blocking} series needs attention. "
              "This becomes blocking on a release tree.")
        return 0

    print(f"[series-drift] {blocking} series needs a decision before this "
          "release is tagged.")
    print("Resolve it, or record the decision in the release notes so the gap "
          "ships as a stated known limit rather than as a surprise.")
    return 0 if list_only else 1


if __name__ == "__main__":
    sys.exit(main())
