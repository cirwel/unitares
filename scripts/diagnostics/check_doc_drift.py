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
STALE_STAMP_BURNDOWN: dict[str, str] = {}

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


# The product story is deliberately repeated on several public surfaces. Keep
# the required concepts and the caveats that bound them executable so a later
# wording pass cannot silently turn a multi-runtime, single-authority server
# into a claim of cross-server federation or complete reconstruction.
PUBLIC_POSITIONING_CHECKS: dict[str, list[tuple[str, tuple[str, ...]]]] = {
    "README.md": [
        ("product category", ("federation kernel",)),
        ("claims and evidence", ("claims and evidence",)),
        ("review", ("review",)),
        ("outcomes", ("outcomes",)),
        ("reconstruction", ("reconstruction",)),
        (
            "single authority boundary",
            ("one operator-controlled server and authority domain",),
        ),
        (
            "no cross-server replication",
            ("does not promise autonomous cross-server replication",),
        ),
        (
            "check-in text retention limit",
            ("does not retain the original report text",),
        ),
        ("unmeasured comparative benefit", ("still need comparative evaluation",)),
    ],
    "docs/PRODUCT_DEFINITION.md": [
        ("product category", ("federation kernel",)),
        ("claims and evidence", ("claims and evidence",)),
        ("review", ("review",)),
        ("outcomes", ("outcomes",)),
        ("reconstruction", ("reconstruction",)),
        (
            "single authority boundary",
            ("one operator-controlled server and authority domain",),
        ),
        (
            "no cross-server replication",
            ("replicates state across independent servers",),
        ),
        (
            "check-in text retention limit",
            ("original `sync_state` report text is transient",),
        ),
        ("unmeasured comparative benefit", ("remain unmeasured",)),
    ],
    "docs/CAPABILITIES_AND_DEPLOYMENT.md": [
        ("product category", ("federation kernel",)),
        ("claims and evidence", ("claims and evidence",)),
        ("review", ("review",)),
        ("outcomes", ("outcomes",)),
        ("reconstruction", ("reconstruction",)),
        (
            "single authority boundary",
            ("one operator-controlled server and authority domain",),
        ),
        (
            "check-in text retention limit",
            (
                "original `sync_state` report text is not part of the persisted state history",
            ),
        ),
        (
            "AGE replacement-link boundary",
            ("AGE enables graph-specific behavior, including replacement-link",),
        ),
        ("provider-hosted Glama boundary", ("private provider-hosted Glama bundle",)),
        ("unmeasured comparative benefit", ("remain evaluation questions",)),
    ],
    "src/tool_modes.py": [
        ("product category", ("federation kernel",)),
        ("claims and evidence", ("claims and evidence",)),
        ("review", ("review",)),
        ("outcomes", ("outcomes",)),
        ("reconstruction", ("reconstruction",)),
        (
            "single authority boundary",
            ("one operator-controlled server and authority domain",),
        ),
        (
            "no cross-server replication",
            ("does not replicate state across independent servers",),
        ),
        (
            "check-in text retention limit",
            ("original report text is not retained as durable history",),
        ),
        (
            "AGE replacement-link boundary",
            ("replacement-link traversal requires the AGE backend",),
        ),
        (
            "discovery-readiness boundary",
            ("advertising a tool does not establish dependency readiness",),
        ),
    ],
    "pyproject.toml": [
        ("product category", ("federation kernel",)),
        ("claims and evidence", ("claims and evidence",)),
        ("review", ("review",)),
        ("outcomes", ("outcomes",)),
        ("reconstruction", ("reconstruction",)),
    ],
    "docs/deployment/glama.md": [
        ("self-hostable product", ("self-hostable federation kernel",)),
        ("provider-hosted deployment", ("private provider-hosted deployment",)),
        ("fresh-process identity", ("fresh processes receive fresh identities",)),
        ("external-processing boundary", ("optional inference and integrations",)),
    ],
}


def public_positioning_failures(root: Path) -> list[str]:
    """Return missing public concepts/caveats after whitespace normalization."""
    failures: list[str] = []
    for rel_path, requirements in PUBLIC_POSITIONING_CHECKS.items():
        path = root / rel_path
        if not path.exists():
            failures.append(f"{rel_path}: missing public positioning surface")
            continue
        raw = path.read_text(encoding="utf-8")
        # Blockquote markers recur on each wrapped source line but are not part
        # of the rendered sentence the positioning contract is checking.
        rendered_text = re.sub(r"(?m)^\s*>\s?", "", raw)
        normalized = " ".join(rendered_text.split()).casefold()
        for label, alternatives in requirements:
            if not any(
                " ".join(term.split()).casefold() in normalized for term in alternatives
            ):
                failures.append(
                    f"{rel_path}: missing public positioning requirement {label!r}"
                )
    return failures


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
    failures.extend(public_positioning_failures(REPO_ROOT))

    if failures:
        print("Doc drift check failed:")
        for failure in failures:
            print(f" - {failure}")
        return 1

    print("Doc drift check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
