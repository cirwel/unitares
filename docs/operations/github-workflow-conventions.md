# GitHub Workflow Conventions

One delivery contract for **every** agent that pushes to this repo — Codex,
Claude (CLI), and Claude (web/cloud harness) — so that concurrent sessions
don't collide, and so the operator can predict whether any given session's
work *lands* or *waits*.

This is the canonical reference. `AGENTS.md` and `CLAUDE.md` carry a short
summary in their shared-contract block and point here for the detail.

## Why this exists

Before this convention, delivery behavior diverged by *entrypoint*, not by
intent:

- **`ship.sh` default (`auto`)** routed runtime code to a draft PR but
  direct-pushed docs/tests/"other" straight to the current branch — landing
  immediately, no PR (`scripts/dev/ship.sh` `auto` branch, ~lines 195-204).
- **`ship.sh --auto-merge`** opened a PR and enabled GitHub
  auto-merge-on-green ("the old behavior").
- **Claude on the web/cloud harness** was handed a fixed
  `claude/<topic>-<id>` branch and always parked a *draft PR*, bypassing
  `ship.sh` routing entirely.

So Codex tended to direct-push docs and could opt into auto-merge, while
Claude-on-web parked draft PRs that sat until a human merged them. With many
sessions running at once that is unpredictable: three branch-naming schemes,
and merge behavior that depended on which tool and which agent shipped the
change. This document collapses that to one rule set.

## The convention

### 1. Branch naming — one pattern, agent-prefixed

```
<agent>/<topic>-<short-id>
```

- `<agent>` is `claude` or `codex` — kept as a prefix so a branch is
  self-identifying at a glance, which matters when several sessions run in
  parallel.
- `<topic>` is a short kebab-case slug of the change.
- `<short-id>` is a timestamp or short hash that makes the branch unique.

Both existing generators already satisfy this shape:

- `ship.sh` mints `<agent>/auto/<timestamp>-<slug>` (the agent prefix is
  detected from `CLAUDECODE`, or set via `UNITARES_SHIP_AGENT`).
- The web/cloud harness hands Claude a `claude/<topic>-<id>` branch.

Never push to `main` or `master`. If you find yourself on the default branch,
create a feature branch first.

### 2. Delivery — draft PR for everything

Every session lands its work as a **draft PR**, regardless of agent and
regardless of whether the change is runtime code or docs/tests. The operator
is the merge gate.

- **Do not** direct-push to a shared branch.
- **Do not** enable auto-merge by default.
- A draft PR means "visible, not claiming merged." Marking ready and merging
  is a deliberate human (or explicitly-instructed) action, taken only after CI
  is green and you've confirmed no collision with an in-flight branch.

With `ship.sh`, use the explicit draft-PR route — not the bare `auto` route,
whose docs/tests path would direct-push:

```bash
./scripts/dev/ship.sh --draft-pr "type(scope): concise message"
```

`./scripts/dev/ship.sh --plan "..."` previews the route without shipping.
`--auto-merge` remains available for the rare case where the operator
explicitly wants auto-merge-on-green; it is not the default.

### 3. Parallel / simultaneous work

This convention exists because a lot of work happens concurrently. Two
guards keep concurrent sessions from clobbering each other:

- **Single-writer surfaces** (migrations, identity/onboarding, `plan.md`, hot
  RFC docs, large test consolidations): before touching one, check for an
  in-flight PR and branch from its head instead of starting a parallel
  attempt. The authoritative list lives under *"Before Starting Work on a
  Single-Writer Surface"* in the `AGENTS.md` / `CLAUDE.md` shared contract.
- **Branch hygiene**: stale and superseded branches are swept per
  `docs/operations/branch-hygiene-runbook.md`. Branches with unique local work
  (`git cherry master <branch>` showing `+`) are held for review, never auto-
  deleted — so parking a draft PR is always safe.

## Quick reference

| Situation | Do this |
| --- | --- |
| Ship any change (Codex or Claude CLI) | `./scripts/dev/ship.sh --draft-pr "msg"` |
| Preview the route first | `./scripts/dev/ship.sh --plan "msg"` |
| Claude on the web harness | Already parks a draft PR on its `claude/...` branch — nothing extra needed |
| About to touch a single-writer surface | Check for an in-flight PR first; branch from its head if one exists |
| Operator explicitly wants auto-merge | `./scripts/dev/ship.sh --auto-merge "msg"` (not the default) |
| Tempted to direct-push docs | Don't — draft PR for everything |

## Per-entrypoint mapping

- **Codex (CLI):** stage, then `ship.sh --draft-pr`. Report the delivery line
  at closeout (`/closeout`). Do not rely on bare `ship.sh` for non-runtime
  changes — its `auto` route direct-pushes them.
- **Claude (CLI, plugin harness):** same as Codex — `ship.sh --draft-pr`.
- **Claude (web/cloud harness):** the harness already enforces the convention
  (fixed `claude/...` branch + draft PR). Keep work on that branch; let the
  draft PR be the delivery artifact.
