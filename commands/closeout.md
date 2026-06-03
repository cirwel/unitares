---
description: "Close out a UNITARES workspace before final response"
---

Use this before saying work is done, especially after edits, test runs, local
servers, BEAM residents, or any cleanup request from the operator.

First run a non-mutating check:

- `python3 scripts/dev/workspace_closeout.py`

This uses `.unitares/workspace-closeout-baseline.json` when present. The
baseline is written by `/governance-start`; it lets the closeout check ignore
resident/control-plane processes that already existed before the agent began
work and focus on newly started repo-rooted processes.

Report:

- whether git is clean
- any staged, unstaged, or untracked files
- any repo-rooted processes still running
- whether a remaining process is managed by a LaunchAgent label

If the operator asked to clean the workspace, or if you are finishing a task
whose intended work is already committed/stashed, run:

- `python3 scripts/dev/workspace_closeout.py --stash-dirty --stop-repo-processes --bootout-launch-agents`

Rules:

- Do not stash or terminate processes merely because the script found issues
  unless the operator asked for cleanup or the intended work is already safely
  committed/stashed.
- If there are staged changes, decide whether to commit, unstage, or stash
  before final response; do not leave them ambiguous.
- If there are unrelated dirty files, preserve them in a labeled stash rather
  than reverting them.
- Stop only processes rooted inside the current workspace. Do not stop services
  rooted in sibling deploy repos unless the operator explicitly asks.
- Include the stash name, commit hash, and stopped LaunchAgent labels in the
  final response.
