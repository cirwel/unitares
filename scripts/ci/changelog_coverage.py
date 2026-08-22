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

The denominator comes from `git log --first-parent`, matching either a squash
subject's trailing `(#N)` or a merge commit's `Merge pull request #N`. The first
version of this script used `git log --no-merges` and the trailing form only.
That silently dropped every pull request merged as a merge commit: thirteen of
them in v2.19.0, and *all twenty-one* in v2.18.0, where the method was 100%
merge commits. The gate reported "covers 131/131" on an entry missing thirteen
merges, and that ratio was copied onto the immutable release page as
"cites 133 of the 134 merged pull requests". See docs/releases/2.19.0-errata.md.

This still cannot see a rebase merge or a direct push, whose commits carry no
reference at all. So it does not assume: it counts first-parent commits, counts
the ones it could attribute, and **fails when any are unattributed** rather than
quietly measuring a smaller set. A denominator a merge-method choice can shrink
is not a denominator.

For the same reason it prints provenance, not a ratio. A bare "133/134" is a
claim, and a claim from a method that cannot vouch for its own denominator will
be quoted somewhere immutable. It was.

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
MERGE_PR = re.compile(r"^Merge pull request #(\d+)\b")

# A reason must come from a closed set. Free prose is unreviewable at write time
# and unsearchable afterwards, and an over-used category is only visible as a
# pattern if the categories are finite.
EXEMPT_REASONS = {
    "release-chore",      # this release's own bookkeeping
    "superseded",         # the change was reverted or replaced before shipping
    "no-user-effect",     # internal only, nothing an operator or agent can observe
    "covered-elsewhere",  # described under another entry's bullet
}
PR_REF = re.compile(r"#(\d+)")
EXEMPT_LINE = re.compile(
    r"<!--\s*changelog-coverage-exempt:\s*#?(\d+)\s+([a-z-]+)\s*-->"
)


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


def merged_prs(since_tag: str) -> tuple[dict[int, str], list[str]]:
    """(PR number -> subject, unattributed subjects) over the first-parent walk.

    First-parent is the list of changes that landed on this branch, one entry per
    merge, whatever method was used. Two subject shapes carry a reference:
    a squash's trailing `(#N)` and a merge commit's `Merge pull request #N`.
    A subject with two refs (`(#1607) (#1785)`) names an issue and then its PR;
    the trailing one is the merge.

    Anything else -- a rebase merge, a direct push -- is returned as
    unattributed rather than dropped. The caller fails on a non-empty list.
    Silently narrowing the denominator to what the parser happens to recognise
    is the defect this function was rewritten to remove.
    """
    out = _git("log", "--first-parent", "--format=%H\t%s", f"{since_tag}..HEAD")
    # Commits on the release branch itself have not been merged yet, so they
    # carry no reference and never will until this PR lands. Only a commit that
    # is ALREADY on the base branch without a reference indicates a real gap.
    base = _base_branch()
    found: dict[int, str] = {}
    unattributed: list[str] = []
    for line in out.splitlines():
        sha, _, subject = line.partition("\t")
        match = MERGE_PR.match(subject) or TRAILING_PR.search(subject)
        if match:
            found[int(match.group(1))] = subject
        elif RELEASE_CHORE.match(subject):
            continue
        elif base and not is_ancestor(sha, base):
            continue
        else:
            unattributed.append(subject)
    return found, unattributed


def _base_branch() -> str | None:
    """The upstream default branch, if this checkout can see it."""
    for ref in ("origin/master", "origin/main"):
        if subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "--verify", "-q", ref],
            capture_output=True,
        ).returncode == 0:
            return ref
    return None


def is_shallow() -> bool:
    return _git("rev-parse", "--is-shallow-repository") == "true"


def tag_commit(tag: str) -> str:
    return _git("rev-list", "-n", "1", tag)


def is_ancestor(commit: str, of: str) -> bool:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), "merge-base", "--is-ancestor", commit, of],
        capture_output=True,
    ).returncode == 0


def cited_prs(section: str) -> set[int]:
    return {int(n) for n in PR_REF.findall(section)}


def exempt_prs(section: str) -> tuple[dict[int, str], list[str]]:
    """(PR number -> reason, complaints) from one-per-line exemption comments.

    One declaration per pull request, each carrying a reason from a closed set.
    The earlier form accepted a comma-separated list of bare numbers and the
    script printed a ready-to-paste line containing every missing one, which
    made declaring an exemption feel like satisfying the tool rather than making
    a claim. It no longer prints one.
    """
    declared: dict[int, str] = {}
    complaints: list[str] = []
    for number, reason in EXEMPT_LINE.findall(section):
        if reason not in EXEMPT_REASONS:
            complaints.append(
                f"#{number} declares reason {reason!r}, which is not one of: "
                f"{', '.join(sorted(EXEMPT_REASONS))}"
            )
            continue
        declared[int(number)] = reason
    return declared, complaints


