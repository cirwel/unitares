#!/usr/bin/env python3
"""Orphan-push tripwire — fail loudly when a push lands on a dead branch.

The loss mode (three confirmed incidents, 2026-08-12/19): a session keeps
committing to a branch after its PR squash-merged. Everything downstream
reads as success — `git push` succeeds (git knows nothing of PRs), the PR
page says MERGED, `merge-base --is-ancestor` is structurally wrong under
squash — and the commits strand until a prune sweep deletes them.

The weekly stranded_work_audit.py catches this class at up to 7 days'
latency. This guard is its real-time counterpart on the same taxonomy:
it runs on every push to an agent branch, and if every PR for that branch
is already MERGED/CLOSED **and the push carries commits beyond what the
PR merged** it fails the run and files/updates a ci-finding issue with
the re-land recipe. A dead-branch push that adds nothing is the audit's
PRUNABLE class — hygiene, deliberately NOT an alarm here.

Squash discipline: a squash-merged branch's own commits are never
ancestors of the default branch, so a naive `master...tip` compare lists
the entire already-merged history. The orphan set is instead anchored on
the merged PR's own head (`headRefOid...tip`): commits beyond what the PR
merged are the orphans, the rest is landed content. For a CLOSED-unmerged
PR nothing was squashed, so the default-branch compare is the right one.

Two honest limits. (1) A push-triggered workflow runs the definition on
the PUSHED ref, so branches cut before this workflow merged never run it;
they keep their weekly-audit coverage, and merge-content-check.yml (which
runs from the base side) covers their merge-adjacent pushes. (2) The
red X and the issue land minutes after the push, not inside the pushing
terminal — this shortens loss-to-record from days to minutes; it does not
interrupt the pusher mid-flow.

Known false positive: deliberately reusing a branch name for a new round
of work fires this guard on pushes made before the new PR opens (ten
branch names carried multiple PRs in the last 400). The issue text says
how to resolve; fresh `<author>/<topic>-<id>` names avoid it entirely.

Fail-open on API errors, but degraded is never silent — see
merge_loss_common.py. When the guard DOES fire, issue-filing failures do
not soften it: the run still exits 1 so the red X survives.

Env (set by .github/workflows/orphan-push-guard.yml):
  GITHUB_REPOSITORY  owner/name
  BRANCH             the pushed branch (github.ref_name)
  PUSHED_SHA         the pushed tip (github.sha)
  DEFAULT_BRANCH     the repo default branch (falls back to master)
"""

from __future__ import annotations

import os
import subprocess
import sys

from merge_loss_common import (
    degraded,
    file_or_comment_finding,
    fingerprint_marker,
    gh_json,
    summary,
)

GUARD = "orphan-push-guard"

GhError = (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError)


def orphan_commits(repo: str, newest_pr: dict, sha: str, base: str):
    """The commits this push carries beyond what the branch's PR landed.

    Anchored on the merged PR's head when there is one (squash-safe: the
    orphans are exactly the commits beyond what got squashed); on the
    default branch for a CLOSED-unmerged PR (nothing was squashed, so the
    whole unlanded tail is the finding). Returns (commits | None, error_note).
    """
    anchor = newest_pr.get("headRefOid") if newest_pr.get("state") == "MERGED" else None
    anchor = anchor or base
    try:
        cmp = gh_json("api", f"repos/{repo}/compare/{anchor}...{sha}")
    except GhError:
        return None, f"compare `{anchor[:12]}...{sha[:9]}` failed — orphaned commits could NOT be listed"
    if cmp.get("status") in ("identical", "behind"):
        return [], None
    return cmp.get("commits", []), None


def main() -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    branch = os.environ["BRANCH"]
    sha = os.environ["PUSHED_SHA"]
    base = os.environ.get("DEFAULT_BRANCH") or "master"

    try:
        prs = gh_json(
            "pr", "list", "-R", repo,
            "--head", branch,
            "--state", "all",
            "--json", "number,state,mergedAt,headRefOid,url",
            "--limit", "30",
        )
    except GhError as exc:
        return degraded(GUARD, f"could not list PRs for `{branch}` ({type(exc).__name__})")

    if not prs:
        summary(f"{GUARD}: `{branch}` has no PR yet — nothing to guard.")
        return 0
    if any(p.get("state") == "OPEN" for p in prs):
        summary(f"{GUARD}: `{branch}` has an open PR — healthy.")
        return 0

    # Every PR for this head is MERGED or CLOSED: classify before alarming.
    newest = max(prs, key=lambda p: p["number"])
    commits, err = orphan_commits(repo, newest, sha, base)

    if err is None and not commits:
        summary(
            f"{GUARD}: `{branch}` is dead (PR #{newest['number']} {newest['state']}) but this "
            f"push carries nothing beyond what the PR landed — PRUNABLE hygiene, not loss."
        )
        return 0

    if err is not None:
        commit_list = f"({err} — treat as INDETERMINATE: verify by content diff before re-landing, do not blind cherry-pick; `python3 scripts/dev/stranded_work_audit.py` gives the full classification)"
        pick = "<verify shas first>"
    else:
        commit_list = "\n".join(
            f"- `{c['sha'][:9]}` {c['commit']['message'].splitlines()[0]}" for c in commits
        )
        pick = " ".join(c["sha"] for c in commits)

    author = branch.split("/", 1)[0] if "/" in branch else "reland"
    title = f"Orphan push: commits landed on `{branch}` after its PR was {newest['state']}"
    body = f"""A push just landed on a branch whose every PR is already MERGED/CLOSED.
Nothing will merge this branch again: the commits below will strand and the
branch cleanup sweep will eventually delete them.

- branch: `{branch}` · pushed tip: `{sha}`
- newest PR: #{newest['number']} ({newest['state']}{', merged at ' + newest['mergedAt'] if newest.get('mergedAt') else ''}) — {newest.get('url', '')}

Commits beyond what the PR landed:

{commit_list}

**Re-land recipe** (do not keep pushing to the dead branch):

```
git fetch origin
git switch -c {author}/reland-{branch.rsplit('/', 1)[-1]} origin/{base}
git cherry-pick {pick}
```

then open a fresh PR. Close this issue once the work is re-landed or
explicitly discarded.

**Reusing this branch name on purpose for a new round of work?** Open the
new PR now — pushes with an open PR pass this guard — and close this issue
as a false positive.

_Filed by orphan-push-guard.yml — the real-time counterpart of the weekly
stranded-work audit._"""

    try:
        file_or_comment_finding(repo, fingerprint_marker(f"{GUARD} {branch}"), title, body)
        filed = "a ci-finding issue was filed/updated"
    except GhError as exc:
        filed = f"ISSUE FILING FAILED ({type(exc).__name__}) — the red X on this run is the only surface"

    print(f"::error::{GUARD}: push to `{branch}` landed after its PR was {newest['state']} — this work will strand; {filed}")
    summary(
        f"**{GUARD}: ORPHAN PUSH.** `{branch}` received `{sha[:9]}` after PR "
        f"#{newest['number']} was {newest['state']}. {filed}. Re-land onto a fresh "
        f"branch off {base}; do not keep pushing here."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
