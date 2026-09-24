#!/usr/bin/env python3
"""Fail an ordinary pull request that edits `## [Unreleased]` in docs/CHANGELOG.md.

Ordinary pull requests add a fragment under `docs/changelog.d/` instead; the
release cut folds the fragments in (`scripts/dev/changelog_assemble.py`). While
every PR wrote its entry into the same `[Unreleased]` subsection, each merge
put every other open PR with an entry into CONFLICTING, and resolving that
conflict re-keyed the PR's review. One PR that goes back to editing the section
restores the conflict for everyone after it, so this check says so on the PR.

Scope, deliberately narrow:

- Only lines inside the `## [Unreleased]` section count, on either side of the
  diff. A released entry stays editable, because an errata correction and the
  forward-merge of a maintenance release legitimately change one, and neither
  collides with the next merge the way an Unreleased entry does. The check
  reads the pull request's diff only, so the entries already on the base
  branch never trip it.
- A release tree is exempt: VERSION names a version with no tag yet, the same
  test `changelog_coverage.py` uses, so the release cut can write its entry.
- A dependabot pull request is exempt.
- It runs only for a `pull_request` event; any other event is a quiet pass.

Usage:
    python scripts/ci/changelog_direct_edit.py [--base origin/master]
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import changelog_coverage as coverage  # noqa: E402  (same directory)

REPO_ROOT = coverage.REPO_ROOT
CHANGELOG_PATH = "docs/CHANGELOG.md"
DEPENDABOT = "dependabot[bot]"

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
UNRELEASED = re.compile(r"^## \[Unreleased\]")
NEXT_RELEASE = re.compile(r"^## \[")


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(REPO_ROOT), *args],
                          capture_output=True, text=True, check=check)


def is_release_tree() -> bool:
    """True unless `v<VERSION>` is a tag reachable from HEAD."""
    version = coverage.current_version()
    tag = f"v{version}"
    if tag in coverage.release_tags():
        return not coverage.is_ancestor(coverage.tag_commit(tag), "HEAD")
    return True


def unreleased_lines(text: str) -> range:
    """1-based line numbers of the `## [Unreleased]` section, header included.

    Trailing blank lines and `---` rules belong to the separator before the
    next release heading, so a forward-merged release entry inserted there is
    not mistaken for an Unreleased edit.
    """
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if UNRELEASED.match(line)), None)
    if start is None:
        return range(0)
    end = next((i for i in range(start + 1, len(lines))
                if NEXT_RELEASE.match(lines[i])), len(lines))
    while end > start + 1 and lines[end - 1].strip() in ("", "---"):
        end -= 1
    return range(start + 1, end + 1)


def _show(rev: str) -> str:
    result = _git("show", f"{rev}:{CHANGELOG_PATH}", check=False)
    return result.stdout if result.returncode == 0 else ""


def changed_lines(base: str) -> tuple[list[int], list[int]]:
    """(removed line numbers in base, added line numbers in HEAD) for the changelog."""
    diff = _git("diff", "--no-color", "--no-ext-diff", "-U0", base, "HEAD",
                "--", CHANGELOG_PATH).stdout
    removed: list[int] = []
    added: list[int] = []
    old = new = 0
    for line in diff.splitlines():
        match = HUNK.match(line)
        if match:
            old, new = int(match.group(1)), int(match.group(3))
            continue
        if line.startswith(("---", "+++")):
            continue
        if line.startswith("-"):
            removed.append(old)
            old += 1
        elif line.startswith("+"):
            added.append(new)
            new += 1
    return removed, added


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", default="origin/master",
                        help="the branch the pull request targets")
    args = parser.parse_args(argv)

    event = os.environ.get("GITHUB_EVENT_NAME")
    if event and event != "pull_request":
        print(f"[changelog-direct-edit] {event} event, not a pull request — skipping")
        return 0
    author = os.environ.get("PR_AUTHOR", "")
    head_ref = os.environ.get("GITHUB_HEAD_REF", "")
    if author == DEPENDABOT or head_ref.startswith("dependabot/"):
        print("[changelog-direct-edit] dependabot pull request — exempt")
        return 0
    if is_release_tree():
        print(f"[changelog-direct-edit] VERSION {coverage.current_version()} is not "
              "tagged: a release tree, which writes the changelog entry — exempt")
        return 0

    merge_base = _git("merge-base", args.base, "HEAD", check=False)
    if merge_base.returncode != 0:
        print(f"[changelog-direct-edit] cannot find a merge base with {args.base}; "
              "check out with fetch-depth: 0", file=sys.stderr)
        return 1
    base = merge_base.stdout.strip()

    removed, added = changed_lines(base)
    old_section = unreleased_lines(_show(base))
    new_section = unreleased_lines(_show("HEAD"))
    touched = ([n for n in removed if n in old_section]
               + [n for n in added if n in new_section])
    if not touched:
        note = " (released entries only)" if removed or added else ""
        print(f"[changelog-direct-edit] `## [Unreleased]` untouched{note} — ok")
        return 0

    print(f"[changelog-direct-edit] this pull request edits the `## [Unreleased]` "
          f"section of {CHANGELOG_PATH} ({len(touched)} line(s)).", file=sys.stderr)
    print("Add the entry as a fragment instead: one new file, "
          "docs/changelog.d/<section>-<slug>.md, whose body is the entry exactly as "
          "it would appear in the changelog. See docs/changelog.d/README.md.",
          file=sys.stderr)
    print("Every PR that edits that section conflicts with every merge that "
          "lands another entry, and resolving the conflict re-keys its review. "
          "The release cut folds fragments in with "
          "scripts/dev/changelog_assemble.py.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
