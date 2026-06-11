"""
Fail fast on a small set of stale phrases in active docs.

This is intentionally narrow: it guards against high-impact contradictions
that have already caused agents to surface outdated architecture claims.
"""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]

ACTIVE_DOC_CHECKS = {
    "docs/guides/START_HERE.md": [
        "System operates on agent-reported inputs.",
    ],
    "docs/UNIFIED_ARCHITECTURE.md": [
        "- `complexity` — self-reported cognitive load [0, 1]",
    ],
}

REQUIRED_STATUS_PREFIX = {
    "README.md": "Status:",
    "docs/dev/CIRCUIT_BREAKER_DIALECTIC.md": "Status:",
    "docs/UNIFIED_ARCHITECTURE.md": "Status:",
    "docs/guides/TROUBLESHOOTING.md": "Status:",
    "docs/guides/START_HERE.md": "Status:",
    "docs/operations/OPERATOR_RUNBOOK.md": "Status:",
    "docs/dev/CANONICAL_SOURCES.md": "Status:",
    "docs/operations/database_architecture.md": "Status:",
    "docs/operations/DEFINITIVE_PORTS.md": "Status:",
    "docs/guides/CIRS_PROTOCOL.md": "Status:",
    "docs/dev/TOOL_REGISTRATION.md": "Status:",
}

MAX_LINES = {
    "docs/guides/START_HERE.md": 80,
    "docs/operations/database_architecture.md": 80,
    "docs/operations/DEFINITIVE_PORTS.md": 60,
}


def main() -> int:
    failures: list[str] = []

    for rel_path, banned_phrases in ACTIVE_DOC_CHECKS.items():
        path = REPO_ROOT / rel_path
        text = path.read_text(encoding="utf-8")
        for phrase in banned_phrases:
            if phrase in text:
                failures.append(f"{rel_path}: stale phrase present -> {phrase!r}")

    for rel_path, prefix in REQUIRED_STATUS_PREFIX.items():
        path = REPO_ROOT / rel_path
        text = path.read_text(encoding="utf-8")
        if prefix not in text:
            failures.append(f"{rel_path}: missing required status marker {prefix!r}")

    for rel_path, max_lines in MAX_LINES.items():
        path = REPO_ROOT / rel_path
        line_count = len(path.read_text(encoding="utf-8").splitlines())
        if line_count > max_lines:
            failures.append(
                f"{rel_path}: too long ({line_count} lines > {max_lines}); keep it as a thin entrypoint"
            )

    canonical_doc = REPO_ROOT / "docs" / "dev" / "CANONICAL_SOURCES.md"
    if not canonical_doc.exists():
        failures.append("docs/dev/CANONICAL_SOURCES.md: missing canonical source map")

    if failures:
        print("Doc drift check failed:")
        for failure in failures:
            print(f" - {failure}")
        return 1

    print("Doc drift check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
