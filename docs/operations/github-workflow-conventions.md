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
  immediately, no PR.
- **`ship.sh --auto-merge`** opened a PR and enabled GitHub
  auto-merge-on-green ("the old behavior").
- **Claude on the web/cloud harness** was handed a fixed
  `claude/<topic>-<id>` branch and always parked a *draft PR*, bypassing
  `ship.sh` routing entirely.

So Codex tended to direct-push docs and could opt into auto-merge, while
Claude-on-web parked draft PRs that sat until a human merged them. With many
sessions running at once that is unpredictable: three branch-naming schemes,
and merge behavior that depended on which tool and which agent shipped the
change. This document collapses that to one rule set, and `ship.sh`'s default
`auto` route now opens a draft PR for every change (see *Delivery* below).

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

- If the operator asks an agent to ship, finish, deliver, open a PR, or
  otherwise complete a delivery workflow, the agent may assume branch -> commit
  -> push -> draft PR is authorized. Do not stop for a second confirmation just
  to push the branch or open the draft PR.
- **Do not** direct-push to a shared branch.
- **Do not** enable auto-merge by default.
- A draft PR means "visible, not claiming merged." **Merging** is the
  operator's deliberate action. **Marking ready** is the working agent's:
  the agent that owns the PR declares readiness itself, once its validation
  actually passed — CI green, review round joined, no collision with an
  in-flight branch.
- **Readiness is agent-declared, never operator-inferred.** The operator
  pressing merge in order cannot verify content and should not have to
  guess doneness: a PR still in draft is "still working — hands off," even
  when the diff looks finished, and nobody marks another agent's PR ready
  on its behalf. A draft whose owner went silent is a question for the
  owner (KG channel) or the stranded-work audit — not a green button.
  Ordering constraints the agent knows about ("merge after #N") belong in
  the PR body, so in-order merging acts on declared state. Rationale
  (2026-08-27): marking agent PRs ready on inference while the agent was
  still revising is the trigger shape of the post-merge orphan-push
  incidents — the human gate authorizes; verification lives with CI,
  reviews, and the merge-loss guards.

`ship.sh` enforces this. Its default `auto` route now opens a **draft PR for
every change** — runtime, docs, or tests:

```bash
./scripts/dev/ship.sh "type(scope): concise message"
```

- If all current worktree changes belong in the PR, use
  `./scripts/dev/ship.sh --stage-all "type(scope): concise message"` to stage,
  branch if needed, commit, push, and open the draft PR in one command.
- Runtime and detached-HEAD work mint a fresh agent-prefixed branch and open
  the draft PR there.
- Non-runtime work on a named feature branch opens the draft PR on that branch.
- `./scripts/dev/ship.sh --plan "..."` previews the route without shipping;
  `--stage-all --plan` previews the route for the full dirty worktree without
  mutating the index.
- `--direct` is the opt-out, for docs/tests-only pushes where you knowingly
  skip the PR.
- `--auto-merge` remains available for the rare case where the operator
  explicitly wants auto-merge-on-green; it is not the default.

### 3. Parallel / simultaneous work

This convention exists because a lot of work happens concurrently. Two
guards keep concurrent sessions from clobbering each other:

- **Single-writer surfaces** (migrations, identity/onboarding, `plan.md`, hot
  RFC docs, large test consolidations): before touching one, check for an
  in-flight PR and branch from its head instead of starting a parallel
  attempt. The authoritative list lives under *"Before Starting Work on a
  Single-Writer Surface"* in the `AGENTS.md` / `CLAUDE.md` shared contract.
- **Correcting an architecture fact in prose**: the same PR greps
  reader-facing docs (README, `docs/` outside `proposals/`, `skills/`,
  `AGENTS.md`/`CLAUDE.md`) for the old claim and updates every copy, and adds
  a row + deny-pattern to the Contested Claims Registry in
  `docs/dev/CANONICAL_SOURCES.md` so `check_doc_health.py` blocks the stale
  wording from reappearing. Corrections that land in one doc and drift in the
  others were the entire defect class of the 2026-07-02 coherence audit.
