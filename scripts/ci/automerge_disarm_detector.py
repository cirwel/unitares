#!/usr/bin/env python3
"""Auto-merge disarm detector — surface PRs GitHub silently disarmed.

The strand mode (observed on PR #1476, 2026-08-02): GitHub auto-disables
auto-merge when a required check reports failure even transiently (timeline
event `auto_merge_disabled`). The PR then silently drops out of every
automated path — nothing updates it, nothing merges it, and its page looks
like an ordinary open PR — until a human notices, which has taken days.

This detector scans open, non-draft PRs whose auto-merge is currently OFF
and checks whether their most recent auto-merge timeline event is a
disarm. Hits are reported through ONE tracking issue updated in place
(never one issue per PR — this is a dashboard, not an alarm bell), plus
the job summary. Re-arming stays a human/session decision by design; the
detector only ends the silence.

PRs that were never armed produce no auto-merge events and are correctly
ignored — under this repo's draft-PR delivery contract that is the normal
state, not a finding.

Exit code is always 0 on completion (a red scheduled run pages nobody;
the issue is the surface). API failures degrade visibly per
merge_loss_common; per-PR timeline failures are listed as UNVERIFIABLE
in the report rather than dropped.

Env (set by .github/workflows/automerge-disarm.yml):
  GITHUB_REPOSITORY  owner/name
"""

from __future__ import annotations

import os
import subprocess
import sys

from merge_loss_common import (
    degraded,
    ensure_finding_label,
    find_open_finding,
    fingerprint_marker,
    gh,
    gh_json,
    summary,
)

GUARD = "automerge-disarm-detector"
MARKER = fingerprint_marker(GUARD)
TIMELINE_PAGES = 5  # 100 events/page; long PRs beyond this read as unverifiable

GhError = (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError)


def last_automerge_event(repo: str, pr_number: int) -> dict | None:
    """The newest auto_merge_enabled/disabled timeline event, or None."""
    found: dict | None = None
    for page in range(1, TIMELINE_PAGES + 1):
        events = gh_json(
            "api", f"repos/{repo}/issues/{pr_number}/timeline?per_page=100&page={page}"
        )
        for event in events:
            if event.get("event") in ("auto_merge_enabled", "auto_merge_disabled"):
                found = event  # timeline is oldest-first; keep the latest seen
        if len(events) < 100:
            break
    return found


def main() -> int:
    repo = os.environ["GITHUB_REPOSITORY"]

    try:
        prs = gh_json(
            "pr", "list", "-R", repo,
            "--state", "open",
            "--json", "number,title,isDraft,autoMergeRequest,url",
            "--limit", "100",
        )
    except GhError as exc:
        return degraded(GUARD, f"could not list open PRs ({type(exc).__name__})")

    disarmed: list[str] = []
    unverifiable: list[str] = []
    for pr in prs:
        if pr.get("isDraft") or pr.get("autoMergeRequest") is not None:
            continue
        try:
            event = last_automerge_event(repo, pr["number"])
        except GhError:
            unverifiable.append(f"- #{pr['number']} {pr['title']} — timeline unreadable, disarm state UNKNOWN")
            continue
        if event is not None and event["event"] == "auto_merge_disabled":
            disarmed.append(
                f"- #{pr['number']} {pr['title']} — disarmed at {event.get('created_at', 'unknown time')} ({pr['url']})"
            )

    if not disarmed and not unverifiable:
        summary(f"{GUARD}: no silently-disarmed PRs.")
        try:
            existing = find_open_finding(repo, MARKER)
            if existing is not None:
                gh(
                    "issue", "close", str(existing), "-R", repo,
                    "--reason", "completed",
                    "--comment", "The disarm detector found no silently-disarmed PRs. Closing this resolved finding.",
                )
        except GhError as exc:
            return degraded(GUARD, f"all clear, but closing the tracking issue failed ({type(exc).__name__})")
        return 0

    body_sections = []
    if disarmed:
        body_sections.append(
            "These open, non-draft PRs had auto-merge armed and GitHub disarmed it\n"
            "(usually a transiently-failing required check). Nothing automated will\n"
            "touch them again until someone re-arms (`gh pr merge --auto`) or merges\n"
            "manually — re-arming is deliberately a human/session decision.\n\n" + "\n".join(disarmed)
        )
    if unverifiable:
        body_sections.append("Timeline unreadable — disarm state unknown, do not read as healthy:\n\n" + "\n".join(unverifiable))
    body = (
        "\n\n".join(body_sections)
        + "\n\n_Updated in place by automerge-disarm.yml on a schedule; this issue closes itself when the list empties._"
    )
    title = "Auto-merge silently disarmed on open PRs"

    try:
        ensure_finding_label(repo)
        existing = find_open_finding(repo, MARKER)
        if existing is not None:
            gh("issue", "edit", str(existing), "-R", repo, "--body", f"{MARKER}\n{body}")
        else:
            gh(
                "issue", "create", "-R", repo,
                "--title", title,
                "--label", "ci-finding",
                "--body", f"{MARKER}\n{body}",
            )
    except GhError as exc:
        return degraded(GUARD, f"found {len(disarmed)} disarmed / {len(unverifiable)} unverifiable PRs but could not write the tracking issue ({type(exc).__name__})")

    summary(
        f"**{GUARD}:** {len(disarmed)} disarmed, {len(unverifiable)} unverifiable — tracking issue updated."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
