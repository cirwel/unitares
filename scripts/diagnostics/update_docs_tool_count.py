#!/usr/bin/env python3
"""
Documentation Tool Count Updater - Prevent Drift

Checks and updates tool-count claims in documentation files.
Run this after adding/removing tools to keep docs in sync.

Documentation publishes two different quantities, and each is checked against
its own source:

* **registry** — registered ``@mcp_tool`` dispatch tools
  (``count_tools.resolve_tool_count``). Written ``NN registered tools``, or in
  the older shapes ``**NN tools**``, ``NN tools)``, ``count: NN)``,
  ``NN+ tools``, which have always meant this count.
* **advertised** — what ``tools/list`` advertises: the registry plus the
  primary workflow aliases (``count_tools.resolve_advertised_tool_count``).
  Written ``NN advertised tools``.

A number in any other shape is not a guarded claim. So that such a number
cannot quietly replace a guarded one, every file names the quantities it must
carry, and a file missing one fails; a file that carries no count at all is
reported as unenforced rather than passed in silence.

Usage:
    python3 scripts/diagnostics/update_docs_tool_count.py --check  # Check for mismatches
    python3 scripts/diagnostics/update_docs_tool_count.py --update # Update all docs
"""

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, FrozenSet, List, Mapping

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

REGISTRY = "registry"
ADVERTISED = "advertised"

QUANTITY_LABELS = {
    REGISTRY: "registry tool count (registered dispatch tools)",
    ADVERTISED: "advertised tool count (registry + workflow aliases)",
}

#: The unambiguous marker for each quantity, named in failure messages.
EXPLICIT_MARKER = {
    REGISTRY: "NN registered tools",
    ADVERTISED: "NN advertised tools",
}


def load_tool_count():
    """Count via the runtime registry, as an available-or-not `ToolCount`.

    Imported at call time (not module level) so the doc-validation CI runner,
    which installs no project dependencies, reports unavailable instead of
    crashing — and so tests can patch the counter on its defining module.
    """
    from scripts.diagnostics.count_tools import resolve_tool_count

    return resolve_tool_count()


def load_advertised_tool_count():
    """Count the advertised roster, as an available-or-not `ToolCount`."""
    from scripts.diagnostics.count_tools import resolve_advertised_tool_count

    return resolve_advertised_tool_count()


def load_tool_counts() -> Dict[str, object]:
    """Both quantities, keyed by quantity name. Neither substitutes for the other."""
    return {REGISTRY: load_tool_count(), ADVERTISED: load_advertised_tool_count()}


# Guarded files, each with the quantities it must state. A file with an empty
# set is still scanned, so a count added to it is checked, but its lack of one
# is reported rather than failed.
DOC_FILES: Dict[str, FrozenSet[str]] = {
    # The README has carried no tool count since #2137 replaced the per-mode
    # figures with the one-catalog prose.
    "README.md": frozenset(),
    "docs/guides/START_HERE.md": frozenset(),
    # The full claim ledger states the registry count in registry framing.
    "docs/EVIDENCE_AND_LIMITS.md": frozenset({REGISTRY}),
}


@dataclass(frozen=True)
class Marker:
    """A recognised count shape. Group 1 of ``pattern`` is the number."""

    quantity: str
    pattern: "re.Pattern[str]"
    template: str

    def render(self, count: int) -> str:
        return self.template.format(count=count)


# The shapes are mutually exclusive: the explicit forms put a qualifier between
# the number and "tools", which none of the older registry shapes allow.
PATTERNS = [
    Marker(ADVERTISED, re.compile(r"\b(\d+) advertised tools\b"), "{count} advertised tools"),
    Marker(REGISTRY, re.compile(r"\b(\d+) registered tools\b"), "{count} registered tools"),
    Marker(REGISTRY, re.compile(r"\*\*(\d+) tools\*\*"), "**{count} tools**"),
    Marker(REGISTRY, re.compile(r"(\d+) tools\)"), "{count} tools)"),
    Marker(REGISTRY, re.compile(r"count: (\d+)\)"), "count: {count})"),
    # A lower-bound claim becomes the unambiguous canonical registry form.
    Marker(REGISTRY, re.compile(r"(\d+)\+ tools"), "{count} registered tools"),
]


@dataclass(frozen=True)
class Claim:
    file: str
    line: int
    quantity: str
    found: int
    text: str


def scan_file(filepath: Path) -> List[Claim]:
    """Every recognised count claim in ``filepath``, tagged with its quantity."""
    claims = []
    for i, line in enumerate(filepath.read_text().split("\n"), 1):
        for marker in PATTERNS:
            for match in marker.pattern.finditer(line):
                claims.append(
                    Claim(str(filepath), i, marker.quantity, int(match.group(1)), line.strip())
                )
    return claims


