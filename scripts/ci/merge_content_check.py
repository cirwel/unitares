#!/usr/bin/env python3
"""Post-merge content check — did the branch's final push actually merge?

The loss mode (PR #1610, 2026-08-12, cause never diagnosed): six commits
were pushed, the PR merged green, and master contained five of them — the
final pushed commit was silently absent. The PR page, CI, and the push all
read as success; the loss surfaced weeks later via a live-server probe.

This guard runs when a PR merges. It recovers the branch's latest actual
pushed tip from the repository's public event stream (PushEvents), then
asks the compare API whether the merged head contains that push. If the
last push carries commits the merge lacks, it fails the run and files a
ci-finding issue naming them.

Two honest limitations, both reported as DEGRADED rather than silence:
the events window is finite (~300 events), so a PR merged long after its
last push may be unverifiable; and compare on a deleted branch relies on
GitHub retaining the dangling commits (it does, short-term — this guard
runs within minutes of the merge).

Env (set by .github/workflows/merge-content-check.yml):
  GITHUB_REPOSITORY  owner/name
  PR_NUMBER          the merged PR
  HEAD_BRANCH        the PR's head branch name
  HEAD_SHA           the head SHA GitHub merged (event payload)
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

GUARD = "merge-content-check"
EVENT_PAGES = 3  # 100 events/page; the API retains ~300 either way

GhError = (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError)


def latest_pushed_tip(repo: str, branch: str) -> str | None:
    """Newest PushEvent head for the branch, from the repo event stream.

    Pages are newest-first, so the first match is the latest push.
    Raises on API failure; returns None when the branch has no PushEvent
    inside the retention window.
    """
    ref = f"refs/heads/{branch}"
    for page in range(1, EVENT_PAGES + 1):
        events = gh_json("api", f"repos/{repo}/events?per_page=100&page={page}")
        for event in events:
            if event.get("type") == "PushEvent" and event.get("payload", {}).get("ref") == ref:
                return event["payload"]["head"]
        if len(events) < 100:
            break
    return None


def main() -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    pr_number = os.environ["PR_NUMBER"]
    branch = os.environ["HEAD_BRANCH"]
    head_sha = os.environ["HEAD_SHA"]

    try:
        last_push = latest_pushed_tip(repo, branch)
    except GhError as exc:
        return degraded(GUARD, f"could not read the repo event stream ({type(exc).__name__}); merge of PR #{pr_number} is content-unverified")

    if last_push is None:
        return degraded(GUARD, f"no PushEvent for `{branch}` inside the events window; merge of PR #{pr_number} is content-unverified")

    if last_push == head_sha:
        summary(f"{GUARD}: PR #{pr_number} merged head `{head_sha[:9]}` matches the branch's last push — verified.")
        return 0

    try:
        cmp = gh_json("api", f"repos/{repo}/compare/{head_sha}...{last_push}")
    except GhError as exc:
        return degraded(GUARD, f"compare `{head_sha[:9]}...{last_push[:9]}` failed ({type(exc).__name__}); merge of PR #{pr_number} is content-unverified")

    status = cmp.get("status")
    if status in ("identical", "behind"):
        # The merged head already contains the last push (e.g. GitHub's own
        # update-branch merge advanced the head past it). Nothing lost.
        summary(f"{GUARD}: PR #{pr_number} merged head contains the branch's last push (`{last_push[:9]}`, {status}) — verified.")
        return 0

    # ahead / diverged: the last push carries commits the merge does not.
    commits = cmp.get("commits", [])
    commit_lines = "\n".join(
        f"- `{c['sha'][:9]}` {c['commit']['message'].splitlines()[0]}" for c in commits
    ) or "(compare returned no commit detail — inspect the compare URL below)"

    title = f"PR #{pr_number} merged WITHOUT the final push to `{branch}`"
    body = f"""PR #{pr_number} merged with head `{head_sha}`, but the branch's latest
actual push was `{last_push}` — and that push carries commits the merged
head does not (compare status: `{status}`). This is the PR-#1610 loss mode:
everything reads green while a pushed commit never reaches master.

Commits in the last push but not in the merge:

{commit_lines}

Compare: {cmp.get('html_url', f'https://github.com/{repo}/compare/{head_sha}...{last_push}')}

**Re-land recipe:**

```
git fetch origin
git switch -c reland-pr-{pr_number} origin/master
git cherry-pick {' '.join(c['sha'] for c in commits) if commits else '<missing shas>'}
```

then open a fresh PR. Close this issue once re-landed or explicitly discarded.

_Filed by merge-content-check.yml. The head branch may already be deleted;
the commits remain fetchable by SHA short-term, and survive in the pushing
session's local clone/reflog._"""

    try:
        file_or_comment_finding(repo, fingerprint_marker(f"{GUARD} pr-{pr_number}"), title, body)
        filed = "a ci-finding issue was filed/updated"
    except GhError as exc:
        filed = f"ISSUE FILING FAILED ({type(exc).__name__}) — the red X on this run is the only surface"

    print(f"::error::{GUARD}: PR #{pr_number} merged without the final push to `{branch}` ({last_push[:9]} vs merged {head_sha[:9]}); {filed}")
    summary(
        f"**{GUARD}: CONTENT MISSING.** PR #{pr_number} merged `{head_sha[:9]}` but the "
        f"branch's last push was `{last_push[:9]}` ({status}). {filed}."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
