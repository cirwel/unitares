#!/usr/bin/env python3
"""Audit RFC §9 named test gates against the live test suite.

Mechanically reconciles every `Test name:` / `Test names:` reference in §9 of
`docs/proposals/surface-lease-plane-v0.md` against the actual Python and
Elixir test suites. For each gate, classifies as:

  exact   — a test with the named symbol exists
  variant — closest existing test name has difflib ratio >= VARIANT_THRESHOLD
  missing — no plausible match found

Architect-recommended starting move for §9 reconciliation
(`docs/proposals/surface-lease-plane-phase-a-plan.md` line 348). The
mechanical baseline lets follow-up PRs target specific missing/variant
rows rather than relitigating the count each time.

Usage:
    python3 scripts/dev/audit_rfc_section_9_gates.py            # full table
    python3 scripts/dev/audit_rfc_section_9_gates.py --missing  # missing only
    python3 scripts/dev/audit_rfc_section_9_gates.py --json     # machine-readable

Exit code: always 0. This is an audit, not a gate.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RFC_PATH = REPO_ROOT / "docs/proposals/surface-lease-plane-v0.md"
PYTHON_TEST_DIRS = [REPO_ROOT / "tests"]
ELIXIR_TEST_DIRS = [REPO_ROOT / "elixir/lease_plane/test"]

VARIANT_THRESHOLD = 0.75

_PY_TEST_NAME = re.compile(r"`(test_[a-zA-Z0-9_]+)`")
_ELIXIR_TEST_NAME = re.compile(r"`(test [^`]+)`")
_PY_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(test_[a-zA-Z0-9_]+)\s*\(", re.MULTILINE)
_EX_TEST = re.compile(r'^\s*test\s+"([^"]+)"', re.MULTILINE)
# Annotation: `# §9: test_xxx` (or with RFC prefix) above/inside a test
# declares it satisfies a §9 named gate even if the test's own name differs.
# Lets descriptive test names ("test_acquire_with_retry_jittered_backoff_within_bounds")
# claim the shorter §9 gate name ("test_acquire_with_retry_jittered_backoff")
# without requiring a rename. One annotation per line; multiple annotations
# on consecutive lines are all picked up.
_PY_ALIAS = re.compile(r"#\s*(?:RFC\s*)?§9:\s*(test_[a-zA-Z0-9_]+)")
_EX_ALIAS = re.compile(r"#\s*(?:RFC\s*)?§9:\s*(test [^\n]+?)\s*$", re.MULTILINE)


def find_section_9(text: str) -> str:
    start = text.find("## 9. Pre-implementation checklist")
    if start < 0:
        raise SystemExit("§9 not found in RFC")
    end = text.find("\n## 10.", start)
    return text[start:end] if end > start else text[start:]


def _uniq(seq):
    seen: set[str] = set()
    out: list[str] = []
    for s in seq:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def parse_gates(section_text: str) -> tuple[list[str], list[str]]:
    """Return (python_gates, elixir_gates) — names extracted from §9 lines
    that contain a `Test name:` or `Test names:` reference. Backtick-quoted
    `test_*` identifiers are Python; backtick-quoted `test "<desc>"`-style
    strings are Elixir."""
    py: list[str] = []
    elx: list[str] = []
    for line in section_text.splitlines():
        if "Test name" not in line:
            continue
        py.extend(m.group(1) for m in _PY_TEST_NAME.finditer(line))
        elx.extend(m.group(1) for m in _ELIXIR_TEST_NAME.finditer(line))
    return _uniq(py), _uniq(elx)


def collect_python_tests(dirs: list[Path]) -> dict[str, Path]:
    """Return {test_name: path}. Includes both real test defs AND §9 alias
    annotations (`# §9: test_xxx`) so a single test can claim multiple
    named gates without renaming.
    """
    found: dict[str, Path] = {}
    for d in dirs:
        if not d.exists():
            continue
        for p in d.rglob("*.py"):
            try:
                txt = p.read_text(errors="replace")
            except OSError:
                continue
            for m in _PY_DEF.finditer(txt):
                found.setdefault(m.group(1), p)
            for m in _PY_ALIAS.finditer(txt):
                found.setdefault(m.group(1), p)
    return found


def collect_elixir_tests(dirs: list[Path]) -> dict[str, Path]:
    """Return {full_test_string: path}. Includes both real test declarations
    AND §9 alias annotations.
    """
    found: dict[str, Path] = {}
    for d in dirs:
        if not d.exists():
            continue
        for p in d.rglob("*.exs"):
            try:
                txt = p.read_text(errors="replace")
            except OSError:
                continue
            for m in _EX_TEST.finditer(txt):
                found.setdefault(f"test {m.group(1)}", p)
            for m in _EX_ALIAS.finditer(txt):
                found.setdefault(m.group(1), p)
    return found


def _rel(p: Path) -> str:
    try:
        return str(p.relative_to(REPO_ROOT))
    except ValueError:
        return str(p)


def classify(name: str, found: dict[str, Path]) -> tuple[str, str]:
    """Return (status, evidence) where status is exact|variant|missing."""
    if name in found:
        return "exact", _rel(found[name])
    best_name = ""
    best_ratio = 0.0
    for cand in found:
        r = SequenceMatcher(None, name, cand).ratio()
        if r > best_ratio:
            best_name = cand
            best_ratio = r
    if best_ratio >= VARIANT_THRESHOLD:
        return "variant", f"{best_name}  (ratio {best_ratio:.2f})"
    return "missing", ""


def audit() -> list[dict[str, str]]:
    text = RFC_PATH.read_text()
    section = find_section_9(text)
    py_gates, elx_gates = parse_gates(section)
    py_found = collect_python_tests(PYTHON_TEST_DIRS)
    elx_found = collect_elixir_tests(ELIXIR_TEST_DIRS)

    rows: list[dict[str, str]] = []
    for name in py_gates:
        status, evidence = classify(name, py_found)
        rows.append({"lang": "python", "gate": name, "status": status, "evidence": evidence})
    for name in elx_gates:
        status, evidence = classify(name, elx_found)
        rows.append({"lang": "elixir", "gate": name, "status": status, "evidence": evidence})
    return rows


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--missing", action="store_true", help="show only missing gates")
    p.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = p.parse_args(argv)

    rows = audit()
    if args.missing:
        rows = [r for r in rows if r["status"] == "missing"]

    if args.json:
        print(json.dumps(rows, indent=2))
        return 0

    counts = {"exact": 0, "variant": 0, "missing": 0}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    print(f"{'lang':<7}  {'gate':<72}  {'status':<8}  evidence")
    print("-" * 7 + "  " + "-" * 72 + "  " + "-" * 8 + "  " + "-" * 40)
    for r in rows:
        print(f"{r['lang']:<7}  {r['gate']:<72}  {r['status']:<8}  {r['evidence']}")

    total = sum(counts.values())
    print()
    print(
        f"§9 audit: {total} named gates  →  "
        f"{counts['exact']} exact  /  {counts['variant']} variant  /  {counts['missing']} missing"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