def main() -> int:
    list_only = "--list" in sys.argv
    version = current_version()
    tags = release_tags()

    # --- stand-down, and the two conditions that must not be confused with it
    if f"v{version}" in tags:
        tagged = tag_commit(f"v{version}")
        if is_ancestor(tagged, "HEAD"):
            print(f"[changelog-coverage] v{version} is already tagged — not a "
                  "release tree, skipping")
            return 0
        # A tag with the right name on a commit this tree never saw is an
        # abandoned or mistaken tagging attempt, not evidence of a release.
        # Standing down on the name alone would let it switch the gate off.
        print(f"[changelog-coverage] v{version} exists but is not an ancestor of "
              f"HEAD ({tagged[:8]}) — treating this as an unreleased tree",
              file=sys.stderr)

    section = entry_section(version)
    if section is None:
        print(f"[changelog-coverage] VERSION is {version}, it has no tag, and "
              f"docs/CHANGELOG.md has no `## [{version}]` entry. A release tree "
              "must carry its entry.", file=sys.stderr)
        return 0 if list_only else 1

    # --- preconditions. "I could not measure" must not exit like "it is fine".
    if is_shallow():
        print("[changelog-coverage] shallow repository — the merge range cannot "
              "be measured. Check out with fetch-depth: 0.", file=sys.stderr)
        return 0 if list_only else 1

    previous = next((t for t in tags if t != f"v{version}"), None)
    if previous is None:
        print("[changelog-coverage] no prior release tag — cannot establish a "
              "baseline. This is a genuine first release only if the tag list is "
              "genuinely empty; verify tags were fetched.", file=sys.stderr)
        return 0 if list_only else 1

    if not is_ancestor(tag_commit(previous), "HEAD"):
        print(f"[changelog-coverage] {previous} is not an ancestor of HEAD — the "
              "range would be meaningless.", file=sys.stderr)
        return 0 if list_only else 1

    merged, unattributed = merged_prs(previous)

    considered = {n: subj for n, subj in merged.items() if not RELEASE_CHORE.match(subj)}
    cited = cited_prs(section)
    exempt, complaints = exempt_prs(section)
    missing = {n: subj for n, subj in considered.items()
               if n not in cited and n not in exempt}

    # Provenance, not a ratio. A bare "133/134" is a claim, and the release body
    # copied the last one verbatim onto an immutable page.
    by_merge = sum(1 for s in merged.values() if MERGE_PR.match(s))
    print(f"[changelog-coverage] {version} since {previous}: "
          f"{len(merged) + len(unattributed)} first-parent changes, "
          f"{len(merged)} attributed ({by_merge} merge-commit, "
          f"{len(merged) - by_merge} squash), {len(unattributed)} unattributed")
    print(f"[changelog-coverage] {len(considered) - len(missing)} of "
          f"{len(considered)} cited in the entry "
          f"({len(merged) - len(considered)} release-chore excluded, "
          f"{len(exempt)} declared exempt)")

    failed = False

    if unattributed:
        failed = True
        print()
        print(f"[changelog-coverage] {len(unattributed)} change(s) carry no pull "
              "request reference, so the denominator is incomplete:")
        for subject in unattributed[:20]:
            print(f"  {subject}")
        print("A rebase merge or a direct push lands this way. Until each is "
              "attributable, coverage cannot be established.")

    if complaints:
        failed = True
        print()
        for complaint in complaints:
            print(f"[changelog-coverage] {complaint}")

    if missing:
        failed = True
        print()
        print(f"[changelog-coverage] {len(missing)} merged change(s) are not "
              "named in the entry:")
        for num in sorted(missing):
            print(f"  #{num}  {missing[num]}")
        print()
        print("Fold each into the entry. If one genuinely does not belong there,")
        print(f"declare it inside the `## [{version}]` section, one per line,")
        print("with a reason from: " + ", ".join(sorted(EXEMPT_REASONS)) + ".")
        print("The declaration form is:")
        print("    <!-- changelog-coverage-exempt: #NNNN reason -->")
        print()
        print("The omissions that matter most are the ones that qualify a claim")
        print("the entry already makes: citing a capability without the change")
        print("that bounds it reads as a stronger claim than the code supports.")

    if not failed:
        return 0
    return 0 if list_only else 1


if __name__ == "__main__":
    sys.exit(main())
