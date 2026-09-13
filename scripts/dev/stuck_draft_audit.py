#!/usr/bin/env python3
"""Stuck-draft audit — find drafts that are finished and waiting on nobody.

THE DEADLOCK THIS MEASURES
--------------------------
`CLAUDE.md` makes readiness agent-declared: the owning agent marks its own PR
ready "once its validation actually passed (CI green, review round joined),"
and until then draft means "still working — hands off."

This repo runs no review bot. So "review round joined" cannot happen without a
human. But draft also tells that human to keep hands off, and merging is their
act too. The precondition for the automatic step therefore depends on the
manual step, and a green draft whose session has ended has no exit that is not
a person noticing it. Drafts accumulate at the rate sessions produce them and
drain only at the rate one maintainer remembers them.

That is a circular dependency in the contract, not an agent being lazy. Agents
leave these drafts *correctly*: PR #2191 says in its own body "no independent
reviewer has looked at this," which is true, and which the contract says is a
reason to stay draft.

WHAT THIS SCRIPT DOES AND DOES NOT DO
-------------------------------------
It reports. It never flips a draft to ready, never merges, never comments on a
PR. `CLAUDE.md` reserves readiness to the owning agent and merging to the
maintainer, and a script that quietly took either would be exactly the "silent
green button" that section forbids. Whether anything should close this loop
automatically is an operator decision; this audit only makes the queue visible
so that decision is made against a list rather than a memory.

Classification per OPEN DRAFT pull request:

  UNBLOCKED   All expected required CI is present and green on the head commit,
              mergeability is confirmed, no changes requested or unresolved
              threads, and untouched for longer than --quiet-hours. A candidate
              for a readiness decision; inactivity does not prove work is done.
  CONFLICTED  merge conflict against the base branch. Needs a base merge from
              whoever owns it.
  CI-RED      at least one failing check on the head commit.
  REVIEW-OPEN changes requested or unresolved review threads.
  CI-PENDING  checks running/missing, or required check policy unreadable.
  UNKNOWN     mergeability or review state could not be established.
  IN-FLIGHT   touched within --quiet-hours; the owner may still be working.
              Not reported.

`age_profile` tallies how long open drafts have been open, in total and for the
UNBLOCKED class alone. The classes say what to DO about one PR; the profile says
what the backlog is costing. Those differ: one PR sitting a week is a decision
someone deferred, while a steady median of several days is a drain rate problem,
and only the second is fixed by changing the process rather than by clearing the
list once.

GITHUB_TOKEN-only via the `gh` CLI — no metered APIs (execution-cost policy).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from urllib.parse import quote

CLASS_ORDER = {
    "UNBLOCKED": 0,
    "CONFLICTED": 1,
    "CI-RED": 2,
    "REVIEW-OPEN": 3,
    "CI-PENDING": 4,
    "UNKNOWN": 5,
}

ALARM_CLASSES = ("UNBLOCKED", "CONFLICTED", "CI-RED")


def run(*cmd: str) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def open_drafts(repo: str) -> list[dict]:
    """Every open draft PR with the fields classification needs.

    `statusCheckRollup` is the head commit's checks; `reviewDecision` and
    `reviewThreads` cover review state; `mergeable` is GitHub's conflict read
    (CONFLICTING / MERGEABLE / UNKNOWN — UNKNOWN means it has not computed yet).
    """
    out = run(
        "gh", "pr", "list",
        "--repo", repo,
        "--state", "open",
        "--limit", "200",
        "--json",
        "number,title,isDraft,createdAt,updatedAt,headRefName,headRefOid,baseRefName,author,"
        "mergeable,statusCheckRollup,reviewDecision,reviewRequests",
    )
    return [pr for pr in json.loads(out) if pr.get("isDraft")]


def required_checks(repo: str, branch: str) -> list[dict] | None:
    """Read expected contexts and apps from protection and effective rulesets.

    The rollup only contains checks that exist. It cannot establish whether a
    required workflow never started (for example, awaiting bot-run approval).
    An unreadable policy remains unknown; an unprotected base declares no gate.
    """
    owner, _, name = repo.partition("/")
    query = """
      query($owner:String!, $name:String!, $ref:String!) {
        repository(owner:$owner, name:$name) {
          ref(qualifiedName:$ref) {
            branchProtectionRule {
              requiredStatusChecks { context app { databaseId } }
            }
          }
        }
      }
    """
    try:
        protection = json.loads(run(
            "gh", "api", "graphql", "-f", f"query={query}",
            "-F", f"owner={owner}", "-F", f"name={name}",
            "-F", f"ref=refs/heads/{branch}",
        ))
        if not isinstance(protection, dict) or protection.get("errors"):
            return None
        rule = protection["data"]["repository"]["ref"]["branchProtectionRule"]
        checks = [
            {"context": check["context"], "app_id": check["app"]["databaseId"] if check["app"] else None}
            for check in rule["requiredStatusChecks"] or []
        ] if rule else []
        pages = json.loads(run(
            "gh", "api", "--paginate", "--slurp",
            f"repos/{repo}/rules/branches/{quote(branch, safe='')}",
        ))
        for rules in pages:
            for rule in rules:
                if rule["type"] in {"workflows", "code_scanning", "required_deployments"}:
                    # These gates cannot be established from named check
                    # contexts. Do not silently certify the subset we can read.
                    return None
                if rule["type"] == "required_status_checks":
                    checks.extend(
                        {"context": check["context"], "app_id": check.get("integration_id")}
                        for check in rule["parameters"]["required_status_checks"]
                    )
        if any(
            not isinstance(check["context"], str) or not check["context"]
            or (check["app_id"] is not None and (type(check["app_id"]) is not int or check["app_id"] <= 0))
            for check in checks
        ):
            return None
        return checks
    except (subprocess.CalledProcessError, KeyError, TypeError, ValueError):
        return None


def head_check_rollup(repo: str, oid: str) -> list[dict] | None:
    """Read checks with their producing app at the exact sampled PR head.

    ``gh pr list`` omits app identity. A same-name check from another app must
    not satisfy an app-constrained requirement. Unknown/truncated reads cannot
    establish complete coverage.
    """
    owner, _, name = repo.partition("/")
    query = """
      query($owner:String!, $name:String!, $oid:GitObjectID!) {
        repository(owner:$owner, name:$name) {
          object(oid:$oid) { ... on Commit {
            statusCheckRollup { contexts(first:100) {
              pageInfo { hasNextPage }
              nodes {
                ... on CheckRun { name status conclusion checkSuite { app { databaseId } } }
                ... on StatusContext { context state }
              }
            } }
          } }
        }
      }
    """
    try:
        payload = json.loads(run(
            "gh", "api", "graphql", "-f", f"query={query}",
            "-F", f"owner={owner}", "-F", f"name={name}", "-F", f"oid={oid}",
        ))
        if not isinstance(payload, dict) or payload.get("errors"):
            return None
        rollup = payload["data"]["repository"]["object"]["statusCheckRollup"]
        if rollup is None:
            return []
        connection = rollup["contexts"]
        if connection["pageInfo"]["hasNextPage"]:
            return None
        checks = connection["nodes"]
        for check in checks:
            if "name" in check:
                app = check["checkSuite"]["app"]
                check["app_id"] = app["databaseId"] if app else None
        return checks
    except (subprocess.CalledProcessError, KeyError, TypeError, ValueError):
        return None


def unresolved_threads(repo: str, number: int) -> int | None:
    """Count unresolved review threads, or None if the query failed.

    None is deliberately not folded into 0: "no unresolved threads" and "could
    not read the threads" are different findings, and a degraded audit that
    reports the healthy one is this repo's named recurring failure mode.
    """
    query = """
      query($owner:String!, $name:String!, $number:Int!) {
        repository(owner:$owner, name:$name) {
          pullRequest(number:$number) {
            reviewThreads(first:100) {
              nodes { isResolved }
              pageInfo { hasNextPage }
            }
          }
        }
      }
    """
    owner, _, name = repo.partition("/")
    try:
        out = run(
            "gh", "api", "graphql",
            "-f", f"query={query}",
            "-F", f"owner={owner}",
            "-F", f"name={name}",
            "-F", f"number={number}",
        )
    except subprocess.CalledProcessError:
        return None
    try:
        payload = json.loads(out)
        if not isinstance(payload, dict) or payload.get("errors"):
            return None
        connection = payload["data"]["repository"]["pullRequest"]["reviewThreads"]
        if connection["pageInfo"]["hasNextPage"]:
            return None  # A truncated clean prefix is not a clean review.
        nodes = connection["nodes"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return None
    return sum(1 for n in nodes if not n.get("isResolved"))


def check_state(pr: dict) -> str:
    """Reduce the head commit's check rollup to green / red / pending.

    A SKIPPED or NEUTRAL conclusion is not a failure: this repo's conditional
    Elixir jobs skip on most PRs and counting those as unfinished would park
    every PR in CI-PENDING forever.
    """
    rollup = pr.get("statusCheckRollup") or []
    if not rollup:
        return "pending"
    failed = False
    required = pr.get("requiredStatusChecks")
    # No declared gate is insufficient evidence for this audit's CI-green
    # claim, including stacked PRs targeting an unprotected feature branch.
    pending = not required or any(
        not any(
            (check.get("name") or check.get("context")) == rule["context"]
            and (rule["app_id"] is None or check.get("app_id") == rule["app_id"])
            for check in rollup
        )
        for rule in required
    )
    for check in rollup:
        # Check runs use status/conclusion; legacy commit statuses use state.
        if check.get("status") is not None:
            if check.get("status") != "COMPLETED":
                pending = True
                continue
            conclusion = (check.get("conclusion") or "").upper()
            if conclusion in ("FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"):
                failed = True
            elif conclusion not in ("SUCCESS", "SKIPPED", "NEUTRAL"):
                pending = True
        else:
            state = (check.get("state") or "").upper()
            if state in ("FAILURE", "ERROR"):
                failed = True
            elif state != "SUCCESS":
                pending = True
    if failed:
        return "red"
    return "pending" if pending else "green"


def classify(pr: dict, threads: int | None, quiet_hours: float, now: datetime) -> dict:
    idle_h = (now - _parse_ts(pr["updatedAt"])).total_seconds() / 3600
    age_h = (now - _parse_ts(pr["createdAt"])).total_seconds() / 3600

    finding = {
        "number": pr["number"],
        "title": pr["title"],
        "branch": pr["headRefName"],
        "author": (pr.get("author") or {}).get("login"),
        "age_hours": round(age_h, 1),
        "idle_hours": round(idle_h, 1),
        "unresolved_threads": threads,
    }

    if idle_h < quiet_hours:
        finding["class"] = "IN-FLIGHT"
        finding["reason"] = f"touched {idle_h:.1f}h ago; owner may still be working"
        return finding

    mergeable = (pr.get("mergeable") or "UNKNOWN").upper()
    checks = check_state(pr)
    review = pr.get("reviewDecision")

    if mergeable == "CONFLICTING":
        finding["class"] = "CONFLICTED"
        finding["reason"] = "merge conflict against the base branch"
    elif checks == "red":
        finding["class"] = "CI-RED"
        finding["reason"] = "at least one failing check on the head commit"
    elif review == "CHANGES_REQUESTED":
        finding["class"] = "REVIEW-OPEN"
        finding["reason"] = "changes requested in a review; waiting on the author"
    elif threads is None:
        finding["class"] = "CI-PENDING"
        finding["reason"] = "review threads could not be read; state indeterminate"
    elif threads > 0:
        finding["class"] = "REVIEW-OPEN"
        finding["reason"] = f"{threads} unresolved review thread(s); waiting on the author"
    elif checks == "pending":
        finding["class"] = "CI-PENDING"
        finding["reason"] = "checks running/missing, or expected required CI could not be established"
    elif mergeable != "MERGEABLE":
        finding["class"] = "UNKNOWN"
        finding["reason"] = "mergeability has not been established"
    elif "reviewDecision" not in pr or review not in (None, "", "APPROVED", "REVIEW_REQUIRED"):
        finding["class"] = "UNKNOWN"
        finding["reason"] = "review decision could not be established"
    elif review == "REVIEW_REQUIRED":
        finding["class"] = "REVIEW-OPEN"
        finding["reason"] = "required review approval is outstanding"
    else:
        finding["class"] = "UNBLOCKED"
        finding["reason"] = (
            f"green, mergeable, no open threads, untouched {idle_h:.1f}h "
            "— candidate for a readiness decision; completion is not established"
        )
    return finding


def age_profile(findings: list[dict]) -> dict:
    """Backlog cost, separate from any single PR's verdict."""
    def stats(rows: list[dict]) -> dict:
        if not rows:
            return {"count": 0}
        ages = sorted(f["age_hours"] for f in rows)
        mid = len(ages) // 2
        median = ages[mid] if len(ages) % 2 else (ages[mid - 1] + ages[mid]) / 2
        return {
            "count": len(ages),
            "median_age_hours": round(median, 1),
            "oldest_age_hours": round(ages[-1], 1),
        }

    reported = [f for f in findings if f["class"] != "IN-FLIGHT"]
    return {
        "all_open_drafts": stats(findings),
        "reported": stats(reported),
        "unblocked": stats([f for f in findings if f["class"] == "UNBLOCKED"]),
    }


