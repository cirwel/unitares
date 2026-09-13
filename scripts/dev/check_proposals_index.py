#!/usr/bin/env python3
"""Fail if ``docs/proposals/README.md`` has drifted from the docs it indexes.

``docs/proposals/`` is 92 files and ~31k lines — over half of all documentation
in this repo. What keeps it navigable is not the folder, it is the index:
README.md tags every doc with one of five dispositions (Built / Registered /
Active / Parked / Closed) so "what is alive here" has a one-word answer per doc.

That index is hand-maintained, and a hand-maintained curation surface rots
quietly. This repo has the receipts. ``docs/operations/dormant-capability-registry.md``
carries a CUT backlog that went half-stale without anyone noticing: one row's
entire premise was another row that had been withdrawn seven months earlier, and
the withdrawal never propagated because the dependency was stated in prose and
never traversed. The registry now says so in its own header. This guard exists so
the proposals index does not repeat it.

It had already started. At the 2026-09-03 tagging the index recorded
``Active 21``; ten days later the rows said 23. Two docs had been added and
nobody updated the count. Nothing failed, because nothing was checking.

WHAT THIS GUARD DOES AND DOES NOT DECIDE
----------------------------------------
It checks the index against itself and against the filesystem — coverage, dead
links, count arithmetic, presence of a status line. All four are mechanical.

It deliberately does **not** decide whether a tag is *right*. The index's
30-day Active line is stated there as a choice ("a choice, not a measurement"),
and choosing what a doc's disposition should be is the operator's call, not a
script's. A guard that re-derived tags would quietly become the tagging
authority, and the README would stop being canonical for its own rule. So a
doc tagged **Active** that nobody has touched in a year passes this check. That
is not an oversight; reporting it is the index's job, and revising it is a
human's.

Equally, this never judges a proposal's content. A Parked doc is not a dead doc
— see the dormant-capability registry's standing rule that a caller count opens
an investigation and does not close one. Nothing here proposes deleting anything.

KNOWN DEBT
----------
``STATUS_LINE_DEBT`` lists docs whose bodies carry no parseable status line
today. They are recorded rather than auto-fixed: a status line is a claim about
a document, and backfilling one from the index tag would invert the authority
the README declares (body canonical, index a map). Each needs its author, or
someone who reads it, to state the status. The guard's job is to stop the list
growing.

``INDEX_EXEMPT`` lists docs that are correctly absent from the index.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PROPOSALS = REPO_ROOT / "docs" / "proposals"
INDEX = PROPOSALS / "README.md"

VALID_TAGS = ("Built", "Registered", "Active", "Parked", "Closed")

# Relocation stubs: the executed RFC lives in resolved/ and IS indexed there.
# The stub is a redirect for old links, not a proposal in its own right, so it
# does not earn an index row.
INDEX_EXEMPT = {
    "beam-wave-1-sentinel.md",
    "beam-wave-3a-read-only-handlers.md",
}

# Bodies with no parseable status line. EMPTY as of 2026-09-13, and the check
# below fails if an entry here no longer needs one, so it cannot quietly rot
# back into a list of excuses.
#
# It held nine names for part of that day. Two — cedar-delegation-authz-v0 and
# governed-effect-plane-v0 — were never debt at all: both state a status plainly,
# in the `**Created:** … · **Status:** …` shape, which the first version of
# STATUS_RE could not see because it anchored only to line start. A guard that
# over-reports debt is no better than one that under-reports it; both fail to
# discriminate, and this one was inventing work. The remaining seven were real
# and were written by reading each document, never by copying its index tag —
# the index is a map and the body is canonical, so backfilling from the tag would
# have inverted that.
STATUS_LINE_DEBT: set[str] = set()

# A LABELLED status field, not the word "status" appearing in prose.
#
# The label may open the line (`**Status:** …`, `- Status: …`) or follow a
# separator on a metadata line (`**Created:** 2026-08-21 · **Status:** …`), which
# is a shape two docs here already use. An earlier version anchored only to line
# start and so reported `cedar-delegation-authz-v0` and `governed-effect-plane-v0`
# as stating no status when both state one plainly — a guard over-reporting debt,
# which is no better than one under-reporting it.
#
# What it deliberately will NOT match is `status` inside a sentence, e.g.
# "Inference status: UNTESTED AS DEPLOYED" in a verdict paragraph. A substring
# that passes on a coincidence certifies nothing (the same objection PR #2184
# raised against matching a routed action as a substring), and a doc whose only
# "status" is prose has not declared one.
STATUS_RE = re.compile(
    r"(?:^\s*[-*>]?\s*|[·|]\s*)\**status\**\s*[:：]"
    r"|^\s*\**(?:status|disposition)\**\s*$",
    re.IGNORECASE,
)
LINK_RE = re.compile(r"\]\(([^)]+\.md)\)")
ROW_TAG_RE = re.compile(r"^\|\s*\[.*?\]\([^)]+\)\s*\|\s*\*\*(" + "|".join(VALID_TAGS) + r")")
COUNTS_RE = re.compile(
    r"(?:Current counts|Counts at tagging):\s*\n?\s*"
    + r"\s*·\s*".join(rf"({t})\s+(\d+)" for t in VALID_TAGS)
)


def has_status_line(path: Path) -> bool:
    with path.open(encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            if i >= 15:
                break
            if STATUS_RE.search(line):
                return True
    return False


def main() -> int:
    if not INDEX.is_file():
        print(f"❌ Proposals index guard: {INDEX} not found")
        return 1

    index_text = INDEX.read_text(encoding="utf-8")
    problems: list[str] = []
    notes: list[str] = []

    on_disk = {p.name for p in PROPOSALS.glob("*.md") if p.name != "README.md"}
    linked = {
        link.removeprefix("./")
        for link in LINK_RE.findall(index_text)
    }
    linked_toplevel = {l for l in linked if "/" not in l}

    # 1. Coverage — every proposal is reachable from the index.
    for name in sorted(on_disk - linked_toplevel - INDEX_EXEMPT):
        problems.append(
            f"not indexed: docs/proposals/{name} has no row in README.md. "
            f"Add one, or add it to INDEX_EXEMPT with the reason."
        )

    # 2. Dead links — every index link resolves.
    for link in sorted(linked):
        if link.startswith("../"):
            target = (PROPOSALS / link).resolve()
        else:
            target = PROPOSALS / link
        if not target.exists():
            problems.append(f"dead index link: README.md points at '{link}', which does not exist")

    # 3. Stale exemptions — don't let the allowlists outlive their subjects.
    for name in sorted(INDEX_EXEMPT - on_disk):
        problems.append(f"stale INDEX_EXEMPT entry: '{name}' is no longer on disk; remove it")
    for name in sorted(STATUS_LINE_DEBT - on_disk):
        problems.append(f"stale STATUS_LINE_DEBT entry: '{name}' is no longer on disk; remove it")

    # 4. Status lines — flag new debt only; the known list is recorded, not enforced.
    missing = {n for n in on_disk if not has_status_line(PROPOSALS / n)}
    for name in sorted(missing - STATUS_LINE_DEBT):
        problems.append(
            f"no status line: docs/proposals/{name} states no status in its first 15 lines. "
            f"The index is a map; the body is canonical."
        )
    fixed = STATUS_LINE_DEBT - missing
    for name in sorted(fixed):
        problems.append(
            f"STATUS_LINE_DEBT is out of date: '{name}' now has a status line. "
            f"Remove it from the list so the debt count stays honest."
        )

    # 5. Count arithmetic — the index's own summary must match its own rows.
    actual: dict[str, int] = {t: 0 for t in VALID_TAGS}
    for line in index_text.splitlines():
        m = ROW_TAG_RE.match(line)
        if m:
            actual[m.group(1)] += 1

    m = COUNTS_RE.search(index_text)
    if not m:
        problems.append(
            "could not find the 'Current counts:' summary in README.md. "
            "If the summary was intentionally removed, drop this check with it."
        )
    else:
        stated = {m.group(i): int(m.group(i + 1)) for i in range(1, 2 * len(VALID_TAGS), 2)}
        for tag in VALID_TAGS:
            if stated[tag] != actual[tag]:
                problems.append(
                    f"count drift: README says {tag} {stated[tag]}, but {actual[tag]} rows "
                    f"are tagged **{tag}**. Update the summary line."
                )

    if STATUS_LINE_DEBT & on_disk:
        notes.append(
            f"{len(STATUS_LINE_DEBT & on_disk)} doc(s) carry no status line (known debt, not yet fixed)"
        )

    print("🔍 Proposals index guard...")
    for note in notes:
        print(f"⚠️  {note}")
    if problems:
        for p in problems:
            print(f"  ❌ {p}")
        print(f"\n❌ Proposals index guard: {len(problems)} problem(s)")
        return 1

    print(
        f"✅ Proposals index guard: {len(on_disk)} proposal(s) indexed, "
        f"{len(linked)} link(s) resolve, counts match rows"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
