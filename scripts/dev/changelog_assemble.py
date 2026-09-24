#!/usr/bin/env python3
"""Fold `docs/changelog.d/` fragments into the `## [Unreleased]` section.

Every pull request used to add its entry at the top of the same `[Unreleased]`
subsection of `docs/CHANGELOG.md`, so each merge put every other open PR that
carried an entry into CONFLICTING. Resolving that conflict changed the blob of
a file in the PR's diff, which re-keyed its review (`scripts/dev/review_gate.py`)
and forced a fresh one, during which the next merge landed another entry.

A fragment is one new file per PR, `docs/changelog.d/<section>-<slug>.md`,
whose body is the entry exactly as it would appear in the changelog. Unique
names cannot conflict and a base merge never touches them, so review keys stay
stable. The release cut folds them in with this script, as its first step and
before the version header is written, so `scripts/ci/changelog_coverage.py`
keeps checking the assembled entry unchanged.

Usage:
    python3 scripts/dev/changelog_assemble.py --check     # validate, exit 1 on a bad fragment
    python3 scripts/dev/changelog_assemble.py --preview   # print what would be folded in
    python3 scripts/dev/changelog_assemble.py             # fold in, then delete the fragments

`--root DIR` points every mode at another checkout (the tests use it).
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FRAGMENT_DIR = Path("docs") / "changelog.d"
CHANGELOG = Path("docs") / "CHANGELOG.md"

# The fragment's `<section>` prefix -> the `### ` heading it lands under.
# The set is the Keep a Changelog headings plus the ones this changelog's
# release entries really use (Breaking, Documentation, Tests, Validation).
# The order is the one a new subsection is created in; it follows the recent
# entries, which put Fixed before Removed.
SECTIONS: dict[str, str] = {
    "breaking": "Breaking",
    "added": "Added",
    "changed": "Changed",
    "deprecated": "Deprecated",
    "fixed": "Fixed",
    "removed": "Removed",
    "security": "Security",
    "documentation": "Documentation",
    "tests": "Tests",
    "validation": "Validation",
}
SECTION_RANK = {heading: rank for rank, heading in enumerate(SECTIONS.values())}

# Files in the fragment directory that are not fragments.
NOT_FRAGMENTS = {"README.md"}

FRAGMENT_NAME = re.compile(
    r"^(?P<section>[a-z]+)-(?P<slug>[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)\.md$"
)
HEADING_LINE = re.compile(r"^#{1,6}\s")
UNRELEASED = re.compile(r"^## \[Unreleased\][ \t]*$", re.MULTILINE)
NEXT_RELEASE = re.compile(r"^## \[", re.MULTILINE)
SUBSECTION = re.compile(r"^### (.+?)[ \t]*$")


@dataclass(frozen=True)
class Fragment:
    path: Path
    section: str  # the heading, e.g. "Added"
    body: str     # stripped of surrounding blank lines


def fragment_paths(root: Path) -> list[Path]:
    """Every file in the fragment directory except its README, sorted."""
    directory = root / FRAGMENT_DIR
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.iterdir()
                  if p.is_file() and p.name not in NOT_FRAGMENTS)


def load(root: Path) -> tuple[list[Fragment], list[str]]:
    """(valid fragments, problems). Any problem means nothing may be folded in."""
    fragments: list[Fragment] = []
    problems: list[str] = []
    for path in fragment_paths(root):
        rel = path.relative_to(root).as_posix()
        match = FRAGMENT_NAME.match(path.name)
        if not match:
            problems.append(
                f"{rel}: name must be <section>-<slug>.md, lowercase letters, "
                "digits and hyphens")
            continue
        section = match.group("section")
        if section not in SECTIONS:
            problems.append(
                f"{rel}: unknown section {section!r}; use one of: "
                + ", ".join(SECTIONS))
            continue
        body = path.read_text(encoding="utf-8").strip("\n").rstrip()
        if not body.strip():
            problems.append(f"{rel}: empty fragment")
            continue
        if not body.startswith("- "):
            problems.append(
                f"{rel}: the body must be the changelog entry itself, a "
                "markdown bullet starting with '- '")
            continue
        headings = [line for line in body.splitlines() if HEADING_LINE.match(line)]
        if headings:
            problems.append(
                f"{rel}: a fragment may not contain a heading ({headings[0]!r}); "
                "the file name chooses the section")
            continue
        fragments.append(Fragment(path, SECTIONS[section], body))
    return fragments, problems


def grouped(fragments: list[Fragment]) -> dict[str, list[Fragment]]:
    """Section heading -> fragments, sections in SECTIONS order, fragments by name."""
    groups: dict[str, list[Fragment]] = {}
    for heading in SECTIONS.values():
        members = sorted((f for f in fragments if f.section == heading),
                         key=lambda f: f.path.name)
        if members:
            groups[heading] = members
    return groups


def _unreleased_bounds(lines: list[str]) -> tuple[int, int]:
    """(index of the `## [Unreleased]` line, index one past its last content line).

    The section runs to the next `## [` heading; trailing blank lines and `---`
    rules before that heading belong to the separator, not to the section.
    """
    start = next((i for i, line in enumerate(lines)
                  if UNRELEASED.match(line.rstrip("\n"))), None)
    if start is None:
        raise ValueError("docs/CHANGELOG.md has no `## [Unreleased]` section")
    end = next((i for i in range(start + 1, len(lines))
                if NEXT_RELEASE.match(lines[i])), len(lines))
    while end > start + 1 and lines[end - 1].strip() in ("", "---"):
        end -= 1
    return start, end


def assemble_text(text: str, fragments: list[Fragment]) -> str:
    """The changelog with every fragment folded into `## [Unreleased]`.

    A fragment joins the top of its existing subsection, matching the newest-
    first convention of the hand-written entries; fragments folded together
    are ordered by file name so the result does not depend on directory order.
    An absent subsection is created in SECTIONS order relative to the ones the
    section already has.
    """
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"

    for heading, members in grouped(fragments).items():
        start, end = _unreleased_bounds(lines)
        block = [line + "\n" for member in members for line in member.body.splitlines()]

        existing = None
        for i in range(start + 1, end):
            match = SUBSECTION.match(lines[i].rstrip("\n"))
            if match and match.group(1) == heading:
                existing = i
                break

        if existing is not None:
            insert_at = existing + 1
            if insert_at < end and lines[insert_at].strip() == "":
                insert_at += 1  # keep the blank line a heading already has
            lines[insert_at:insert_at] = block
            continue

        # Create the subsection before the first existing one that ranks after
        # it, or at the end of the section when none does.
        rank = SECTION_RANK[heading]
        insert_at = end
        for i in range(start + 1, end):
            match = SUBSECTION.match(lines[i].rstrip("\n"))
            if match and SECTION_RANK.get(match.group(1), -1) > rank:
                insert_at = i
                break
        if insert_at == end:
            new = ["\n", f"### {heading}\n", *block]
        else:
            new = [f"### {heading}\n", *block, "\n"]
        lines[insert_at:insert_at] = new

    return "".join(lines)


def _print_problems(problems: list[str]) -> None:
    print(f"[changelog-assemble] {len(problems)} invalid fragment(s):", file=sys.stderr)
    for problem in problems:
        print(f"  {problem}", file=sys.stderr)
    print("See docs/changelog.d/README.md for the format.", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true",
                      help="validate fragment names, sections and bodies")
    mode.add_argument("--preview", action="store_true",
                      help="print what would be folded in; change nothing")
    parser.add_argument("--root", type=Path, default=REPO_ROOT,
                        help="checkout to operate on (default: this one)")
    args = parser.parse_args(argv)
    root = args.root.resolve()

    fragments, problems = load(root)
    if problems:
        _print_problems(problems)
        return 1

    if args.check:
        print(f"[changelog-assemble] {len(fragments)} fragment(s), all valid")
        return 0

    if not fragments:
        print("[changelog-assemble] no fragments to fold in")
        return 0

    if args.preview:
        for heading, members in grouped(fragments).items():
            print(f"### {heading}")
            for member in members:
                print(f"<!-- {member.path.relative_to(root).as_posix()} -->")
                print(member.body)
            print()
        return 0

    changelog = root / CHANGELOG
    try:
        updated = assemble_text(changelog.read_text(encoding="utf-8"), fragments)
    except ValueError as error:
        print(f"[changelog-assemble] {error}", file=sys.stderr)
        return 1
    changelog.write_text(updated, encoding="utf-8")
    for member in fragments:
        member.path.unlink()
    print(f"[changelog-assemble] folded {len(fragments)} fragment(s) into "
          f"`## [Unreleased]` and deleted them; stage the changelog and the "
          f"deletions together")
    return 0


if __name__ == "__main__":
    sys.exit(main())
