---
description: "Run policy-bounded autopilot diagnostics before handoff or delivery"
---

Use this when the operator wants fewer rubber-stamp prompts and more explicit
policy outcomes. The command bundles the boring loop while preserving the
existing safety contract: diagnostics are automatic, delivery is explicit, and
merge/destructive cleanup remains human-gated.

Default read-only run:

- `python3 scripts/dev/autopilot_closeout.py`

Common options:

- `--test-policy auto|skip|always|staged|fresh` controls `test-cache.sh`
  execution. `auto` skips docs-only changes and runs the cache for code-like
  changes.
- `--watcher-mode print|surface|skip` prints unresolved Watcher output by
  default; `surface` mutates Watcher state from pending to surfaced.
- `--branch-hygiene` includes the dry-run branch/worktree hygiene sweep.
- `--ship-plan "type(scope): message"` previews the `ship.sh` route without
  mutating git state.
- `--ship "type(scope): message"` explicitly runs `ship.sh`; add `--stage-all`
  only when the whole worktree belongs to the current task.
- `--json` emits machine-readable output for wrappers.

Policy reading:

- `policy: proceed` means diagnostics completed and no attention item remains.
- `policy: needs_human` means automation did the bounded work, but judgment or
  explicit delivery remains. Typical causes are local-only changes, unresolved
  Watcher lines, repo-rooted processes, or branch hygiene holds.
- `policy: blocked` means a command failed or closeout produced an error.

Rules:

- Do not use `--ship` as a substitute for review. It creates the normal draft PR
  delivery artifact; merge/mark-ready remains deliberate.
- Do not pass `--stage-all` if unrelated dirty files exist.
- Do not stop processes, stash, boot out LaunchAgents, merge, force-push, or run
  destructive DB actions from this command.
- Report the final `delivery:` line from the embedded workspace closeout.
