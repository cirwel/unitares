#!/usr/bin/env python3
"""Fail if a named resident identity is hardcoded in shipped source.

``check-repo-scope.sh`` already guards VENDOR neutrality — career artifacts,
per-vendor agent config, operator-local paths. This guards the other axis:
**fleet neutrality**. UNITARES ships to deployments that have their own
residents, or none. Which residents exist is declared per-deployment through
``UNITARES_RESIDENTS`` (see ``src/grounding/class_indicator.py``), and every
shipped code path must read that roster rather than naming anybody.

The leak this exists to catch is quiet by construction. On 2026-08-18 the
``/v1/residents`` tier-3 resolver filtered the declared roster through a
hardcoded ``["Vigil", "Sentinel", "Watcher", "Steward", "Chronicler",
"Lumen"]``. Any deployment whose residents were named anything else resolved
to an EMPTY list — still reported with ``source: "known-residents"``, so the
response read like a successful resolution of a roster that had silently
vanished. Nothing failed. A test even pinned the hardcoded order, so CI
defended the coupling.

WHAT IS AND IS NOT FLAGGED
--------------------------
Only **string literals in executable code** are flagged. Comments are invisible
to the AST and are deliberately fine: a note like "this threshold produced
false high-risk verdicts on Lumen 2026-05-08" is the reason a constant has the
value it has. Stripping that provenance would make the code less honest without
making it more portable. Docstrings are skipped for the same reason.

Names live in ``FLEET_IDENTITIES`` below, which is the one place in the repo
they are allowed to appear — the guard has to know what it is looking for, the
same way a secret scanner carries patterns.

Usage:
    python3 scripts/dev/check_fleet_identity_leak.py [--paths src agents/sdk/src]

Exit codes:
    0 — no hardcoded fleet identities in shipped source
    1 — at least one found (file:line printed)
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Directories whose contents SHIP or run as the server. Ops scripts, docs,
# tests, dashboards and plist templates are deliberately excluded: a deploy
# script for this operator's fleet is supposed to name this operator's fleet.
#
# This list must track ``[tool.setuptools.packages.find].include`` in
# pyproject.toml, which is ``src`` + ``governance_core``. governance_core was
# missing from the first version of this guard — half the shipped artifact,
# and the half most obliged to be agnostic, since it is the pure-Python core
# every deployment imports. It passes, but it was not being checked.
# agents/sdk/src ships separately as the unitares-sdk PyPI package; config/ is
# imported by src at runtime.
DEFAULT_PATHS = ("src", "governance_core", "config", "agents/sdk/src")

# The operator fleet identities that must not appear in shipped source. This
# list is the guard's own configuration — it is the single sanctioned mention.
FLEET_IDENTITIES = (
    "Vigil",
    "Sentinel",
    "Watcher",
    "Steward",
    "Chronicler",
    "Lumen",
)

# Homonyms and protocol values — NOT agent-label dispatch. Exempt, with the
# reason, because the word is doing a different job in these files.
NOT_IDENTITIES: dict[str, str] = {
    "src/evaluation/resident_validation/model.py":
        '"steward" is a ROLE in VALID_ROLES (dogfood_probe/steward/builder/'
        'reviewer), unrelated to the agent of that name',
    "src/coordination_events.py":
        "Service literal — coordination-protocol service ids, drift-tested by "
        "test_emit_rejects_unknown_service; not agent labels",
    "src/coordination_failure_emit.py":
        "same coordination-protocol service ids as coordination_events.py",
    "src/http_routes/sentinel.py":
        "producer_ref protocol value, not an agent label lookup",
    "src/watcher_state_reader.py":
        "legacy filesystem path component (data/watcher) read only for "
        "migration off the pre-#595 location",
}

# Real couplings that exist today and are NOT yet fixed. These are REPORTED on
# every run and deliberately do NOT fail the build, so this guard can be
# adopted without a six-module refactor first.
#
# ⛔They are not silenced. A guard that printed "clean" over known coupling
# would be the same failure it exists to catch: an instrument reporting health
# it did not establish. Fix an entry and delete its line; never add one to
# quiet a NEW leak.
KNOWN_COUPLINGS: dict[str, str] = {
    "src/http_routes/vigil.py":
        "resident-specific route module that dispatches on label.lower() == "
        '"vigil"; a deployment without that resident gets a dead endpoint',
}

# Match only when the literal IS a name, not when it merely contains one.
#
# This distinction is the whole difference between a usable guard and noise.
# "Sentinel" appears legitimately all over shipped source as a SUBSYSTEM name
# and, in redis_client.py, as an unrelated product (Redis Sentinel). Those are
# route paths ("/v1/sentinel/backlog"), metric ids
# ("governance.sentinel.findings.7d") and prose ("Redis Sentinel connected:
# ..."). None of them dispatch on an agent's label.
#
# A fleet-identity leak looks different: the literal is the label itself,
# compared or keyed against an agent's ``label`` field — ``if label ==
# "Lumen"``, ``canonical_order = ["Vigil", ...]``, ``{"vigil": 2400}``. Those
# are always the bare name. Substring matching flagged 31 places of which 1
# was real; whole-literal matching flags the real ones.
_NAMES_LOWER = frozenset(n.lower() for n in FLEET_IDENTITIES)


def _identity_literal(value: str) -> str | None:
    """Return the identity if the literal IS one, else None."""
    stripped = value.strip()
    return stripped if stripped.lower() in _NAMES_LOWER else None


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """ids() of Constant nodes that are docstrings, which are exempt."""
    out: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            if isinstance(first.value.value, str):
                out.add(id(first.value))
    return out


def scan_file(path: Path) -> list[str]:
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"  {path}: could not parse ({exc})"]

    exempt = _docstring_nodes(tree)
    rel = path.relative_to(REPO_ROOT).as_posix()
    findings: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in exempt:
            continue
        found = _identity_literal(node.value)
        if not found:
            continue
        findings.append(
            f'  {rel}:{node.lineno}: hardcoded fleet identity "{found}" '
            f"in a string literal"
        )
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paths", nargs="*", default=list(DEFAULT_PATHS))
    args = parser.parse_args()

    findings: list[str] = []
    known: list[str] = []
    scanned = 0
    for rel_root in args.paths:
        root = REPO_ROOT / rel_root
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            rel = path.relative_to(REPO_ROOT).as_posix()
            if rel in NOT_IDENTITIES:
                continue
            scanned += 1
            hits = scan_file(path)
            if not hits:
                continue
            if rel in KNOWN_COUPLINGS:
                known.extend(hits)
            else:
                findings.extend(hits)

    # Always printed, pass or fail: this repo has known coupling and the guard
    # must not imply otherwise.
    if known:
        print(f"⚠️  Fleet-identity guard: {len(known)} KNOWN coupling(s), not yet fixed")
        for line in known:
            rel = line.strip().split(":")[0]
            print(f"{line}\n      reason deferred: {KNOWN_COUPLINGS.get(rel, '')}")
        print()

    if not findings:
        print(
            f"✅ Fleet-identity guard: {scanned} file(s) scanned, no NEW "
            f"hardcoded identities"
        )
        return 0

    print(f"❌ Fleet-identity guard: {len(findings)} hardcoded identity reference(s)\n")
    for line in findings:
        print(line)
    print(
        "\nShipped source must read the roster from UNITARES_RESIDENTS "
        "(src/grounding/class_indicator.py), never name a resident.\n"
        "Provenance in a COMMENT is fine and is not flagged — only string "
        "literals in executable code are.\n"
        "See docs/operations/resident-roster.md."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