- **Branch hygiene**: stale and superseded branches are swept per
  `docs/operations/branch-hygiene-runbook.md`. Branches with unique local work
  (`git cherry master <branch>` showing `+`) are held for review, never auto-
  deleted — so parking a draft PR is always safe.

### 4. Landing work — do not babysit the queue

`master` requires branches to be up to date before merging (`strict`), and
`enforce_admins` is on, so nobody can bypass it. With seven required checks and
a ~15-minute slowest job, **every merge invalidates every other open PR**. At
nine open PRs that is nine update-branch clicks and over two hours of CI per
pass through the queue, and each merge re-dirties the rest. That is arithmetic,
not a discipline problem — no amount of care makes it cheaper.

**Use `gh pr merge --auto <n>` instead of watching.** The repo now has
"always suggest updating pull request branches" enabled, so with auto-merge set
GitHub updates the branch itself when the base moves and merges as soon as
checks pass. This does **not** weaken the human merge gate: `--auto` is a
deliberate per-PR act, and it says "this one is approved, land it when green" —
you are giving up the waiting, not the decision. Draft PRs cannot take
`--auto`, so mark ready first; that mark is the gate.

**Confirmed working end-to-end 2026-08-14.** Two armed PRs were fixed and left
alone: #1653 merged at 09:45; #1658 went `BEHIND` the moment it did, and GitHub
moved its head on its own about two minutes later, re-ran CI against the fresh
base, and merged it at 10:08. No script and no human touched the branch in
between. **So do not write or run an update-branch babysitter for this repo** —
polling and pushing only races GitHub's own updater and burns a CI cycle per
redundant update. `unitares-governance-plugin` is the exception that still needs
manual `gh pr update-branch`, because auto-merge is disallowed there.

A workflow that predates this, `.github/workflows/pr-queue-autoupdate.yml`, was
removed in the same pass. It was a poor-man's queue added before the repo
setting existed, it required a PAT (`PR_AUTOUPDATE_TOKEN`) that was never
created, and so every run since — on each push to master plus hourly — exited
early having done nothing. Restoring it would put a second updater in a race
with GitHub's native one.

**Do not stack more than two deep.** Each level must land in order, and every
merge below re-dirties everything above. A three-deep stack built 2026-08-13
produced a conflicted middle PR within hours, purely from its own base moving.
If two changes touch the same file, they are one PR — splitting them buys
reviewability and pays for it in cascade.

**Land or close drafts quickly.** An open draft accrues cascade debt: it needs
a rebuild every time anything merges, whether or not anyone is working on it.

### Merge queue — the real fix, and what blocks it today

A merge queue is the native answer: it tests each PR against the *projected*
result of the ones ahead and merges in order, so `strict` stays on and nobody
hand-updates anything.

**Do not enable it yet.** A merge queue only advances when the required checks
report on `merge_group` events. Five of the seven do — `tests.yml`
(smoke / test / dashboard), `repo-scope.yml` (scope), and
`documentation-validation.yml` (validate) all carry a `merge_group` trigger.
The remaining two, **`Analyze (actions)` and `Analyze (python)`, come from
CodeQL default setup**, which has no workflow file to add a trigger to and does
not run on `merge_group`. Enabling the queue against those two as required
checks means every entry waits forever for a check that will never arrive —
i.e. it blocks all merges rather than speeding them up.

Two ways to unblock, whichever is preferred:

1. Drop the two `Analyze` checks from *required* status checks. They still run
   on every PR; they just stop gating the merge.
2. Convert CodeQL from default setup to an advanced-setup workflow file and add
   `merge_group` to its triggers.

Verify with `gh api repos/cirwel/unitares/code-scanning/default-setup` before
assuming this note is still current.

### Merge-loss guards — the detective layer