def audit(repo: str, quiet_hours: float) -> list[dict]:
    now = datetime.now(timezone.utc)
    findings = []
    policies = {}
    for pr in open_drafts(repo):
        idle_h = (now - _parse_ts(pr["updatedAt"])).total_seconds() / 3600
        # Only pay for the GraphQL round-trip on PRs that could be reported.
        threads = unresolved_threads(repo, pr["number"]) if idle_h >= quiet_hours else 0
        if idle_h >= quiet_hours:
            base = pr["baseRefName"]
            if base not in policies:
                policies[base] = required_checks(repo, base)
            pr["requiredStatusChecks"] = policies[base]
            pr["statusCheckRollup"] = head_check_rollup(repo, pr["headRefOid"])
        findings.append(classify(pr, threads, quiet_hours, now))
    return findings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default="cirwel/unitares")
    ap.add_argument("--json", action="store_true", dest="as_json")
    ap.add_argument(
        "--quiet-hours",
        type=float,
        default=12.0,
        help="a draft untouched for longer than this is no longer IN-FLIGHT (default 12)",
    )
    ap.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if any UNBLOCKED, CONFLICTED or CI-RED draft is found",
    )
    args = ap.parse_args()

    findings = audit(args.repo, args.quiet_hours)
    profile = age_profile(findings)
    reported = [f for f in findings if f["class"] != "IN-FLIGHT"]
    reported.sort(key=lambda f: (CLASS_ORDER[f["class"]], -f["age_hours"]))

    if args.as_json:
        print(json.dumps({"findings": reported, "age_profile": profile}, indent=2))
    elif not reported:
        print(
            f"stuck-draft audit: clean — {profile['all_open_drafts']['count']} open draft(s), "
            "all touched recently."
        )
    else:
        for f in reported:
            threads = "?" if f["unresolved_threads"] is None else f["unresolved_threads"]
            print(
                f"{f['class']:<11} #{f['number']:<5} {f['age_hours']:>6.1f}h old  "
                f"idle {f['idle_hours']:>6.1f}h  threads={threads}  {f['title'][:64]}"
            )
            print(f"{'':<11} └─ {f['reason']}")
        print()
        allp = profile["all_open_drafts"]
        unb = profile["unblocked"]
        print(
            f"open drafts: {allp['count']}  "
            f"median age {allp.get('median_age_hours', 0)}h  "
            f"oldest {allp.get('oldest_age_hours', 0)}h"
        )
        print(
            f"UNBLOCKED (readiness candidates, completion unproven): {unb['count']}"
            + (f"  median age {unb['median_age_hours']}h" if unb["count"] else "")
        )

    if args.check and any(f["class"] in ALARM_CLASSES for f in reported):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
