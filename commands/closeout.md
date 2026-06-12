---
description: "Close out a UNITARES workspace before final response"
---

Use this before saying work is done, especially after edits, test runs, local
servers, BEAM residents, or any cleanup request from the operator.

First run a non-mutating check:

- `python3 scripts/dev/workspace_closeout.py`

This uses `.unitares/workspace-closeout-baseline.json` when present. The
baseline is written by `/governance-start` through `workspace_closeout.py
--start-check`; it lets the closeout check ignore resident/control-plane
processes that already existed before the agent began work and focus on newly
started repo-rooted processes.

Report:

- whether git is clean
- the `delivery:` line, including whether work is local-only, unpushed,
  pushed-but-not-proven-merged, or synced with default upstream
- any staged, unstaged, or untracked files
- any repo-rooted processes still running
- whether a remaining process is managed by a LaunchAgent label

If work should be delivered instead of left local, stage the intentional files
and use the ship helper:

- `./scripts/dev/ship.sh --plan "commit message"` previews the delivery route
- `./scripts/dev/ship.sh --draft-pr "commit message"` commits, pushes, and opens
  a draft PR
- `./scripts/dev/ship.sh "commit message"` uses the default route: runtime or
  detached work becomes a draft PR; ordinary non-runtime branch work direct-pushes
- `./scripts/dev/ship.sh --auto-merge "commit message"` opts into the old
  auto-merge-on-green behavior for PR-routed work

If the operator asked to clean the workspace, or if you are finishing a task
whose intended work is already committed/stashed, run:

- `python3 scripts/dev/workspace_closeout.py --stash-dirty --stop-repo-processes --bootout-launch-agents`

Rules:

- Do not stash or terminate processes merely because the script found issues
  unless the operator asked for cleanup or the intended work is already safely
  committed/stashed.
- If there are staged changes, decide whether to commit, unstage, or stash
  before final response; do not leave them ambiguous.
- If `delivery` is `local_changes`, say plainly: not committed, not pushed,
  not merged. If the changes are intentional, name the ship command that would
  move them to a branch or draft PR.
- If `delivery` is `unpushed_commits`, say plainly: committed locally but not
  pushed or merged.
- If the checkout is detached, do not direct-push. Use `ship.sh --draft-pr` or
  create a named branch first.
- If `delivery` is `pushed_branch`, do not claim merge completion unless you
  also checked GitHub PR state explicitly.
- If the operator asks "merged?", answer directly from delivery state and any
  GitHub check performed.
- If there are unrelated dirty files, preserve them in a labeled stash rather
  than reverting them.
- Stop only processes rooted inside the current workspace. Do not stop services
  rooted in sibling deploy repos unless the operator explicitly asks.
- Include the stash name, commit hash, and stopped LaunchAgent labels in the
  final response.