Until (and after) a queue exists, three repo-side workflows make the known
silent loss modes loud. They are detective, not preventive: each fails a run
and files/updates a deduped `ci-finding` issue at the moment a loss becomes
visible, instead of leaving it to be discovered weeks later. Server-side on
purpose — client-side harness hooks bind one agent; a workflow binds every
pusher.

| Workflow | Loss mode it surfaces | When it runs |
| --- | --- | --- |
| `orphan-push-guard.yml` | Commits pushed to a branch after its PR merged/closed (three confirmed incidents, 2026-08-12/19) — real-time counterpart of the weekly `stranded-work.yml` audit | Pushes to `claude/**` / `codex/**` on branches cut after the workflow landed (push workflows run the pushed ref's definition; older branches keep weekly-audit coverage) |
| `merge-content-check.yml` | A merge whose head lacks the branch's newest recorded push (the stale-head variant of PR #1610); a push that *postdates* the merge routes to the orphan-push finding instead — and since this runs from the base side, that covers old branches too | Every merged PR into master |
| `automerge-disarm.yml` | Auto-merge silently disarmed by a transient check failure, stranding an armed PR (PR #1476). Label a PR `automerge-hold` to mute a deliberate hold | Every 6h; one tracking issue updated in place |

All three share one rule (see `scripts/ci/merge_loss_common.py`): they fail
open on API errors so a broken guard never blocks delivery, but a degraded
run always says so — `::warning::` plus a step-summary line — because a
guard that fails toward "healthy" is this repo's named recurring failure
mode. Two honesty notes baked into the wording they emit: a clean
`merge-content-check` pass says "no contradiction found", not "verified"
(the events feed it reads lags 30s–6h and retains ~300 events, which can
hide a final push but cannot fabricate a false alarm); and the absence of
an `orphan-push-guard` run on an old branch is a coverage gap, not a
clean verdict.

If `orphan-push-guard` fails your push: stop pushing to that branch. The
work is not lost — follow the cherry-pick recipe in the issue it filed
(fresh branch off `origin/master`, new PR). A dead-branch push whose
commits are all already landed is classified PRUNABLE and passes without
an issue. If you are deliberately reusing a branch name for a new round
of work, the guard fires until the new PR opens — open it and close the
finding as a false positive (fresh `<author>/<topic>-<id>` names avoid
this entirely).

## Quick reference

| Situation | Do this |
| --- | --- |
| Ship any change (Codex or Claude CLI) | `./scripts/dev/ship.sh "msg"` — defaults to a draft PR |
| Ship the whole dirty worktree | `./scripts/dev/ship.sh --stage-all "msg"` |
| Operator asks to ship/finish/deliver/open PR | Branch, commit, push, and open the draft PR without an extra confirmation |
| Preview the route first | `./scripts/dev/ship.sh --plan "msg"` |
| Claude on the web harness | Already parks a draft PR on its `claude/...` branch — nothing extra needed |
| About to touch a single-writer surface | Check for an in-flight PR first; branch from its head if one exists |
| Operator explicitly wants auto-merge | `./scripts/dev/ship.sh --auto-merge "msg"` (not the default) |
| PR is approved and you want it to land unattended | `gh pr ready <n> && gh pr merge --auto <n>` |
| Tempted to stack a third PR on a stack | Fold it into the one below instead |
| Docs/tests-only, knowingly skipping the PR | `./scripts/dev/ship.sh --direct "msg"` (the opt-out) |

## Per-entrypoint mapping

- **Codex (CLI):** stage, then `ship.sh "msg"` — its default `auto` route opens
  a draft PR for every change. Report the delivery line at closeout
  (`/closeout`). Use `--direct` only for docs/tests-only pushes you knowingly
  want to skip the PR for.
- **Claude (CLI, plugin harness):** same as Codex — `ship.sh "msg"`.
- **Claude (web/cloud harness):** the harness already enforces the convention
  (fixed `claude/...` branch + draft PR). Keep work on that branch; let the
  draft PR be the delivery artifact.
