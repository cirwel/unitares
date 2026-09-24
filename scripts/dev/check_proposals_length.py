#!/usr/bin/env python3
"""Length ratchet for ``docs/proposals/active/*.md``.

A proposal in ``active/`` is meant to be read before someone acts on it. On
2026-09-23 the 46 files there held about 16.6k lines, and five single documents
ran from 863 to 2,468 lines. A document that long stops doing its job: the
reader pays the archaeology cost the document was written to remove.

The rule:

- A file in ``active/`` over ``CAP`` lines fails, unless it is listed in the
  baseline file.
- A listed file may not grow past its recorded count. Shrinking is allowed.
- A listed file that has dropped to ``CAP`` or below, or left ``active/``, must
  have its entry removed, so the baseline cannot outlive its subject.

``--lower`` rewrites the baseline to the current counts: it lowers entries that
shrank and drops entries that no longer need one. It never adds an entry and
never raises one, so it cannot be used to absorb growth.

This checks length only. It never judges content or status, and it proposes
nothing for deletion; the remedy for a long document is an index, a split into
an archive record, or a shorter rewrite, chosen by whoever owns it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ACTIVE_REL = Path("docs") / "proposals" / "active"
BASELINE_REL = Path("scripts") / "dev" / "proposals_length_baseline.txt"
CAP = 800


def count_lines(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def read_baseline(path: Path) -> tuple[list[str], dict[str, int]]:
    """Return (header comment lines, {repo-relative path: allowed lines})."""
    header: list[str] = []
    entries: dict[str, int] = {}
    if not path.is_file():
        return header, entries
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            if not entries:
                header.append(raw)
            continue
        count, _, rel = line.partition(" ")
        entries[rel.strip()] = int(count)
    return header, entries


def write_baseline(path: Path, header: list[str], entries: dict[str, int]) -> None:
    body = [f"{n} {rel}" for rel, n in sorted(entries.items())]
    path.write_text("\n".join([*header, *body]) + "\n", encoding="utf-8")


def check(root: Path) -> tuple[list[str], list[str]]:
    """Return (problems, notes)."""
    active = root / ACTIVE_REL
    _, baseline = read_baseline(root / BASELINE_REL)
    problems: list[str] = []
    notes: list[str] = []
    counts = {(ACTIVE_REL / p.name).as_posix(): count_lines(p) for p in sorted(active.glob("*.md"))}

    for rel, n in counts.items():
        allowed = baseline.get(rel)
        if allowed is None:
            if n > CAP:
                problems.append(
                    f"{rel} has {n} lines, over the {CAP}-line cap for active proposals. "
                    "Shorten it, move resolved detail to an archive record, or split it."
                )
        elif n > allowed:
            problems.append(
                f"{rel} grew to {n} lines, past its baseline of {allowed}. "
                "Files over the cap may shrink but not grow."
            )
        elif n <= CAP:
            problems.append(
                f"{rel} is now {n} lines, within the {CAP}-line cap. Remove its baseline "
                f"entry (run {Path(__file__).name} --lower)."
            )
        elif n < allowed:
            notes.append(
                f"{rel} shrank to {n} lines (baseline {allowed}); run "
                f"{Path(__file__).name} --lower to ratchet the baseline down."
            )

    for rel in sorted(set(baseline) - set(counts)):
        problems.append(
            f"stale baseline entry: {rel} is no longer in {ACTIVE_REL.as_posix()}/. "
            f"Remove it (run {Path(__file__).name} --lower)."
        )
    return problems, notes


def lower(root: Path) -> int:
    path = root / BASELINE_REL
    header, baseline = read_baseline(path)
    lowered: dict[str, int] = {}
    for rel, allowed in baseline.items():
        target = root / rel
        if not target.is_file():
            continue
        n = count_lines(target)
        if n > CAP:
            lowered[rel] = min(allowed, n)
    write_baseline(path, header, lowered)
    print(f"Baseline rewritten: {len(baseline)} -> {len(lowered)} entr(ies); none added or raised.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help=argparse.SUPPRESS)
    parser.add_argument("--lower", action="store_true", help="lower the baseline to current counts")
    args = parser.parse_args(argv)

    if args.lower:
        return lower(args.root)

    problems, notes = check(args.root)
    print("🔍 Proposals length ratchet...")
    for note in notes:
        print(f"⚠️  {note}")
    if problems:
        for p in problems:
            print(f"  ❌ {p}")
        print(f"\n❌ Proposals length ratchet: {len(problems)} problem(s)")
        return 1
    print(f"✅ Proposals length ratchet: active proposals within the {CAP}-line cap or their baseline")
    return 0


if __name__ == "__main__":
    sys.exit(main())
