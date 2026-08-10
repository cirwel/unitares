#!/usr/bin/env python3
"""Classify open Dependabot PRs across the CIRWEL fleet. Report only.

Replaces a claude.ai cloud routine whose merge policy lived in a web form.
The classification below is that policy, transcribed and made reviewable.

REPORT ONLY: this never merges. It prints what a merger *would* do, so a
week of output can be diffed against the routine before anything is granted
merge rights.

Honesty contract: an empty report must never be indistinguishable from a
broken one. Every repo we could not read is listed under UNREACHABLE, and
every PR we could not classify is listed under UNCLASSIFIED. A silent zero
is the failure mode this replaces (see the KG sweep that emitted an empty
candidate list for five weeks).

Usage:
    python3 scripts/dev/dependabot_triage.py [--repos a,b] [--json] [--markdown]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

DEFAULT_REPOS = [
    "CIRWEL/unitares",
    "CIRWEL/unitares-core",
    "CIRWEL/anima-mcp",
    "CIRWEL/unitares-discord-bridge",
    "CIRWEL/unitares-governance-plugin",
    "CIRWEL/unitares-pi-plugin",
    "CIRWEL/unitares-host-adapter",
    "CIRWEL/cirwel-site",
]

# Verdicts
MERGE = "WOULD-MERGE"
HOLD = "HELD"
BLOCKED = "BLOCKED"
WAIT = "WAITING-ON-CI"
UNCLASSIFIED = "UNCLASSIFIED"

# `bump <pkg> from 1.2.3 to 1.2.4`
BUMP_RE = re.compile(
    r"bump\s+(?P<pkg>\S+)\s+from\s+v?(?P<old>\d+(?:\.\d+)*)\s+to\s+v?(?P<new>\d+(?:\.\d+)*)",
    re.I,
)
# `update <pkg> requirement from <50.0.0,>=41.0.0 to >=41.0.0,<51.0.0`
REQ_RE = re.compile(
    r"update\s+(?P<pkg>\S+)\s+requirement\s+from\s+(?P<old>\S+)\s+to\s+(?P<new>\S+)", re.I
)
# `bump python from `cea0e60` to `a7fb1e6``
DIGEST_RE = re.compile(r"bump\s+(?P<pkg>\S+)\s+from\s+`(?P<old>[0-9a-f]+)`\s+to\s+`(?P<new>[0-9a-f]+)`", re.I)
GROUP_RE = re.compile(r"bump\s+the\s+(?P<group>[\w-]+)\s+group", re.I)
UPPER_BOUND_RE = re.compile(r"<\s*(\d+)(?:\.\d+)*")


@dataclass
class Verdict:
    repo: str
    number: int
    title: str
    verdict: str
    reason: str
    checks: str = ""
    mergeable: str = ""


@dataclass
class Report:
    verdicts: list[Verdict] = field(default_factory=list)
    unreachable: list[dict[str, str]] = field(default_factory=list)
    repos_scanned: list[str] = field(default_factory=list)


def gh_json(args: list[str]) -> Any:
    """Run a gh command expecting JSON. Raises RuntimeError with stderr on failure."""
    proc = subprocess.run(["gh", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "gh failed").strip().splitlines()[-1])
    return json.loads(proc.stdout or "[]")


def parse_version(raw: str) -> list[int]:
    return [int(p) for p in raw.split(".") if p.isdigit()]


def classify_title(title: str) -> tuple[str, str]:
    """Return (class, reason). Class is MERGE, HOLD, or UNCLASSIFIED.

    Mirrors the routine's rules:
      PATCH/MINOR   -> auto-merge candidate
      MAJOR / range-widening -> hold for human review
      actions group -> CI-only, auto-merge candidate
    Anything else is UNCLASSIFIED on purpose. Guessing is how a triage bot
    merges something it did not understand.
    """
    if DIGEST_RE.search(title):
        return UNCLASSIFIED, "digest bump — no version semantics in the title to compare"

    group = GROUP_RE.search(title)
    if group:
        if group.group("group").lower() == "actions":
            return MERGE, "actions group — CI-only surface"
        return UNCLASSIFIED, (
            f"{group.group('group')!r} group bump — per-package versions are in the PR body, "
            "not the title; needs body parsing to classify"
        )

    req = REQ_RE.search(title)
    if req:
        old_upper = UPPER_BOUND_RE.search(req.group("old"))
        new_upper = UPPER_BOUND_RE.search(req.group("new"))
        if old_upper and new_upper:
            if int(new_upper.group(1)) > int(old_upper.group(1)):
                return HOLD, (
                    f"range-widening: upper bound major {old_upper.group(1)} -> {new_upper.group(1)}"
                )
            return MERGE, "requirement update with no upper-bound major increase"
        return UNCLASSIFIED, "requirement update whose bounds could not be parsed"

    bump = BUMP_RE.search(title)
    if bump:
        old, new = parse_version(bump.group("old")), parse_version(bump.group("new"))
        if not old or not new:
            return UNCLASSIFIED, "version strings could not be parsed"
        if new[0] > old[0]:
            return HOLD, f"major bump {bump.group('old')} -> {bump.group('new')}"
        if len(old) > 1 and len(new) > 1 and new[1] > old[1]:
            # Minor bumps auto-merge only for dev/test-only deps. A minor bump
            # on a runtime dependency is a human call — the policy is narrower
            # here than "anything below major", deliberately.
            if title.lower().startswith("chore(deps-dev)"):
                return MERGE, f"minor bump {bump.group('old')} -> {bump.group('new')} (dev-dep)"
            return HOLD, (
                f"minor bump {bump.group('old')} -> {bump.group('new')} on a runtime dep — "
                "auto-merge covers minors only for dev-deps"
            )
        return MERGE, f"patch bump {bump.group('old')} -> {bump.group('new')}"

    return UNCLASSIFIED, "title matched no known Dependabot pattern"


def summarize_checks(rollup: Optional[list[dict[str, Any]]]) -> tuple[str, str]:
    """Return (gate, human summary). Gate is one of pass / wait / fail / none."""
    if not rollup:
        return "none", "no checks configured"
    states: dict[str, int] = {}
    for check in rollup:
        state = (check.get("state") or check.get("conclusion") or "UNKNOWN").upper()
        states[state] = states.get(state, 0) + 1
    summary = ", ".join(f"{k}:{v}" for k, v in sorted(states.items()))
    if states.keys() - {"SUCCESS", "NEUTRAL", "SKIPPED"}:
        if states.keys() & {"PENDING", "IN_PROGRESS", "QUEUED", "EXPECTED"}:
            return "wait", summary
        return "fail", summary
    return "pass", summary


def triage_repo(repo: str, report: Report) -> None:
    # Probe reachability explicitly. `gh pr list` against a repo the token
    # cannot see exits 0 and prints `[]` — indistinguishable from a genuinely
    # clean repo. Inferring access from an empty list is precisely the
    # silent-zero failure this report exists to avoid, so ask directly.
    try:
        gh_json(["repo", "view", repo, "--json", "name"])
    except RuntimeError as exc:
        report.unreachable.append({"repo": repo, "error": f"not readable: {exc}"})
        return

    try:
        prs = gh_json([
            "pr", "list", "--repo", repo, "--author", "app/dependabot",
            "--state", "open", "--limit", "50",
            "--json", "number,title,mergeable,statusCheckRollup",
        ])
    except RuntimeError as exc:
        report.unreachable.append({"repo": repo, "error": str(exc)})
        return

    report.repos_scanned.append(repo)
    for pr in prs:
        title = pr.get("title", "")
        klass, reason = classify_title(title)
        gate, checks = summarize_checks(pr.get("statusCheckRollup"))
        mergeable = (pr.get("mergeable") or "UNKNOWN").upper()

        verdict, why = klass, reason
        if klass == MERGE:
            if mergeable == "CONFLICTING":
                verdict, why = BLOCKED, f"{reason}; but branch is CONFLICTING"
            elif gate == "fail":
                verdict, why = BLOCKED, f"{reason}; but CI is red"
            elif gate == "wait":
                verdict, why = WAIT, f"{reason}; CI still running"

        report.verdicts.append(
            Verdict(repo=repo, number=pr["number"], title=title, verdict=verdict,
                    reason=why, checks=checks, mergeable=mergeable)
        )


def render_markdown(report: Report) -> str:
    lines = ["# Dependabot triage (report only — nothing was merged)", ""]
    buckets = [MERGE, HOLD, WAIT, BLOCKED, UNCLASSIFIED]
    counts = {b: sum(1 for v in report.verdicts if v.verdict == b) for b in buckets}
    lines.append(
        "  ·  ".join(f"**{b}**: {counts[b]}" for b in buckets)
        + f"  ·  **repos read**: {len(report.repos_scanned)}"
        + f"  ·  **unreachable**: {len(report.unreachable)}"
    )
    lines.append("")

    for bucket in buckets:
        rows = [v for v in report.verdicts if v.verdict == bucket]
        if not rows:
            continue
        lines += [f"## {bucket} ({len(rows)})", "", "| PR | Title | Why | Checks |", "|---|---|---|---|"]
        for v in rows:
            short = v.repo.split("/")[-1]
            lines.append(
                f"| [{short}#{v.number}](https://github.com/{v.repo}/pull/{v.number}) "
                f"| {v.title} | {v.reason} | {v.checks} |"
            )
        lines.append("")

    if report.unreachable:
        lines += ["## UNREACHABLE — not scanned, coverage is incomplete", ""]
        for item in report.unreachable:
            lines.append(f"- `{item['repo']}` — {item['error']}")
        lines.append("")
        lines.append(
            "> A repo listed here contributed **zero** rows above because it could not be read, "
            "not because it is clean. Do not read this report as fleet-wide until this list is empty."
        )
        lines.append("")
    else:
        lines.append("_All target repos were read successfully._")

    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repos", help="comma-separated owner/name list (default: the CIRWEL fleet)")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of markdown")
    args = ap.parse_args()

    repos = [r.strip() for r in args.repos.split(",")] if args.repos else DEFAULT_REPOS
    report = Report()
    for repo in repos:
        triage_repo(repo, report)

    if args.json:
        print(json.dumps({
            "verdicts": [asdict(v) for v in report.verdicts],
            "unreachable": report.unreachable,
            "repos_scanned": report.repos_scanned,
        }, indent=2))
    else:
        print(render_markdown(report))

    # Report-only: an unreadable repo is a coverage failure worth a nonzero exit,
    # so a scheduled run cannot look green while silently covering less than asked.
    return 1 if report.unreachable else 0


if __name__ == "__main__":
    sys.exit(main())
