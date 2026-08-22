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

SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
# Declared by the release entry once the plugin carrying this bundle is cut.
RECUT = re.compile(r"<!--\s*plugin-bundle-recut:\s*(v[\d.]+)\s*-->")

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
    """The version the series declares, or None when it cannot be read.

    None is an indeterminate result, not a benign one. A missing or malformed
    version file used to fall through to "not blocking", so deleting the file
    cleared the gate.
    """
    if relative is None:
        return None
    path = REPO_ROOT / relative
    if not path.exists():
        return None
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)["project"]["version"]
    except (tomllib.TOMLDecodeError, KeyError):
        return None


def _parts(version: str | None) -> tuple[int, int, int] | None:
    if version is None:
        return None
    match = SEMVER.match(version.strip())
    return tuple(int(g) for g in match.groups()) if match else None


def is_forward_bump(declared: str | None, tagged: str | None) -> bool | None:
    """True when declared is strictly newer than tagged; None when unknowable.

    This was raw string inequality, so `0.1` and `0.1.0.0` (equal to `0.1.0`),
    the downgrade `0.0.9`, and the literal `banana` all read as "already
    bumped". A version that is merely DIFFERENT does not mean a consumer can
    reach the change.
    """
    left, right = _parts(declared), _parts(tagged)
    if left is None or right is None:
        return None
    return left > right


def recut_declared() -> str | None:
    """The plugin release the changelog entry says carries this bundle."""
    changelog = REPO_ROOT / "docs" / "CHANGELOG.md"
    version = VERSION_FILE.read_text(encoding="utf-8").strip()
    if not changelog.exists():
        return None
    text = changelog.read_text(encoding="utf-8")
    start = text.find(f"## [{version}]")
    if start == -1:
        return None
    nxt = text.find("\n## [", start + 1)
    section = text[start:] if nxt == -1 else text[start:nxt]
    match = RECUT.search(section)
    return match.group(1) if match else None


def is_release_tree() -> bool:
    """True when VERSION names a release this tree has not actually shipped.

    Membership by NAME is not enough: a tag with the right name on a commit this
    tree never saw is an abandoned tagging attempt, and standing the gate down on
    it would let an operator mistake switch the check off.
    """
    version = VERSION_FILE.read_text(encoding="utf-8").strip()
    tags = _git("for-each-ref", "--format=%(refname:short)", "refs/tags/v*").splitlines()
    if f"v{version}" not in tags:
        return True
    commit = _git("rev-list", "-n", "1", f"v{version}")
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), "merge-base", "--is-ancestor", commit, "HEAD"],
        capture_output=True,
    ).returncode != 0


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
    forward = is_forward_bump(declared, tagged) if series["version_file"] else None

    return {
        "series": series,
        "tag": tag,
        "commits": commits,
        "declared": declared,
        "tagged": tagged,
        # Blocking unless the declared version is demonstrably NEWER than the
        # published one. "Not provably forward" covers equal, older, malformed,
        # and unreadable — every one of which leaves a consumer unable to reach
        # the change, and every one of which used to pass.
        "unreachable": series["version_file"] is not None and forward is not True,
        "indeterminate": series["version_file"] is not None and forward is None,
    }


def main() -> int:
    list_only = "--list" in sys.argv
    release = is_release_tree()

    # No tags for a series is "cannot measure", not "nothing to report". The
    # previous version returned the same clean exit for a repository with tags
    # fetched and one without.
    # A series whose paths do not exist here is not applicable; a series whose
    # paths DO exist but has no visible tag is unmeasurable, which is different
    # from clean.
    present = [s for s in SERIES
               if any((REPO_ROOT / path).exists() for path in s["paths"])]
    missing = [s["name"] for s in present
               if latest_tag(s["tag_glob"], s["tag_pattern"]) is None]
    if missing:
        print(f"[series-drift] no tags visible for: {', '.join(missing)} — the "
              "baseline cannot be established. Check out with fetch-depth: 0.",
              file=sys.stderr)
        return 0 if list_only else 1

    findings = [f for f in (inspect(s) for s in present) if f]
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
        if series["version_file"] is not None:
            if finding["indeterminate"]:
                state = "unreadable or not a semantic version"
            elif finding["unreachable"]:
                state = f"not newer than the published {finding['tagged']}"
            else:
                state = f"newer than the published {finding['tagged']}"
            print(f"    declared version: {finding['declared']} ({state})")

        if finding["unreachable"]:
            print(f"    {series['consumer']} cannot reach these changes")
            print(f"    resolution: {series['resolution']}")
            blocking += 1
        elif series["version_file"] is None:
            # This script cannot see the plugin's tags, so it cannot observe the
            # re-cut. It reads a declaration instead — which is the only kind of
            # clearance available for a fact that lives in another repository.
            # Without one the gate was unsatisfiable: the printed remediation
            # said "record the decision in the release notes" and nothing read
            # any such record, so a correctly-mirrored release stayed red.
            recut = recut_declared()
            if recut:
                print(f"    declared as carried by plugin {recut}")
            else:
                print(f"    this tree ships a different bundle than {finding['tag']} did")
                print(f"    resolution: {series['resolution']}, then declare it "
                      f"in the release entry:")
                print(f"        <!-- plugin-bundle-recut: vX.Y.Z -->")
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
    print("Resolve it, or declare the resolution where the script can read it.")
    return 0 if list_only else 1


if __name__ == "__main__":
    sys.exit(main())
