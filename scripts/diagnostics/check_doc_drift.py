"""
Fail fast on a small set of stale phrases in active docs.

This is intentionally narrow: it guards against high-impact contradictions
that have already caused agents to surface outdated architecture claims.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]

# Freshness ceiling: a doc that carries a "Last Updated" / "Last reviewed"
# stamp is claiming its content was verified on that date. Beyond this age
# the claim is stale — re-verify the content and restamp, or archive the doc
# (docs/proposals/resolved/). Docs without a stamp are not checked here;
# REQUIRED_STATUS_PREFIX governs which docs must carry markers at all.
MAX_STAMP_AGE_DAYS = 120

# Preserved-as-written records keep their original stamps by design.
FRESHNESS_EXEMPT_DIRS = ("docs/proposals/resolved/",)
FRESHNESS_EXEMPT_NAME = re.compile(r"20\d{2}-\d{2}")

# Known-stale docs with a rewrite tracked in an issue. An entry suppresses the
# failure (a warning still prints); the rewrite PR removes the entry. An entry
# whose doc is no longer stale fails the check so the list cannot rot.
STALE_STAMP_BURNDOWN = {
    "docs/dev/TOOL_REGISTRATION.md": "#1702",
    "docs/dev/CIRCUIT_BREAKER_DIALECTIC.md": "#1702",
}

_STAMP_RE = re.compile(
    r"^\**Last (?:Updated|reviewed)[:*]+\s*(?P<stamp>[A-Za-z0-9, -]+)",
    re.IGNORECASE | re.MULTILINE,
)


def parse_stamp_date(raw: str) -> date | None:
    """Parse the leading date out of a freshness stamp, ISO or 'Month D, YYYY'."""
    head = raw.strip()
    iso = re.match(r"\d{4}-\d{2}-\d{2}", head)
    if iso:
        return datetime.strptime(iso.group(0), "%Y-%m-%d").date()
    prose = re.match(r"[A-Z][a-z]+ \d{1,2}, \d{4}", head)
    if prose:
        return datetime.strptime(prose.group(0), "%B %d, %Y").date()
    return None


def freshness_failures(root: Path, today: date) -> list[str]:
    failures: list[str] = []
    candidates = sorted(root.glob("docs/**/*.md")) + [root / "ROADMAP.md"]
    for path in candidates:
        if not path.exists():
            continue
        rel = path.relative_to(root).as_posix()
        if rel.startswith(FRESHNESS_EXEMPT_DIRS):
            continue
        if FRESHNESS_EXEMPT_NAME.search(path.name):
            continue
        match = _STAMP_RE.search(path.read_text(encoding="utf-8"))
        if not match:
            continue
        stamp = parse_stamp_date(match.group("stamp"))
        if stamp is None:
            continue
        age = (today - stamp).days
        stale = age > MAX_STAMP_AGE_DAYS
        if rel in STALE_STAMP_BURNDOWN:
            if stale:
                print(
                    f" ! {rel}: stale stamp {stamp.isoformat()} tolerated, "
                    f"rewrite tracked in {STALE_STAMP_BURNDOWN[rel]}"
                )
            else:
                failures.append(
                    f"{rel}: no longer stale; remove its STALE_STAMP_BURNDOWN entry"
                )
        elif stale:
            failures.append(
                f"{rel}: freshness stamp {stamp.isoformat()} is {age} days old "
                f"(> {MAX_STAMP_AGE_DAYS}); re-verify and restamp, or archive"
            )
    return failures

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

    failures.extend(freshness_failures(REPO_ROOT, date.today()))

    if failures:
        print("Doc drift check failed:")
        for failure in failures:
            print(f" - {failure}")
        return 1

    print("Doc drift check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
