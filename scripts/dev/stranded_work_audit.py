#!/usr/bin/env python3
"""Stranded-work audit — find branch work that silently never reached master.

The failure mode this catches (bit the repo through 2026-07-01, when a sweep
found 10 branches carrying it): a PR merges, then follow-up commits are pushed
to the same branch. GitHub does not reopen the PR, auto-delete-on-merge does
not fire (the re-push recreates the branch), and no surface ever reports the
gap — so real fixes strand on merged-PR branches indefinitely. A common driver
is a push rejected by a guard (e.g. the repo scope guard) late in a session:
the commit stays on the branch, the session ends, nobody re-lands it.

Classification per remote branch (skips master/main, archive/*, backup/*):

  STRANDED  newest PR for the branch is MERGED, the branch HEAD advanced past
            the merged head, and `git cherry` says some of those commits'
            patches are NOT in master. Real work is marooned -> re-land or
            explicitly discard. THE ALARM CLASS.
  PRUNABLE  branch content is fully contained in master (merged ghost) -> safe
            to delete, pure hygiene.
  DANGLING  unique commits but no PR route (no PR ever, or newest PR CLOSED
            unmerged). Informational: might be parked work, might be abandoned.
  ACTIVE    newest PR is OPEN, or the branch tip is younger than --active-days.
            Not reported.

Uses only git + the `gh` CLI (GITHUB_TOKEN scope; no metered APIs).

Usage:
  python3 scripts/dev/stranded_work_audit.py [--repo owner/name] [--json]
                                             [--check] [--active-days N]

  --check exits 1 when any STRANDED branch is found (for CI gating).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time

SKIP_EXACT = {"master", "main", "HEAD", "origin", "gh-pages"}  # "origin" = the origin/HEAD symref
SKIP_PREFIXES = ("archive/", "backup/")


def run(*cmd: str) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def remote_branches() -> list[str]:
    out = run("git", "for-each-ref", "refs/remotes/origin",
              "--format=%(refname:short)")
    branches = []
    for ref in out.splitlines():
        name = ref.removeprefix("origin/")
        if name in SKIP_EXACT or name.startswith(SKIP_PREFIXES):
            continue
        branches.append(name)
    return branches


def newest_pr(repo: str, branch: str) -> dict | None:
    out = run("gh", "pr", "list", "--repo", repo, "--head", branch,
              "--state", "all", "--limit", "1",
              "--json", "number,state,headRefOid")
    prs = json.loads(out)
    return prs[0] if prs else None


def unmerged_patch_count(branch: str, since: str | None = None) -> int:
    """Commits on the branch whose patch-id is absent from origin/master.

    `since` restricts to commits after a known-merged head (the post-merge
    advance); without it the whole branch is compared.
    """
    args = ["git", "cherry", "origin/master", f"origin/{branch}"]
    if since:
        args.append(since)
    out = run(*args)
    return sum(1 for line in out.splitlines() if line.startswith("+"))


def tip_age_days(branch: str) -> int:
    ts = int(run("git", "log", "-1", "--format=%ct", f"origin/{branch}").strip())
    return int((time.time() - ts) // 86400)


def audit(repo: str, active_days: int) -> list[dict]:
    findings = []
    for branch in remote_branches():
        sha = run("git", "rev-parse", f"origin/{branch}").strip()
        pr = newest_pr(repo, branch)
        age = tip_age_days(branch)

        if pr and pr["state"] == "OPEN":
            continue  # ACTIVE: the PR is the tracking surface

        if pr and pr["state"] == "MERGED":
            merged_head = pr["headRefOid"]
            if merged_head == sha:
                findings.append({"branch": branch, "class": "PRUNABLE",
                                 "detail": f"PR #{pr['number']} merged at this exact head; "
                                           "auto-delete missed it (likely re-push residue)"})
                continue
            # Branch advanced past the merged head — is the advance in master?
            since = merged_head if _known_object(merged_head) else None
            stranded = unmerged_patch_count(branch, since)
            if stranded:
                findings.append({"branch": branch, "class": "STRANDED",
                                 "detail": f"{stranded} commit(s) pushed after PR "
                                           f"#{pr['number']} merged; patches NOT in master"})
            else:
                findings.append({"branch": branch, "class": "PRUNABLE",
                                 "detail": f"advanced past merged PR #{pr['number']} but all "
                                           "patches landed in master"})
            continue

        # No PR, or newest PR closed-unmerged.
        if age < active_days:
            continue  # ACTIVE: recent tip, assume a session is still on it
        unique = unmerged_patch_count(branch)
        if unique == 0:
            findings.append({"branch": branch, "class": "PRUNABLE",
                             "detail": "no open route needed — content is in master"})
        else:
            route = f"PR #{pr['number']} CLOSED unmerged" if pr else "no PR ever opened"
            findings.append({"branch": branch, "class": "DANGLING",
                             "detail": f"{unique} unique commit(s), {route}, "
                                       f"tip {age}d old"})
    return findings


def _known_object(sha: str) -> bool:
    return subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"],
                          capture_output=True).returncode == 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default="cirwel/unitares")
    ap.add_argument("--json", action="store_true", dest="as_json")
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if any STRANDED branch is found")
    ap.add_argument("--active-days", type=int, default=7,
                    help="tips younger than this with no merged PR are ACTIVE (default 7)")
    args = ap.parse_args()

    findings = audit(args.repo, args.active_days)
    order = {"STRANDED": 0, "DANGLING": 1, "PRUNABLE": 2}
    findings.sort(key=lambda f: (order[f["class"]], f["branch"]))

    if args.as_json:
        print(json.dumps(findings, indent=2))
    elif not findings:
        print("stranded-work audit: clean — no stranded/dangling/prunable branches.")
    else:
        width = max(len(f["branch"]) for f in findings)
        for f in findings:
            print(f"{f['class']:<9} {f['branch']:<{width}}  {f['detail']}")

    stranded = sum(1 for f in findings if f["class"] == "STRANDED")
    if stranded:
        print(f"\n{stranded} STRANDED branch(es) — real work marooned off master; "
              "re-land (fresh branch off master, cherry-pick, PR) or explicitly discard.",
              file=sys.stderr)
    return 1 if (args.check and stranded) else 0


if __name__ == "__main__":
    sys.exit(main())