def check_file(filepath: Path, counts: Mapping[str, int], required: FrozenSet[str] = frozenset()) -> dict:
    """Compare ``filepath``'s claims against ``counts``.

    Returns ``mismatches`` (claims whose number is wrong), ``missing`` (required
    quantities the file does not state), ``claims`` (everything recognised) and
    ``exists``. A missing file states nothing, so all its requirements are missing.
    """
    if not filepath.exists():
        return {"exists": False, "claims": [], "mismatches": [], "missing": sorted(required)}

    claims = scan_file(filepath)
    mismatches = [c for c in claims if c.found != counts[c.quantity]]
    stated = {c.quantity for c in claims}
    return {
        "exists": True,
        "claims": claims,
        "mismatches": mismatches,
        "missing": sorted(required - stated),
    }


def update_file(filepath: Path, counts: Mapping[str, int]) -> bool:
    """Rewrite every recognised claim in ``filepath`` to its quantity's count."""
    if not filepath.exists():
        return False

    original = filepath.read_text()
    content = original
    for marker in PATTERNS:
        replacement = marker.render(counts[marker.quantity])
        content = marker.pattern.sub(lambda _m, r=replacement: r, content)

    if content != original:
        filepath.write_text(content)
        return True
    return False


def _report_coverage(results: Mapping[str, dict]) -> bool:
    """Print what each guarded file enforced. Returns True if any file failed."""
    failed = False
    for doc_file, result in results.items():
        if not result["exists"]:
            print(f"❌ {doc_file}: guarded file not found — nothing can be enforced on it")
            failed = True
            continue
        if result["missing"]:
            failed = True
            for quantity in result["missing"]:
                print(
                    f"❌ {doc_file}: states no {QUANTITY_LABELS[quantity]}; this file "
                    "is required to carry one, and a count in an unrecognised shape "
                    f"is not checked (write `{EXPLICIT_MARKER[quantity]}`)"
                )
        if not result["claims"]:
            print(f"⚠️  {doc_file}: no recognised tool count — nothing enforced in this file")
        else:
            stated = sorted({c.quantity for c in result["claims"]})
            print(f"   {doc_file}: checked {', '.join(stated)} ({len(result['claims'])} claim(s))")
    return failed


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Update tool count in documentation")
    parser.add_argument('--check', action='store_true', help='Check for mismatches (no changes)')
    parser.add_argument('--update', action='store_true', help='Update all documentation files')
    parser.add_argument(
        '--require-registry',
        action='store_true',
        help=(
            'Fail instead of skipping when the runtime registry is unavailable. '
            'Use where dependencies ARE installed, so an unavailable count is a '
            'real breakage rather than an accepted degradation.'
        ),
    )
    args = parser.parse_args()

    results = load_tool_counts()
    unavailable = {q: r for q, r in results.items() if not r.available}

    if unavailable:
        # Both counts need the same import tree, so one unavailable count means
        # the environment cannot count. Never compare an absent count against
        # the docs and never write one into them — and say plainly that this
        # run enforced nothing, so a green step is not read as a passed check.
        reason = "; ".join(f"{q}: {r.reason}" for q, r in unavailable.items())
        if args.require_registry:
            print(
                f"❌ Tool count unavailable ({reason}); "
                "--require-registry treats that as a failure"
            )
            sys.exit(1)
        print(f"WARNING: Tool count unavailable ({reason})", file=sys.stderr)
        print(
            f"⏭️  Tool count unavailable ({reason}) — "
            "doc tool-count check SKIPPED. This run enforced nothing; the "
            "count is enforced by the `smoke` job in the Tests workflow, "
            "which installs the runtime dependencies."
        )
        sys.exit(0)

    counts = {q: r.total for q, r in results.items()}
    for quantity, count in counts.items():
        print(f"{QUANTITY_LABELS[quantity]}: {count}")

    if args.update and not args.check and any(count == 0 for count in counts.values()):
        # A registry that imports but registers nothing is still not something
        # worth writing into reader-facing docs.
        print("❌ Refusing to update docs with a zero tool count")
        sys.exit(1)

    if args.update and not args.check:
        updated = [
            doc_file for doc_file in DOC_FILES if update_file(PROJECT_ROOT / doc_file, counts)
        ]
        if updated:
            print(f"\n✅ Updated {len(updated)} files:")
            for doc_file in updated:
                print(f"  - {doc_file}")
        else:
            print("\n✅ All files already up to date!")

    checks = {
        doc_file: check_file(PROJECT_ROOT / doc_file, counts, required)
        for doc_file, required in DOC_FILES.items()
    }
    mismatches = [c for result in checks.values() for c in result["mismatches"]]

    print()
    coverage_failed = _report_coverage(checks)

    if mismatches:
        print(f"\n❌ Found {len(mismatches)} mismatches:")
        for claim in mismatches:
            print(f"  {claim.file}:{claim.line}")
            print(f"    {claim.quantity}: found {claim.found}, expected {counts[claim.quantity]}")
            print(f"    {claim.text}")

    if mismatches or coverage_failed:
        sys.exit(1)
    print("\n✅ All documentation has correct tool counts!")
    sys.exit(0)


if __name__ == "__main__":
    main()
