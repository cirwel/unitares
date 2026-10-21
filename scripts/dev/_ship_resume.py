#!/usr/bin/env python3
"""Resume an owned, pushed docs/tests/tooling UNITARES PR using live evidence.

No local checkpoint can authorize readiness: the PR head, ownership, reviews,
CI, and reserved-path holds are rechecked on each invocation. Exit 2 parks
incomplete work; running the same command again reuses recorded review work.
"""

from __future__ import annotations

import argparse
from fnmatch import fnmatchcase
import json
import os
import re
import shutil
import subprocess
import sys
from uuid import UUID


class Parked(RuntimeError):
    pass


class CommandFailed(Parked):
    def __init__(self, args: tuple[str, ...], result: subprocess.CompletedProcess):
        self.returncode = result.returncode
        self.output = result.stdout + result.stderr
        super().__init__(f"{' '.join(args)} exited {result.returncode}:\n{self.output}")


def run(*args: str, remote: bool = False, read_only: bool = False) -> str:
    command = list(args)
    if remote:
        if shutil.which("unitares-github"):
            command = ["unitares-github", *(["--read-only"] if read_only else []), "--", *command]
        elif not (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")):
            raise Parked("No App runner or explicitly provisioned GitHub token; refusing implicit gh login.")
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode:
        raise CommandFailed(args, result)
    return result.stdout


def snapshot() -> dict:
    return json.loads(run("gh", "pr", "view", "--json",
                          "number,url,state,body,headRefName,headRefOid,baseRefName,baseRefOid,"
                          "isDraft,isCrossRepository,labels,statusCheckRollup,comments",
                          remote=True, read_only=True))


def owned_head(pr: dict, agent: str) -> str:
    branch = run("git", "branch", "--show-current").strip()
    head = run("git", "rev-parse", "HEAD").strip()
    if not branch or branch in {"main", "master", pr["baseRefName"]}:
        raise Parked("Resume requires a named feature branch.")
    if pr["baseRefName"] != "master":
        raise Parked("The serial queue only accepts PRs targeting master; stacked PRs stay parked.")
    if run("git", "status", "--porcelain=v1", "-uall").strip():
        raise Parked("Commit and push local changes before resuming; resume never stages or commits.")
    footers = re.findall(r"^UNITARES agent: ([0-9a-fA-F-]+)\s*$", pr["body"], re.MULTILINE)
    if not footers or footers[-1].lower() != agent:
        raise Parked("PR ownership footer does not match UNITARES_AGENT_UUID; record an explicit handoff first.")
    if pr["state"] not in {"OPEN", "MERGED"} or pr["isCrossRepository"]:
        raise Parked("Resume requires an open, same-repository PR (or an already merged result).")
    if pr["headRefName"] != branch or pr["headRefOid"] != head:
        raise Parked("Local branch and PR head differ; inspect and synchronize before resuming.")
    return head


def changed_paths(pr: dict) -> list[str]:
    return [path for path in run("git", "diff", "--no-ext-diff", "--name-only", "-z",
                                 f"{pr['baseRefOid']}...HEAD").split("\0") if path]


def mandatory_review(pr: dict) -> bool:
    policy = json.loads(run("git", "show", f"{pr['baseRefOid']}:scripts/dev/review_policy.json"))
    patterns = policy["second_family_paths"]
    return any(fnmatchcase(path, pattern) for path in changed_paths(pr) for pattern in patterns)


def reserved_hold(pr: dict) -> None:
    labels = {label["name"] for label in pr["labels"]}
    if labels & {"governance-sensitive", "automerge-hold", "no-auto-merge", "no-auto-review", "operator-armed"}:
        raise Parked("PR carries an explicit governance, review, or merge hold.")
    classification = re.search(r"\b(?:classification|class)\s*:\s*[*` ]*(fix|capability)\b",
                               pr["body"], re.IGNORECASE)
    if classification is None or classification[1].lower() != "fix":
        raise Parked("Declare Classification: fix; capability readiness needs its operator ruling.")
    # The contract delegates docs, tests and tooling delivery. Runtime auth or
    # identity changes can require an operator decision even when absent from
    # the narrower governance-sensitivity manifest. Do not infer that decision.
    delegated = ("docs/", "tests/", "scripts/dev/", "scripts/diagnostics/", ".github/workflows/")
    if any(not path.startswith(delegated) for path in changed_paths(pr)):
        raise Parked("Automatic readiness is limited to delegated docs/tests/tooling paths; operator decisions stay explicit.")
    # Read the manifest from the actual base commit, never the branch's copy.
    manifest = run("git", "show", f"{pr['baseRefOid']}:scripts/dev/governance_sensitivity_manifest.tsv")
    for row in manifest.splitlines():
        if not row.strip() or row.startswith("#"):
            continue
        path, symbol, why = row.split("\t", 2)
        patch = run("git", "diff", "--no-ext-diff", "-U0", f"{pr['baseRefOid']}...HEAD",
                    "--", f":(literal){path}")
        changed = "\n".join(line for line in patch.splitlines()
                            if line[:1] in {"+", "-"} and not line.startswith(("+++", "---")))
        if patch and (symbol == "-" or re.search(symbol, changed)):
            raise Parked(f"Operator merge required: {path}: {why}")


def approval_hold(pr: dict) -> None:
    pages = json.loads(run("gh", "api", f"repos/{{owner}}/{{repo}}/issues/{pr['number']}/events?per_page=100",
                           "--paginate", "--slurp", remote=True, read_only=True))
    events = [event for page in pages for event in page
              if event.get("event") in {"labeled", "unlabeled"}
              and event.get("label", {}).get("name") == "approved-to-merge"]
    if events and max(events, key=lambda event: event["id"])["event"] == "unlabeled":
        raise Parked("Queue approval was removed; label history preserves this hold across subsequent pushes.")


def check_ci(pr: dict) -> None:
    # Required checks may be missing or cancelled: gh's required-check query
    # must succeed as well as every published test finishing without failure.
    required = json.loads(run("gh", "pr", "checks", str(pr["number"]), "--required",
                              "--json", "bucket,name", remote=True, read_only=True))
    if not required or any(c["bucket"] not in {"pass", "skipping"} for c in required):
        raise Parked("Required CI is incomplete or failing.")
    checks = pr["statusCheckRollup"]
    if not checks:
        raise Parked("No CI evidence is available.")
    for check in checks:
        if check.get("__typename") == "StatusContext":
            good = check.get("state") == "SUCCESS"
        else:
            good = (check.get("status") == "COMPLETED" and check.get("conclusion") in
                    {"SUCCESS", "SKIPPED", "NEUTRAL", "CANCELLED"})
        if not good:
            raise Parked(f"CI is pending or failing: {check.get('name', check.get('context', 'unknown'))}")
    # A queued/cancelled workflow may not have published its aggregate check
    # yet. Inspect runs too, retaining the newest run of each workflow at HEAD.
    runs = json.loads(run("gh", "run", "list", "--commit", pr["headRefOid"],
                          "--branch", pr["headRefName"], "--event", "pull_request",
                          "--limit", "100", "--json", "databaseId,workflowDatabaseId,status,conclusion",
                          remote=True, read_only=True))
    if not runs or len(runs) == 100:
        raise Parked("Workflow evidence is absent or exceeds the bounded inventory.")
    latest = {}
    for workflow in sorted(runs, key=lambda item: item["databaseId"], reverse=True):
        latest.setdefault(workflow["workflowDatabaseId"], workflow)
    if any(w["status"] != "completed" or w["conclusion"] not in {"success", "skipped", "neutral"}
           for w in latest.values()):
        raise Parked("A current PR workflow is queued, running, cancelled, or failing.")


def resume(*, reviewer: str | None, wait: bool, plan: bool) -> None:
    try:
        agent = str(UUID(os.environ.get("UNITARES_AGENT_UUID", "")))
    except ValueError as error:
        raise Parked("Set UNITARES_AGENT_UUID to this process's bound UNITARES identity.") from error
    pr = snapshot()
    head = owned_head(pr, agent)
    if pr["state"] == "MERGED":
        print(f"[ship] already merged: {pr['url']}")
        return
    reserved_hold(pr)
    approval_hold(pr)
    if plan:
        print(f"[ship] resume {pr['url']} at {head}: review -> CI -> prepr -> ready/queue -> closeout")
        return
    marker = f"<!-- unitares-ship-ready head={head} agent={agent} -->"
    recorded = any(marker in comment["body"] for comment in pr["comments"])
    prior_readiness = any("<!-- unitares-ship-ready " in comment["body"]
                          and f"agent={agent} -->" in comment["body"] for comment in pr["comments"])
    labelled = "approved-to-merge" in {label["name"] for label in pr["labels"]}
    if prior_readiness and not labelled:
        raise Parked("Queue approval was removed after readiness; do not overwrite that hold automatically.")
    unreviewed = None
    try:
        if shutil.which("unitares-github"):
            run("review", *(["--reviewer", reviewer] if reviewer else []), remote=True)
        else:
            run("./scripts/dev/review.sh", *(["--reviewer", reviewer] if reviewer else []))
    except CommandFailed as error:
        # Exit 2 can also mean a pending native review or declined follow-up;
        # those are not provider outages. Known findings and mandatory model
        # family coverage remain blockers. CI is independently checked below.
        text = error.output.lower()
        unavailable = any(word in text for word in ("unavailable", "disabled", "no reviewer completed"))
        unfinished = any(word in text for word in ("pending", "still running", "scope was declined", "full_required", "scope not confirmed"))
        if (error.returncode != 2 or not reviewer or not unavailable or unfinished
                or mandatory_review(pr)):
            raise
        unreviewed = error.output
    if wait:
        # An optional cancelled workflow can make --watch exit nonzero. The
        # complete snapshot and required checks below remain authoritative.
        try:
            run("gh", "pr", "checks", str(pr["number"]), "--watch", "--interval", "30",
                remote=True, read_only=True)
        except Parked:
            pass
    current = snapshot()
    if owned_head(current, agent) != head or current["baseRefOid"] != pr["baseRefOid"]:
        raise Parked("PR head or base moved during validation; resume to inspect the new evidence.")
    reserved_hold(current)
    check_ci(current)
    run("git", "prepr", remote=True, read_only=True)
    # Re-read ownership, holds and CI immediately before the readiness write.
    final = snapshot()
    if owned_head(final, agent) != head or final["baseRefOid"] != current["baseRefOid"]:
        raise Parked("PR changed before readiness; resume against the current head.")
    reserved_hold(final)
    check_ci(final)
    approval_hold(final)
    final_labelled = "approved-to-merge" in {label["name"] for label in final["labels"]}
    if labelled and not final_labelled:
        raise Parked("Queue approval was removed during validation; leave the hold in place.")
    advisory_marker = f"<!-- unitares-ship-unreviewed head={head} agent={agent} -->"
    if unreviewed and not any(advisory_marker in c["body"] for c in final["comments"]):
        run("gh", "pr", "comment", str(pr["number"]), "--body",
            advisory_marker + "\nAdvisory review unavailable for this delegated ordinary fix; proceeding under the "
            "documented exception after CI. Reviewer output:\n\n" + unreviewed
            + f"\nUNITARES agent: {agent}", remote=True)
    if final["isDraft"]:
        run("gh", "pr", "ready", str(pr["number"]), remote=True)
    if final_labelled and not recorded:
        # A label from before a push must be deliberately renewed after the
        # new head passes. The receipt below makes repeated resume idempotent.
        run("gh", "pr", "edit", str(pr["number"]), "--remove-label", "approved-to-merge", remote=True)
    if not final_labelled or not recorded:
        run("gh", "pr", "edit", str(pr["number"]), "--add-label", "approved-to-merge", remote=True)
    if not recorded:
        run("gh", "pr", "comment", str(pr["number"]), "--body",
            f"{marker}\nDelivery resumed after checking ownership, review status, CI, reserved paths and prepr.\n"
            f"UNITARES agent: {agent}", remote=True)
    run("git", "closeout", "--strict", remote=True, read_only=True)
    print(f"[ship] ready and queued: {pr['url']} at {head}; the queue owns base updates and landing.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reviewer", choices=["codex", "claude", "antigravity"])
    parser.add_argument("--wait", action="store_true", help="join running CI before inspecting readiness")
    parser.add_argument("--plan", action="store_true", help="inspect ownership and holds without writing")
    args = parser.parse_args()
    try:
        resume(reviewer=args.reviewer, wait=args.wait, plan=args.plan)
    except (Parked, json.JSONDecodeError, OSError, KeyError, ValueError) as error:
        print(f"[ship] parked: {error}\n[ship] resume: ./scripts/dev/ship.sh --resume", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
