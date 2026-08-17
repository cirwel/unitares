# GitHub Workflow Conventions

One delivery contract for every agent that pushes to this repo: Codex, Claude
CLI, and Claude web/cloud. The contract separates authorship from merge
authority while keeping every change visible and recoverable as a PR.

## 1. Branch naming

Use an author-prefixed branch:

```text
<author>/<topic>-<short-id>
```

`<author>` is normally `codex` or `claude`. The prefix is operational
provenance: the merge conductor selects the opposite host as the first reviewer.
An unknown prefix is deliberately not guessed and is escalated.

Both existing generators satisfy the convention:

- `ship.sh` mints `<author>/auto/<timestamp>-<slug>` when it needs a branch.
- The web/cloud harness supplies `claude/<topic>-<id>`.

Never push to `main` or `master`.

## 2. Delivery

Every completed session publishes a draft PR. The default `ship.sh` route also
marks that draft for autonomous review:

```bash
./scripts/dev/ship.sh "type(scope): concise message"
```

The marker means “the author is finished; the independent merge control plane
may evaluate this exact head.” It does not mean the author reviewed or approved
its own work.

- `--stage-all` stages the full worktree before shipping.
- `--plan` previews the route without mutation.
- `--draft-pr` opens an unqueued/manual draft.
- `--direct` is a deliberate docs/tests-only PR opt-out.
- `--auto-merge` is a deprecated compatibility alias for the same queued draft;
  it no longer pre-arms GitHub directly.

For an existing web/cloud draft, the finishing agent adds `merge:auto` after
its tests and closeout pass:

```bash
gh pr edit <number> -R cirwel/unitares --add-label merge:auto
```

If the label has not been installed yet, leave the draft unqueued and report
that explicitly. Do not treat a failed label write as successful delivery.

## 3. Autonomous merge authority

After rollout activation, the resident conductor owns the normal Ready/arm
transition. Authoring agents do not ready or directly auto-merge their default
queued PRs. During the report-only soak, queue markers are inert and a human
maintainer remains the merge gate; do not assume a marked draft will land by
itself.

For each queued PR, the conductor:

1. serializes the train to one armed PR;
2. waits for a current branch and green non-review CI;
3. requires clear surface claims and resolved review threads;
4. classifies path/churn risk deterministically;
5. requests a tool-disabled review from the opposite host first;
6. requires a second fresh review for runtime changes;
7. publishes `agent-review` against the exact head SHA;
8. rechecks every mutable gate after review to close the review-to-merge race;
9. refuses execution unless strict branch protection requires `agent-review`;
   and
10. marks Ready and arms GitHub native auto-merge.

GitHub branch protection—not the model—performs the final merge gate. The
required `agent-review` context makes a base refresh invalidate the prior
approval automatically.

Full policy and rollout: [Autonomous Merge Conductor](merge-automation-plan.md).

## 4. Human/root exceptions

Human attention is exception-based. Root/control surfaces remain parked unless
the `merge:root-approved` label event is attributable to the separately
configured root-approver GitHub App:

- database migrations;
- identity, authentication, and security authority;
- GitHub Actions, branch protection, release, and deployment permissions;
- `AGENTS.md` / `CLAUDE.md` delivery-contract changes;
- `ship.sh`, the merge conductor, and its resident configuration;
- oversized or otherwise unreadable review evidence.

The shared `cirwel` credential cannot authorize root automation: a label event
without the configured App ID is rejected. When no root-approver App is
configured, these PRs stay manual. Verified root approval does not skip review;
it permits two independent reviews to run. Any disagreement still holds the PR.

`merge:hold` is the unconditional per-PR stop. `merge:escalate` records a
policy/review refusal. To requeue a corrected PR, push a new commit; remove the
escalation label only after addressing the recorded reason.

## 5. Parallel work and single-writer surfaces

Before touching a listed single-writer surface, follow the `AGENTS.md` /
`CLAUDE.md` collision search and `git surface claim` contract. The conductor
will not arm any PR while a repository claim is active, but that merge-time
check does not excuse authors from claiming before edits.

