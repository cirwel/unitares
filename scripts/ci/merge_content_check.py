#!/usr/bin/env python3
"""Post-merge content check — did the branch's final push actually merge?

The loss mode (PR #1610, 2026-08-12, cause never conclusively diagnosed):
six commits were pushed, the PR merged green, and master contained five —
the final pushed commit was silently absent. The PR page, CI, and the
push all read as success; the loss surfaced weeks later via a live-server
probe.

This guard runs when a PR merges. It recovers the branch's latest actual
pushed tip from the repository event stream (PushEvents) and asks the
compare API whether the head GitHub merged contains that push. A
contradiction — the last push carries commits the merged head does not —
fails the run and files a ci-finding issue naming them. It covers the
stale-head variant of the #1610 mode; since the incident's cause was
never diagnosed, no claim is made to cover every variant.

A push that POSTDATES the merge is a different loss mode — the orphan
push — and is routed to the orphan-push-guard finding (same fingerprint,
so the two guards dedup into one issue). Because this workflow runs from
the BASE side of the PR, that routing also covers branches too old to
carry orphan-push-guard.yml themselves.

Evidence honesty: the events feed is ordered by event id (ingestion),
which live data shows is NOT always strict created_at order — so the
newest push is chosen by created_at across all matches, not by array
position. The feed also lags (GitHub documents 30s–6h), and retains only
~300 events (≈1.6 days at 2026-08 traffic). A clean pass therefore
reports "no contradiction found", never "verified" — a lagging feed can
hide the final push, though it cannot fabricate a false alarm (stale
entries are older pushes, which are contained in the head). The weekly
stranded-work audit remains the backstop. Unverifiable cases report as
DEGRADED — never as silence.

Env (set by .github/workflows/merge-content-check.yml):
  GITHUB_REPOSITORY  owner/name
  PR_NUMBER          the merged PR
  HEAD_BRANCH        the PR's head branch name
  HEAD_SHA           the head SHA GitHub merged (event payload)
  MERGED_AT          the merge timestamp (event payload)
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime

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


def _parse_ts(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def latest_pushed_tip(repo: str, branch: str) -> tuple[str, str] | None:
    """(head_sha, created_at) of the branch's newest push in the event stream.

    Scans every retained page and picks the maximum by created_at, because
    the array's id ordering does not reliably track wall-clock order
    (verified live 2026-08-28 on this repo's own master PushEvents).
    Raises on API failure; returns None when the branch has no PushEvent
    inside the retention window.
    """
    ref = f"refs/heads/{branch}"
    matches: list[tuple[str, str]] = []
    for page in range(1, EVENT_PAGES + 1):
        events = gh_json("api", f"repos/{repo}/events?per_page=100&page={page}")
        for event in events:
            if event.get("type") == "PushEvent" and event.get("payload", {}).get("ref") == ref:
                matches.append((event.get("created_at", ""), event["payload"]["head"]))
        if len(events) < 100:
            break
    if not matches:
        return None
    created_at, head = max(matches)
    return head, created_at


def route_post_merge_push(repo: str, pr_number: str, branch: str, last_push: str, pushed_at: str) -> int:
    """A push AFTER the merge is the orphan mode, not a merge-content loss."""
    title = f"Orphan push: commits landed on `{branch}` after PR #{pr_number} merged"
    body = f"""PR #{pr_number} merged, and `{branch}` then received a LATER push
(`{last_push}` at {pushed_at}). Nothing will merge that branch again: the
post-merge commits will strand and the branch cleanup sweep will
eventually delete them.

Re-land onto a fresh branch off the default branch (cherry-pick the
post-merge commits) and open a fresh PR; do not keep pushing to the dead
branch. `python3 scripts/dev/stranded_work_audit.py` classifies the exact
orphan set.

_Filed by merge-content-check.yml, routed to the orphan-push finding —
this also covers branches too old to carry orphan-push-guard.yml._"""
    try:
        file_or_comment_finding(repo, fingerprint_marker(f"orphan-push-guard {branch}"), title, body)
        filed = "filed/updated the orphan-push finding"
    except GhError as exc:
        filed = f"ISSUE FILING FAILED ({type(exc).__name__}) — the red X on this run is the only surface"
    print(f"::error::{GUARD}: `{branch}` was pushed AFTER PR #{pr_number} merged — orphan-push mode; {filed}")
    summary(
        f"**{GUARD}: POST-MERGE PUSH.** `{branch}` received `{last_push[:9]}` after PR "
        f"#{pr_number} merged — that work will strand ({filed})."
    )
    return 1


def main() -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    pr_number = os.environ["PR_NUMBER"]
    branch = os.environ["HEAD_BRANCH"]
    head_sha = os.environ["HEAD_SHA"]
    merged_at = os.environ.get("MERGED_AT", "")

    try:
        found = latest_pushed_tip(repo, branch)
    except GhError as exc:
        return degraded(GUARD, f"could not read the repo event stream ({type(exc).__name__}); merge of PR #{pr_number} is content-unverified")

    if found is None:
        return degraded(GUARD, f"no PushEvent for `{branch}` inside the events window; merge of PR #{pr_number} is content-unverified")
    last_push, pushed_at = found

    merged_ts = _parse_ts(merged_at)
    pushed_ts = _parse_ts(pushed_at)
    if merged_ts is not None and pushed_ts is not None and pushed_ts > merged_ts:
        return route_post_merge_push(repo, pr_number, branch, last_push, pushed_at)

    if last_push == head_sha:
        summary(
            f"{GUARD}: PR #{pr_number} merged head `{head_sha[:9]}` matches the branch's newest "
            f"recorded push — no contradiction found (the events feed can lag; this is evidence, not proof)."
        )
        return 0

    try:
        cmp = gh_json("api", f"repos/{repo}/compare/{head_sha}...{last_push}")
    except GhError as exc:
        return degraded(GUARD, f"compare `{head_sha[:9]}...{last_push[:9]}` failed ({type(exc).__name__}); merge of PR #{pr_number} is content-unverified")

    status = cmp.get("status")
    if status in ("identical", "behind"):
        # The merged head already contains the last recorded push (e.g.
        # GitHub's own update-branch merge advanced the head past it).
        summary(
            f"{GUARD}: PR #{pr_number} merged head contains the branch's newest recorded push "
            f"(`{last_push[:9]}`, {status}) — no contradiction found (events feed can lag; evidence, not proof)."
        )
        return 0

    # ahead / diverged: the last push carries commits the merge does not.
    commits = cmp.get("commits", [])
    commit_lines = "\n".join(
        f"- `{c['sha'][:9]}` {c['commit']['message'].splitlines()[0]}" for c in commits
    ) or "(compare returned no commit detail — inspect the compare URL below)"

    title = f"PR #{pr_number} merged WITHOUT the final push to `{branch}`"
    body = f"""PR #{pr_number} merged with head `{head_sha}`, but the branch's latest
recorded push was `{last_push}` ({pushed_at}) — and that push carries
commits the merged head does not (compare status: `{status}`). This
matches the stale-head variant of the PR #1610 loss mode: everything
reads green while a pushed commit never reaches master.

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
        f"branch's last recorded push was `{last_push[:9]}` ({status}). {filed}."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
