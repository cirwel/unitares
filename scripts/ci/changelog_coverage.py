#!/usr/bin/env python3
"""Fail a release PR whose changelog entry does not cover the merge range.

`docs/releases/2.18.0-errata.md` exists because the 2.18.0 entry "omitted two
merged changes and conflated several citations", and that was only discoverable
after the tag was public and immutable. The 2.19.0 entry repeated it at larger
scale: 88 of 133 merged pull requests cited on the first pass.

Neither was carelessness. The entry is hand-curated from a range the author did
not personally live through -- 155 commits over nine days for 2.19.0 -- so
under-coverage is the expected output of the method, not an accident of it.
Nothing compared the entry against the range, so nothing reported it. This is
that comparison.

It runs only on a release: a tree whose VERSION has no corresponding tag yet.
Once `v<VERSION>` exists the same tree is a normal post-release state and the
check stands down, so ordinary PRs never pay for it.

Escape hatch, because a real release will have deliberate omissions (the
release PR itself, internal workflow notes): an HTML comment inside the entry,

    <!-- changelog-coverage-exempt: 1788, 1789 -->

Exemptions are listed in the diff, in the file the release ships, next to the
entry they justify -- which is the point. A silent allowance would reproduce
the failure one layer up.

Usage:
    python scripts/ci/changelog_coverage.py            # check, exit 1 on gaps
    python scripts/ci/changelog_coverage.py --list     # report, always exit 0
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CHANGELOG = REPO_ROOT / "docs" / "CHANGELOG.md"
VERSION_FILE = REPO_ROOT / "VERSION"

# A merge whose own subject is the release bookkeeping. It cannot appear in the
# entry it creates, so requiring it would make every release permanently red.
RELEASE_CHORE = re.compile(r"^(chore|docs)\(release\)")

TRAILING_PR = re.compile(r"\(#(\d+)\)$")
PR_REF = re.compile(r"#(\d+)")
EXEMPT_LINE = re.compile(r"<!--\s*changelog-coverage-exempt:\s*([0-9,\s#]*)-->")


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def current_version() -> str:
    return VERSION_FILE.read_text(encoding="utf-8").strip()


def release_tags() -> list[str]:
    """Server release tags, newest first. SDK tags use their own series."""
    out = _git("for-each-ref", "--sort=-creatordate",
               "--format=%(refname:short)", "refs/tags/v*")
    return [t for t in out.splitlines() if re.fullmatch(r"v\d+\.\d+\.\d+", t)]


def entry_section(version: str) -> str | None:
    """The `## [version]` block of the changelog, or None if absent."""
    text = CHANGELOG.read_text(encoding="utf-8")
    start = text.find(f"## [{version}]")
    if start == -1:
        return None
    nxt = text.find("\n## [", start + 1)
    return text[start:] if nxt == -1 else text[start:nxt]


def merged_prs(since_tag: str) -> dict[int, str]:
    """PR number -> subject, for every non-merge commit since `since_tag`.

    Keyed on the trailing `(#N)`, which is the squash-merge reference. A subject
    carrying two refs (`(#1607) (#1785)`) names an issue and then its PR; the
    trailing one is the merge.
    """
    out = _git("log", "--no-merges", "--format=%s", f"{since_tag}..HEAD")
    found: dict[int, str] = {}
    for subject in out.splitlines():
        m = TRAILING_PR.search(subject)
        if m:
            found[int(m.group(1))] = subject
    return found


def cited_prs(section: str) -> set[int]:
    return {int(n) for n in PR_REF.findall(section)}


def exempt_prs(section: str) -> set[int]:
    out: set[int] = set()
    for raw in EXEMPT_LINE.findall(section):
        out |= {int(n) for n in re.findall(r"\d+", raw)}
    return out


def main() -> int:
    list_only = "--list" in sys.argv
    version = current_version()

    if f"v{version}" in release_tags():
        print(f"[changelog-coverage] v{version} is already tagged — not a release tree, skipping")
        return 0

    section = entry_section(version)
    if section is None:
        print(f"[changelog-coverage] no `## [{version}]` entry yet — not a release PR, skipping")
        return 0

    tags = release_tags()
    if not tags:
        print("[changelog-coverage] no prior release tag to compare against, skipping")
        return 0
    previous = tags[0]

    merged = merged_prs(previous)
    if not merged:
        print(f"[changelog-coverage] no merged pull requests since {previous}, nothing to cover")
        return 0

    cited = cited_prs(section)
    exempt = exempt_prs(section)

    # Release bookkeeping is out of scope entirely rather than counted as
    # covered: reporting "2/2" for one real change and one chore(release)
    # overstates what the entry actually accounts for.
    considered = {
        num: subject for num, subject in merged.items()
        if not RELEASE_CHORE.match(subject)
    }
    missing = {
        num: subject for num, subject in considered.items()
        if num not in cited and num not in exempt
    }

    covered = len(considered) - len(missing)
    print(f"[changelog-coverage] {version} covers {covered}/{len(considered)} "
          f"merged pull requests since {previous}")
    if exempt:
        print(f"[changelog-coverage] exempted by declaration: "
              f"{', '.join(f'#{n}' for n in sorted(exempt))}")

    if not missing:
        return 0

    print()
    print(f"[changelog-coverage] {len(missing)} merged change(s) are not named in the entry:")
    for num in sorted(missing):
        print(f"  #{num}  {missing[num]}")
    print()
    print("Fold each into the entry, or declare it deliberately inside the")
    print(f"`## [{version}]` section:")
    print()
    print(f"    <!-- changelog-coverage-exempt: {', '.join(str(n) for n in sorted(missing))} -->")
    print()
    print("The omissions that matter most are the ones that qualify a claim the")
    print("entry already makes: citing a capability without the change that")
    print("bounds it reads as a stronger claim than the code supports.")

    return 0 if list_only else 1


if __name__ == "__main__":
    sys.exit(main())
