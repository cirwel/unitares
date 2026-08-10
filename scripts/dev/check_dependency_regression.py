#!/usr/bin/env python3
"""Fail a change that silently lowers an already-merged dependency version.

## The failure this exists to catch

A Dependabot branch opened before its neighbours merged carries a lockfile
generated against the older base. Merging it does the bump it advertises AND
rolls every *other* dependency back to whatever the stale base had — because
relative to that base, the old versions are not a change at all.

Nothing catches this today:

* `git merge` reports **CLEAN**. There is no conflict, because the branch
  never touched those lines relative to its own base. A reviewer looking for
  a conflict marker sees a green PR.
* The PR title and file list are honest and unremarkable — one bump, one or
  two files. The revert is buried in a few hundred lines of regenerated lock.
* `npm ci` succeeds. The downgraded tree is internally consistent; it is just
  older than what master already shipped.

Observed three times in this repo (2026-08): #1499 and #1502 carried it behind
a merge conflict that happened to block them, and #1517 carried it with a clean
merge — the dangerous variant, where only a human noticing the version table
stands between the branch and a silent rollback on master.

## What it does

Compares dependency versions between a base ref and the working tree. Any
version that moves *backwards* fails the check. Upgrades, additions, and
removals all pass — this guards direction only, and deliberately says nothing
about whether a bump is wise.

Manifests covered (skipped silently when absent, so it is safe on any tree):

* ``dashboard/package-lock.json`` — resolved versions, the ones that ship
* ``dashboard/package.json``     — declared ranges

Usage:
    python3 scripts/dev/check_dependency_regression.py --base origin/master
    python3 scripts/dev/check_dependency_regression.py --base HEAD~1 --json
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from typing import Dict, Iterable, Optional, Tuple

REPO_MANIFESTS = ("dashboard/package.json", "dashboard/package-lock.json")

# Leading range operators on a declared dependency (^1.2.3, >=1.2.3, ~1.2.3).
# Stripped before comparison so `^30.0.1 -> ^30.0.0` is seen as the downgrade
# it is, rather than as two opaque strings.
_RANGE_PREFIX = re.compile(r"^[\^~>=<\s]*")
_NUMERIC = re.compile(r"^\d+$")


def _read_at_ref(ref: str, path: str) -> Optional[str]:
    """File contents at a git ref, or None when the path does not exist there."""
    result = subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout


def _read_worktree(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except FileNotFoundError:
        return None


def parse_version(raw: str) -> Optional[Tuple[int, ...]]:
    """Parse a version into comparable integer parts.

    Returns None for anything not plainly numeric — git URLs, ``*``, ``latest``,
    prerelease tags. Unparseable pairs are skipped rather than guessed at: a
    false failure on an exotic version string would train people to ignore this
    check, which costs more than the case it would catch.
    """
    if not raw:
        return None
    cleaned = _RANGE_PREFIX.sub("", str(raw).strip())
    # Drop build/prerelease suffixes: 1.2.3-beta.1 -> 1.2.3
    cleaned = re.split(r"[-+]", cleaned, maxsplit=1)[0]
    if not cleaned:
        return None
    parts = cleaned.split(".")
    if not all(_NUMERIC.match(part) for part in parts):
        return None
    return tuple(int(part) for part in parts)


def _lock_versions(content: str) -> Dict[str, str]:
    """Resolved version per package from an npm lockfile (v2/v3 `packages`)."""
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return {}
    versions: Dict[str, str] = {}
    for path, package in (data.get("packages") or {}).items():
        if not path or not isinstance(package, dict):
            continue  # "" is the root project, which has no version of its own
        version = package.get("version")
        if isinstance(version, str):
            versions[path.removeprefix("node_modules/")] = version
    return versions


def _manifest_versions(content: str) -> Dict[str, str]:
    """Declared range per dependency from a package.json."""
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return {}
    versions: Dict[str, str] = {}
    for field in ("dependencies", "devDependencies", "optionalDependencies"):
        for name, spec in (data.get(field) or {}).items():
            if isinstance(spec, str):
                versions[name] = spec
    return versions


def _extract(path: str, content: str) -> Dict[str, str]:
    if path.endswith("package-lock.json"):
        return _lock_versions(content)
    return _manifest_versions(content)


def find_regressions(base_ref: str, manifests: Iterable[str]) -> list[dict]:
    """Dependencies whose version moved backwards between base_ref and the tree."""
    regressions: list[dict] = []
    for path in manifests:
        base_content = _read_at_ref(base_ref, path)
        head_content = _read_worktree(path)
        if base_content is None or head_content is None:
            continue  # added or deleted wholesale — not a regression

        base_versions = _extract(path, base_content)
        head_versions = _extract(path, head_content)

        for name, base_raw in base_versions.items():
            head_raw = head_versions.get(name)
            if head_raw is None:
                continue  # removed — a deliberate act, not a silent rollback
            base_parsed = parse_version(base_raw)
            head_parsed = parse_version(head_raw)
            if base_parsed is None or head_parsed is None:
                continue
            if head_parsed < base_parsed:
                regressions.append(
                    {
                        "file": path,
                        "package": name,
                        "base": base_raw,
                        "head": head_raw,
                    }
                )
    return regressions


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fail when a change lowers an already-merged dependency version."
    )
    parser.add_argument(
        "--base",
        default="origin/master",
        help="Git ref to compare against (default: origin/master)",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON")
    args = parser.parse_args()

    regressions = find_regressions(args.base, REPO_MANIFESTS)

    if args.json:
        print(json.dumps({"base": args.base, "regressions": regressions}, indent=2))
        return 1 if regressions else 0

    if not regressions:
        print(f"No dependency regressions against {args.base}.")
        return 0

    print(f"Dependency REGRESSION against {args.base} — versions moved backwards:\n")
    width = max(len(item["package"]) for item in regressions)
    for item in regressions:
        print(
            f"  {item['package']:<{width}}  {item['base']}  ->  {item['head']}"
            f"   ({item['file']})"
        )
    print(
        "\nThis usually means the branch was opened before a neighbouring "
        "dependency PR merged, so its lockfile predates what master already "
        "ships. Merging would roll those packages back. Note that `git merge` "
        "reports CLEAN in this situation — there is no conflict to catch it.\n"
        "\nFix: rebase the branch on the current base and regenerate the lock\n"
        "  (`npm install --package-lock-only` — never hand-edit it), or for a\n"
        "  Dependabot branch comment `@dependabot rebase`.\n"
        "\nIf a downgrade is genuinely intended, say so in the PR body and "
        "adjust this check deliberately rather than working around it."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
