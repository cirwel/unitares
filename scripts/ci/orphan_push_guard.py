#!/usr/bin/env python3
"""Orphan-push tripwire — fail loudly when a push lands on a dead branch.

The loss mode (three confirmed incidents, 2026-08-12/19): a session keeps
committing to a branch after its PR squash-merged. Everything downstream
reads as success — `git push` succeeds (git knows nothing of PRs), the PR
page says MERGED, `merge-base --is-ancestor` is structurally wrong under
squash — and the commits strand until a prune sweep deletes them.

The weekly stranded_work_audit.py catches this class at up to 7 days'
latency, after the session that could trivially re-land is gone. This
guard is its real-time counterpart: it runs on every push to an agent
branch, and if every PR for that branch is already MERGED/CLOSED it fails
the run and files/updates a ci-finding issue with the exact re-land
recipe, while the pushing session is still alive to follow it.

Fail-open on API errors, but degraded is never silent — see
merge_loss_common.py. When the guard DOES fire, issue-filing failures do
not soften it: the run still exits 1 so the red X survives.

Env (set by .github/workflows/orphan-push-guard.yml):
  GITHUB_REPOSITORY  owner/name
  BRANCH             the pushed branch (github.ref_name)
  PUSHED_SHA         the pushed tip (github.sha)
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


def orphan_commit_lines(repo: str, sha: str) -> tuple[str, list[str]]:
    """Describe the commits on `sha` that master lacks.

    Returns (markdown bullet list, [shas oldest->newest]). Degrades to an
    explicit unavailability note — never to an empty list that could read
    as "nothing was lost".
    """
    try:
        cmp = gh_json("api", f"repos/{repo}/compare/master...{sha}")
        commits = cmp.get("commits", [])
        if not commits:
            return ("(compare reports no commits ahead of master — content may already be re-landed; verify by diff)", [])
        lines = [
            f"- `{c['sha'][:9]}` {c['commit']['message'].splitlines()[0]}"
            for c in commits
        ]
        return ("\n".join(lines), [c["sha"] for c in commits])
    except GhError:
        return ("(orphaned commit list unavailable — the compare API call failed; run `python3 scripts/dev/stranded_work_audit.py` locally for the full picture)", [])


def main() -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    branch = os.environ["BRANCH"]
    sha = os.environ["PUSHED_SHA"]

    try:
        prs = gh_json(
            "pr", "list", "-R", repo,
            "--head", branch,
            "--state", "all",
            "--json", "number,state,mergedAt,url",
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

    # Every PR for this head is MERGED or CLOSED: this push is orphaned work.
    newest = max(prs, key=lambda p: p["number"])
    author = branch.split("/", 1)[0] if "/" in branch else "reland"
    commit_list, shas = orphan_commit_lines(repo, sha)
    pick = " ".join(shas) if shas else "<orphaned shas>"

    title = f"Orphan push: commits landed on `{branch}` after its PR was {newest['state']}"
    body = f"""A push just landed on a branch whose every PR is already MERGED/CLOSED.
Nothing will merge this branch again: the commits below will strand and the
branch cleanup sweep will eventually delete them.

- branch: `{branch}` · pushed tip: `{sha}`
- newest PR: #{newest['number']} ({newest['state']}{', merged at ' + newest['mergedAt'] if newest.get('mergedAt') else ''}) — {newest.get('url', '')}

Commits not in master:

{commit_list}

**Re-land recipe** (do not keep pushing to the dead branch):

```
git fetch origin
git switch -c {author}/reland-{branch.rsplit('/', 1)[-1]} origin/master
git cherry-pick {pick}
```

then open a fresh PR. Close this issue once the work is re-landed or
explicitly discarded.

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
        f"branch off master; do not keep pushing here."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