When correcting an architecture fact in prose, grep reader-facing docs
(`README`, `docs/` outside proposals, `skills/`, `AGENTS.md`, and `CLAUDE.md`)
for the old claim and update every copy in the same PR. Add a row and deny
pattern to the Contested Claims Registry in `docs/dev/CANONICAL_SOURCES.md` so
`check_doc_health.py` blocks the stale wording from returning.

Sweep stale/superseded branches through
`docs/operations/branch-hygiene-runbook.md`. `git branch-hygiene` is dry-run by
default and must hold branches with unique unmerged work for review.

Keep stacks at most two deep. Each lower merge moves the base and invalidates
the upper review. If two changes touch the same files and must land together,
prefer one coherent PR over a deep stack.

Close or land drafts promptly. An unfinished draft is safe because it has no
queue marker, but it still accumulates merge and review context debt.

## 6. Why the compatibility service still says `pr-babysitter`

The historical babysitter polled already-armed stale branches every five
minutes. That work is no longer necessary: the repository setting “always
suggest updating pull request branches” makes GitHub update an armed PR after
the base moves. This was confirmed end-to-end on 2026-08-14: #1653 merged,
#1658 became `BEHIND`, GitHub moved its head about two minutes later, reran CI,
and merged it without a script or human branch update.

The installed LaunchAgent label and shell filename are retained only to avoid
a second resident during migration. The shell now delegates to the conductor;
with absent or `0` flags it is report-only. Do not restore a separate polling
updater for armed PRs: it races GitHub's native updater and burns a redundant
CI cycle. The conductor requests an update only for a queued draft, because a
draft cannot be armed and therefore cannot use the native armed-PR updater yet.
For an armed head that stays `BEHIND`, a persisted 15-minute timer permits one
guarded fallback update, preventing a failed native refresh from blocking the
serial train indefinitely without racing the normal two-minute path.
`unitares-governance-plugin`, where auto-merge is disallowed, remains the
manual `gh pr update-branch` exception.

## 7. Native merge queue

A GitHub merge queue would replace the conductor's serial scheduling and branch
refresh logic. GitHub currently exposes merge queues only to organization-owned
repositories; `cirwel/unitares` is user-owned. The repository workflows already
carry `merge_group` triggers where locally controllable so a future transfer can
adopt the native queue without discarding the independent review status.

An organization transfer would not make the queue immediately safe. The two
required CodeQL default-setup checks (`Analyze (actions)` and `Analyze
(python)`) do not run on `merge_group`. Before enabling a native queue, either
remove those contexts from the required set (while leaving CodeQL advisory on
PRs) or convert CodeQL to an advanced-setup workflow with a `merge_group`
trigger. Verify the live default-setup state before relying on this dated
constraint.

Do not create another polling updater. The local conductor plus GitHub native
auto-merge is the current scheduler.

## Quick reference

| Situation | Action |
|---|---|
| Finish normal agent work | `./scripts/dev/ship.sh "msg"` |
| Finish the whole worktree | `./scripts/dev/ship.sh --stage-all "msg"` |
| Preview delivery | `./scripts/dev/ship.sh --plan "msg"` |
| Publish but do not queue | `./scripts/dev/ship.sh --draft-pr "msg"` |
| Queue a completed web/cloud draft | add `merge:auto` |
| Stop conductor action | add `merge:hold` |
| Root/control automation approved | configured root-approver App adds `merge:root-approved` |
| Inspect without writes/models | `python3 scripts/ops/merge_conductor.py --json` |
| One-shot shadow review | `python3 scripts/ops/merge_conductor.py --pr N --review --json` |
| Legacy `--auto-merge` caller | command is now a deprecated alias for the queued draft path |

## Closeout

Delivery still means branch, commit, push, and PR—not “tests passed locally.”
Run the global `git prepr` and `git closeout --strict` gates in their required
order. Report the branch, PR URL, tests, dirty files, unpushed commits, active
surface claims, and lingering repository processes.

A queued draft may merge after the author exits. Report it as queued/pushed,
not merged, unless GitHub explicitly shows it merged at closeout.
