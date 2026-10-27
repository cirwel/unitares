#!/usr/bin/env python3
"""Classify open Dependabot PRs for explicitly selected repositories. Report only.

REPORT ONLY: this never merges. It prints a proposed classification for review;
the caller decides what action to take.

Honesty contract: an empty report must never be indistinguishable from a
broken one. Every repo we could not read is listed under UNREACHABLE, and
every PR we could not classify is listed under UNCLASSIFIED. A silent zero
is the failure mode this replaces (see the KG sweep that emitted an empty
candidate list for five weeks).

Usage:
    python3 scripts/dev/dependabot_triage.py --repos owner/repo,owner/other [--json]
    python3 scripts/dev/dependabot_triage.py --repos owner/repo \
        --receipt /path/to/dependency-steward.json --fail-on-actionable

The optional receipt is stateful but still read-only with respect to GitHub. It
assigns work to a stable role, not to the short-lived process writing it, and
only records completion after GitHub reports MERGED or CLOSED.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

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
    check_gate: str = "none"
    head_sha: str = ""
    merge_state: str = ""
    github_state: str = "OPEN"
    is_draft: bool = False
    approved: bool = False
    armed: bool = False
    url: str = ""


@dataclass
class Report:
    verdicts: list[Verdict] = field(default_factory=list)
    unreachable: list[dict[str, str]] = field(default_factory=list)
    repos_scanned: list[str] = field(default_factory=list)


def gh_json(args: list[str], *, executable: str = "gh") -> Any:
    """Run a gh command expecting JSON. Raises RuntimeError with stderr on failure."""
    try:
        proc = subprocess.run([executable, *args], capture_output=True, text=True)
    except OSError as exc:
        raise RuntimeError(f"could not execute GitHub reader: {exc}") from exc
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


def triage_repo(repo: str, report: Report, *, gh_executable: str = "gh") -> None:
    # Probe reachability explicitly. `gh pr list` against a repo the token
    # cannot see exits 0 and prints `[]` — indistinguishable from a genuinely
    # clean repo. Inferring access from an empty list is precisely the
    # silent-zero failure this report exists to avoid, so ask directly.
    try:
        gh_json(["repo", "view", repo, "--json", "name"], executable=gh_executable)
    except RuntimeError as exc:
        report.unreachable.append({"repo": repo, "error": f"not readable: {exc}"})
        return

    try:
        prs = gh_json([
            "pr", "list", "--repo", repo, "--author", "app/dependabot",
            "--state", "open", "--limit", "500",
            "--json", "number,title,mergeable,mergeStateStatus,statusCheckRollup,"
            "headRefOid,state,isDraft,labels,autoMergeRequest,url",
        ], executable=gh_executable)
    except RuntimeError as exc:
        report.unreachable.append({"repo": repo, "error": str(exc)})
        return

    report.repos_scanned.append(repo)
    for pr in prs:
        title = pr.get("title", "")
        klass, reason = classify_title(title)
        gate, checks = summarize_checks(pr.get("statusCheckRollup"))
        mergeable = (pr.get("mergeable") or "UNKNOWN").upper()
        labels = {item.get("name", "") for item in pr.get("labels") or []}

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
                    reason=why, checks=checks, mergeable=mergeable,
                    check_gate=gate, head_sha=pr.get("headRefOid") or "",
                    merge_state=(pr.get("mergeStateStatus") or "UNKNOWN").upper(),
                    github_state=(pr.get("state") or "OPEN").upper(),
                    is_draft=bool(pr.get("isDraft")),
                    approved="approved-to-merge" in labels,
                    armed=pr.get("autoMergeRequest") is not None,
                    url=pr.get("url") or f"https://github.com/{repo}/pull/{pr['number']}")
        )


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def stewardship_phase(verdict: Verdict) -> tuple[str, str]:
    """Map report classification to an operational phase and explicit next action."""
    if verdict.armed and verdict.merge_state == "BLOCKED" and verdict.check_gate == "pass":
        return (
            "opaque_blocked",
            "Reconcile the missing GitHub merge requirement or repair this exact head.",
        )
    if verdict.armed:
        return "queued", "Keep reconciling until GitHub reports MERGED or CLOSED."
    if verdict.verdict == WAIT:
        return "waiting_ci", "Recheck after the current CI run reaches a conclusion."
    if verdict.verdict == BLOCKED:
        return "repair_needed", "Repair the failing or conflicting exact head, then revalidate."
    if verdict.verdict == HOLD:
        return "review_needed", "Obtain independent review before declaring readiness."
    if verdict.verdict == UNCLASSIFIED:
        return "unclassified", "Classify the dependency change before taking write action."
    if verdict.is_draft:
        return "review_needed", "Complete validation and review before declaring readiness."
    if verdict.approved:
        return "approved", "The merge queue owns the next step; keep reconciling to a terminal state."
    return "ready", "Review, validate, and queue this low-risk candidate under repository policy."


def _load_receipt(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if payload.get("schema") == "unitares.dependency_steward.v1" else {}


def receipt_key(repo: str, number: int | str) -> str:
    """GitHub repository names are case-insensitive; receipt identity is too."""
    return f"{repo.casefold()}#{number}"


def build_stewardship_receipt(
    report: Report,
    *,
    repos: list[str],
    previous: dict[str, Any] | None = None,
    gh_executable: str = "gh",
    observed_at: str | None = None,
    owner_role: str = "dependency-steward",
    allowed_unreachable: set[str] | None = None,
) -> dict[str, Any]:
    """Build durable role-owned state; only GitHub MERGED/CLOSED is terminal."""
    observed_at = observed_at or utc_now()
    previous = previous or {}
    allowed_folded = {repo.casefold() for repo in (allowed_unreachable or set())}
    unreachable_folded = {item["repo"].casefold() for item in report.unreachable}
    old_items: dict[str, dict[str, Any]] = {}
    for item in previous.get("items", []):
        if item.get("repo") and item.get("pr_number") is not None:
            old_items[receipt_key(str(item["repo"]), item["pr_number"])] = item
    items: list[dict[str, Any]] = []
    current_keys: set[str] = set()
    errors: list[dict[str, str]] = []

    for verdict in report.verdicts:
        key = receipt_key(verdict.repo, verdict.number)
        current_keys.add(key)
        old = old_items.get(key, {})
        phase, next_action = stewardship_phase(verdict)
        unchanged = old.get("phase") == phase and old.get("head_sha") == verdict.head_sha
        items.append({
            "key": key,
            "repo": verdict.repo,
            "pr_number": verdict.number,
            "url": verdict.url,
            "title": verdict.title,
            "head_sha": verdict.head_sha,
            "phase": phase,
            "classification": verdict.verdict,
            "reason": verdict.reason,
            "checks": verdict.checks,
            "mergeable": verdict.mergeable,
            "merge_state": verdict.merge_state,
            "approved": verdict.approved,
            "armed": verdict.armed,
            "owner_role": owner_role,
            "first_seen_at": old.get("first_seen_at", observed_at),
            "last_transition_at": old.get("last_transition_at", observed_at) if unchanged else observed_at,
            "next_action": next_action,
            "terminal_state": None,
            "completed_at": None,
            "observation_status": "observed",
        })

    selected = {repo.casefold() for repo in repos}
    for key, old in old_items.items():
        if key in current_keys:
            continue
        repo = str(old.get("repo", ""))
        raw_number = str(old.get("pr_number", ""))
        if repo.casefold() not in selected:
            retained = dict(old)
            retained.update({"key": key, "observation_status": "out_of_scope"})
            items.append(retained)
            continue
        if old.get("terminal_state") in {"MERGED", "CLOSED"}:
            retained = dict(old)
            retained.update({"key": key, "observation_status": "terminal_retained"})
            items.append(retained)
            continue
        if repo.casefold() in unreachable_folded:
            retained = dict(old)
            retained["key"] = key
            if repo.casefold() in allowed_folded:
                retained["observation_status"] = "coverage_unreachable_allowed"
            else:
                retained.update({
                    "phase": "unreachable",
                    "last_transition_at": observed_at,
                    "next_action": "Restore GitHub read coverage; absence from the open list is not completion.",
                    "terminal_state": None,
                    "completed_at": None,
                    "observation_status": "coverage_unreachable",
                })
            items.append(retained)
            continue
        try:
            live = gh_json(
                ["pr", "view", raw_number, "--repo", repo, "--json", "state,mergedAt,closedAt,headRefOid,url"],
                executable=gh_executable,
            )
        except RuntimeError as exc:
            retained = dict(old)
            retained.update({
                "key": key,
                "phase": "unreachable",
                "last_transition_at": observed_at,
                "next_action": "Restore GitHub read coverage; absence from the open list is not completion.",
                "terminal_state": None,
                "completed_at": None,
                "observation_status": "terminal_lookup_failed",
            })
            items.append(retained)
            errors.append({"key": key, "error": str(exc)})
            continue
        state = (live.get("state") or "UNKNOWN").upper()
        if state in {"MERGED", "CLOSED"}:
            terminal = dict(old)
            terminal.update({
                "key": key,
                "head_sha": live.get("headRefOid") or old.get("head_sha", ""),
                "url": live.get("url") or old.get("url", ""),
                "phase": "terminal",
                "last_transition_at": observed_at,
                "next_action": "None; GitHub reported a terminal state.",
                "terminal_state": state,
                "completed_at": live.get("mergedAt") or live.get("closedAt") or observed_at,
                "observation_status": "terminal_confirmed",
            })
            items.append(terminal)
        else:
            retained = dict(old)
            retained.update({
                "key": key,
                "terminal_state": None,
                "completed_at": None,
                "observation_status": "open_absent_from_list",
            })
            items.append(retained)
            errors.append({"key": key, "error": "PR is open but absent from the Dependabot open-PR listing"})

    return {
        "schema": "unitares.dependency_steward.v1",
        "observed_at": observed_at,
        "owner_role": owner_role,
        "completion_contract": "Only GitHub MERGED or CLOSED is terminal.",
        "repos_requested": repos,
        "repos_scanned": report.repos_scanned,
        "unreachable": report.unreachable,
        "allowed_unreachable": sorted(allowed_unreachable or set(), key=str.casefold),
        "reconciliation_errors": errors,
        "items": sorted(items, key=lambda item: (item.get("repo", "").casefold(), item.get("pr_number", 0))),
    }


def write_receipt(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def receipt_needs_attention(payload: dict[str, Any]) -> bool:
    actionable = {"ready", "review_needed", "repair_needed", "opaque_blocked", "unclassified", "unreachable"}
    allowed = {repo.casefold() for repo in payload.get("allowed_unreachable", [])}
    unexpected_unreachable = any(
        item.get("repo", "").casefold() not in allowed for item in payload.get("unreachable", [])
    )
    ignored_observations = {"out_of_scope", "coverage_unreachable_allowed"}
    return unexpected_unreachable or bool(payload.get("reconciliation_errors")) or any(
        item.get("phase") in actionable and item.get("observation_status") not in ignored_observations
        for item in payload.get("items", [])
    )


def coverage_exit_code(unreachable: list[dict[str, str]], allow: set[str]) -> int:
    """Coverage gate: 0 only when every unreachable repo is expected.

    The honesty contract stands — an unreachable repo is always REPORTED —
    but an unreachable set that is exactly a subset of the declared
    allowlist (repos known to be unreadable by policy, e.g. a private repo
    with no fleet token) is partial-by-policy, not a coverage failure.
    Any repo outside the allowlist keeps the run red.
    """
    # GitHub repo names are case-insensitive; the caller's spelling and the
    # target list's spelling must not decide the verdict.
    allow_folded = {a.casefold() for a in allow}
    unexpected = [u for u in unreachable if u["repo"].casefold() not in allow_folded]
    return 1 if unexpected else 0


def render_markdown(report: Report, *, allowed_partial: bool = False) -> str:
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
        if allowed_partial:
            # Neutral wording on purpose: the gate matches repo NAMES only —
            # it cannot distinguish the expected cause (no fleet token) from a
            # rate limit or permission regression on the same repo.
            lines.append(
                "> Partial-by-policy: every repo above is on the caller-supplied "
                "known-unreachable allowlist, so this run exits green-with-caveat. "
                "Unreachability of any repo NOT on that list would have failed the run."
            )
        lines.append("")
    else:
        lines.append("_All target repos were read successfully._")

    return "\n".join(lines)


def repository_selection(value: str) -> list[str]:
    """Require a nonempty, explicit list before making any GitHub reads."""
    repos = [repo.strip() for repo in value.split(",")]
    if any(not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) for repo in repos):
        raise argparse.ArgumentTypeError("expected a comma-separated owner/name list with no empty entries")
    return repos


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repos", required=True, type=repository_selection,
                    help="comma-separated owner/name list; no repositories are selected by default")
    ap.add_argument("--gh-executable", default="gh",
                    help="GitHub reader executable receiving unchanged gh arguments (default: gh)")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of markdown")
    ap.add_argument(
        "--receipt",
        type=Path,
        help="atomically reconcile a durable unitares.dependency_steward.v1 JSON receipt",
    )
    ap.add_argument(
        "--owner-role",
        default="dependency-steward",
        help="stable role that owns receipt reconciliation (never a process UUID)",
    )
    ap.add_argument(
        "--fail-on-actionable",
        action="store_true",
        help="exit 2 when the receipt contains work needing a steward; requires --receipt",
    )
    ap.add_argument(
        "--allow-unreachable",
        default="",
        help="comma-separated repos whose unreachability is expected (partial-"
             "by-policy: still reported, but does not fail the run; any OTHER "
             "unreachable repo still exits 1)",
    )
    args = ap.parse_args()

    if args.fail_on_actionable and args.receipt is None:
        ap.error("--fail-on-actionable requires --receipt")

    repos = args.repos
    allow = {r.strip() for r in args.allow_unreachable.split(",") if r.strip()}
    report = Report()
    for repo in repos:
        triage_repo(repo, report, gh_executable=args.gh_executable)

    exit_code = coverage_exit_code(report.unreachable, allow)
    allowed_partial = bool(report.unreachable) and exit_code == 0

    receipt = None
    if args.receipt is not None:
        receipt = build_stewardship_receipt(
            report,
            repos=repos,
            previous=_load_receipt(args.receipt),
            gh_executable=args.gh_executable,
            owner_role=args.owner_role,
            allowed_unreachable=allow,
        )
        write_receipt(args.receipt, receipt)

    if args.json:
        payload = {
            "verdicts": [asdict(v) for v in report.verdicts],
            "unreachable": report.unreachable,
            "repos_scanned": report.repos_scanned,
        }
        if allow:
            # Only callers who opted into the allowlist see the new key —
            # without the flag the JSON schema is byte-identical to before.
            payload["allowed_partial"] = allowed_partial
        print(json.dumps(payload, indent=2))
    else:
        print(render_markdown(report, allowed_partial=allowed_partial))

    # Report-only: an UNEXPECTED unreadable repo is a coverage failure worth a
    # nonzero exit, so a scheduled run cannot look green while silently
    # covering less than asked. Allowlisted unreachability is partial-by-policy
    # and is still printed in the UNREACHABLE section above.
    if exit_code:
        return exit_code
    if args.fail_on_actionable and receipt is not None and receipt_needs_attention(receipt):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
