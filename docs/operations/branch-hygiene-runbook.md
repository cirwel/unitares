# Branch Hygiene Runbook

`agents/vigil_hygiene/agent.py` is the resident branch-hygiene sweep. It is
designed to remove branch clutter without deleting salvageable work.

The same safety contract is available during manual workspace closeout:

- `python3 scripts/dev/workspace_closeout.py --branch-hygiene` reports stale
  branch/worktree cleanup candidates without mutating them.
- `python3 scripts/dev/workspace_closeout.py --branch-hygiene-live` performs the
  safe sweep and surfaces held branches as closeout findings.

## Safety Contract

For local branches whose upstream is gone:

- `git cherry master <branch>` with one or more `+` commits means **HOLD**.
  The branch contains unique local work. Inspect, salvage into a fresh PR, or
  explicitly delete after review.
- all `-` commits means the work is patch-equivalent to `master`, usually from
  a squash merge. The branch is safe to prune if the worktree is clean.
- empty output means the branch has no commits ahead of `master`. The branch is
  safe to prune if the worktree is clean.
- unparseable or failed `git cherry` output means **SKIP**. Do not delete.
- dirty worktrees, paused rebases, paused cherry-picks, merges, or bisects are
  never removed by the sweep.
- a stale branch checked out in the sweep process's current repo is held; run
  hygiene from another checkout to remove that worktree.

For remote `origin/*` branches:

- branches with open PRs are keepalive.
- branches newer than 24 hours are keepalive.
- branches with all `-` commits are squash-merged and may be deleted.
- branches with any `+` commits are **HOLD** for human review.

## Manual Review Loop

When a branch is held:

1. Inspect `git cherry master <branch>` and `git show --stat <sha>`.
2. Check whether the touched surface is single-writer locked in `AGENTS.md`.
3. If useful work remains, create a fresh `codex/*` branch from `master` and
   cherry-pick or manually port only the live hunks.
4. Run focused tests, then `./scripts/dev/test-cache.sh --staged`.
5. Open a PR and let CI finish before merging.
6. Delete the stale branch only after the salvage PR is merged or after review
   proves the branch is fully superseded.

This is the expected self-improvement loop: classify, preserve evidence, carry
forward bounded useful work, verify, merge, and remove stale references.
