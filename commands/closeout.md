---
description: "Close out a UNITARES workspace before final response"
---

Use this before saying work is done, especially after edits, test runs, local
servers, BEAM residents, or any cleanup request from the operator.

First run a non-mutating check:

- `python3 scripts/dev/workspace_closeout.py`
- `python3 scripts/dev/workspace_closeout.py --branch-hygiene` when branch or
  worktree cleanup is part of the question

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
- the `branch hygiene:` line when requested, including cleanup candidates,
  safe deletions performed, held branches, and sweep errors

If work should be delivered instead of left local, stage the intentional files
and use the ship helper:

- When the operator asks to ship, finish, deliver, open a PR, or otherwise
  complete a delivery workflow, treat the default draft-PR route as authorized;
  do not ask for a second confirmation just to push or open the draft PR.
- `./scripts/dev/ship.sh --plan "commit message"` previews the delivery route
- `./scripts/dev/ship.sh "commit message"` is the default route: a **draft PR for
  every change**, per `docs/operations/github-workflow-conventions.md` (the
  operator is the merge gate). Runtime/detached work mints a fresh agent-prefixed
  branch; non-runtime work on a named branch opens the draft PR on that branch
- `./scripts/dev/ship.sh --direct "commit message"` opts out for docs/tests-only
  pushes where you knowingly skip the PR
- `./scripts/dev/ship.sh --auto-merge "commit message"` opts into
  auto-merge-on-green; use only when the operator explicitly asks, not by default

If the operator asked to clean the workspace, or if you are finishing a task
whose intended work is already committed/stashed, run:

- `python3 scripts/dev/workspace_closeout.py --stash-dirty --stop-repo-processes --bootout-launch-agents --branch-hygiene-live`

`--branch-hygiene-live` reuses the Vigil branch-hygiene safety contract in
`docs/operations/branch-hygiene-runbook.md`: patch-equivalent or empty stale
branches may be pruned, clean stale worktrees may be removed, and branches with
unique commits, dirty worktrees, or a protected checkout are held for review
instead of deleted.

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
- Treat branch hygiene holds as cleanup findings, not green lights. Report the
  branch names and either salvage them or leave them for a follow-up cleanup
  agent.
- Include the stash name, commit hash, and stopped LaunchAgent labels in the
  final response.
