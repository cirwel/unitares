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
It is author-controlled routing metadata, not cryptographic identity proof. An
unknown prefix is deliberately not guessed and is escalated.

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

For a new PR, the queued routes place the autonomous marker in the creation-time
body. For an existing PR without that marker, they add the set-valued
`merge:auto` label so they cannot overwrite a concurrent maintainer body edit.
If that bootstrap label is not installed yet, `ship.sh` prints
`[ship] left unqueued: merge:auto label not installed`, still reports the PR
URL, and succeeds only for the commit/push/PR delivery; it does not claim queue
delivery. Any label lookup failure or a failed write when the label does exist
still fails the command. Queueing preserves an existing maintainer-owned Ready
or auto-merge state while execution is dormant; once active, the conductor
itself parks an armed PR before a fresh review. Explicit
`--draft-pr`/`--open-pr` removes the ordinary marker and `merge:auto` label and
disables auto-merge on an existing PR so the requested manual state remains a
real opt-out.

For an existing web/cloud draft, the finishing agent adds `merge:auto` after
its tests and closeout pass:

```bash
gh pr edit <number> -R cirwel/unitares --add-label merge:auto
```

If the label has not been installed yet, leave the draft unqueued and report
that explicitly. A failed lookup or label write is a real delivery error; only
an authoritative successful lookup showing that the bootstrap label is absent
uses the explicit unqueued-success path.

After the App-bound check becomes required, this same label is also the liveness
path for human/imported same-repository branches; they do not need a Codex or
Claude prefix and still receive both review families. For a fork contribution,
mirror the reviewed head to a maintainer-owned same-repository branch before
adding `merge:auto`, or use the OS-root maintenance window. An unqueued PR has
not requested its required review check and is therefore deliberately not yet
mergeable.

## 3. Autonomous merge authority

After rollout activation, the resident conductor owns the normal Ready/arm
transition. Authoring agents do not ready or directly auto-merge their default
queued PRs. During the report-only soak, queue markers are inert and a human
maintainer remains the merge gate; do not assume a marked draft will land by
itself.

Execution is additionally bound to a root-owned deployment manifest, a
dedicated non-root conductor UID, and a second provider-reviewer UID; both
differ from every authoring UID. The reviewer receives only its subscription
HOME through a root-deployed fixed-mode worker. Startup proves that it cannot
read the conductor credential root, review-App key, or secrets. Fixed provider
CLI paths, isolated/no-site Python and its complete import roots, and the
conductor deploy tree must also be root-owned and non-author-writable. The
service reads root-attested author `git-surface` registries directly, keyed by
repository remote rather than the foreign-owned deploy checkout. Each registry
must be that author's canonical `~/.local/state/git-surfaces` root; execute mode
rejects custom state-root overrides rather than trusting an empty lookalike.
Ordinary GitHub calls receive only a repository-scoped fine-grained PAT with
Administration read, Pull requests write, Contents read, and Checks read; the
resident must have neither Administration nor Contents write. Pull requests
write covers PR labels and timeline comments as well as the SHA-bound
pull-request update endpoint; Issues write is not required for those PR-scoped
operations. The
historical same-user LaunchAgent is report-only and cannot satisfy this
preflight; the conductor command locally requires OS root for gate
install/removal, and any root-approver service and key remain separate again.
This local check cannot constrain a GitHub admin token used directly, so the
root/operator credential must be unavailable to author, conductor, and reviewer
processes outside the declared maintenance window.

For each queued PR, the conductor:

1. serializes the train to one armed PR with a shared lease-plane mutex (the
   local process lock is only a same-host optimization); execute mode fails
   closed if that global lease is held or unavailable;
2. waits for a current branch and green required non-review CI; a red advisory
   check does not become an undocumented permanent gate;
3. requires clear surface claims and resolved review threads;
4. classifies path/churn risk deterministically;
5. requests a tool-disabled review from the opposite host first;
6. requires a second fresh family for every change because branch attribution
   is routing metadata, not authenticated authorship; both reviewers receive
   the identical immutable evidence prompt and neither sees the other's verdict
   before deciding;
7. parks the target as an observably unarmed draft, then publishes
   `agent-review` from the dedicated review GitHub App against the exact head;
8. rechecks every mutable gate both after review and after check publication,
   marks the draft Ready, then re-reads SHA, intent/hold/root authority,
   mergeability, CI, threads, and claims, renews the lease, then performs a
   final claims/checks/review-status/threads/root-proof sweep. Its last network
   read is the full PR snapshot, used locally to revalidate check state, target
   SHA/authority/Ready/mergeability, and every competing arm before the
   separate arm call;
9. refuses execution unless strict branch protection pins `agent-review` to the
   configured App ID and requires review conversations to be resolved. If root
   automation is configured, `agent-root-approval` must also be required and
   pinned to the distinct root App;
   and
10. renews global lease ownership before approval, Ready, and arm, then keeps
    the lease until the target is observably armed or merged. This serializes
    conductor instances, not a maintainer acting directly on GitHub; operators
    must not manually arm queued PRs while execution is live, and the
    pre/post-arm reads fail visibly on an observed collision. An already-merged
    target cannot be rolled back; a simultaneous external arm is reported as an
    invariant error for operator recovery.

GitHub branch protection—not the model—performs the final merge gate. The
required App-bound `agent-review` check makes a base refresh invalidate the
prior approval automatically. The required-conversation rule closes a newly
opened-thread race. A configured root App publishes neutral/success for
ordinary heads, while a root/control head requires its explicit successful
exact-SHA result; the App-bound required check makes a newer root failure a
merge-point veto. A legacy status or same-named check from another
producer does not satisfy it.

Queue intent and `merge:hold` are hard conductor controls through the final
post-Ready, pre-arm read. Once GitHub accepts native auto-merge, required checks
and branch protection are the atomic merge-point authority. A later cycle will
try to disarm an observed hold or lost intent, but that post-arm response is
best-effort; use `gh pr merge --disable-auto` for an immediate operational stop.
Surface claims remain a cooperative authoring lock, not a GitHub merge
predicate: a claim created after the conductor's final claim read can cause
rebase work but cannot bypass required checks, conversation resolution, or
branch protection. Claim before editing rather than at merge time.

Full policy and rollout: [Autonomous Merge Conductor](merge-automation-plan.md).

## 4. Human/root exceptions

Human attention is exception-based. Root/control surfaces remain parked unless
the `merge:root-approved` label event is attributable to the separately
configured root-approver GitHub App and the same App has published a successful
`agent-root-approval` check on the exact current head:

- database migrations;
- identity, authentication, and security authority;
- authorization, OAuth/JWT, token, credential, and session authority;
- GitHub Actions, branch protection, release, and deployment permissions;
- `AGENTS.md` / `CLAUDE.md` delivery-contract changes;
- `ship.sh`, the merge conductor, and its resident configuration;
- merge-serialization lease-plane implementations, clients, and deployment;
- oversized or otherwise unreadable review evidence.

The shared `cirwel` credential cannot authorize root automation: a label event
without the configured App ID is rejected. `merge:root-approved` is authority
only, never queue intent; the PR must also carry the ordinary author marker or
`merge:auto`. A push invalidates the SHA-bound check and needs fresh root
approval. When no root-approver App is configured, these PRs stay manual.
Verified root approval does not skip review; it permits two independent
reviews to run. Any disagreement still holds the PR.

“Manual” requires a root maintenance window once `agent-review` is required.
Live `master` protection was rechecked on 2026-08-17: strict updates and
`enforce_admins` are both enabled, so an administrator cannot click past a
missing conductor check. Without the root-approver App, use this fail-safe
sequence instead:

1. stop conductor execution and reload the isolated service with execution set to `0`;
2. as OS root, remove only `agent-review` with
   `--execute --uninstall-gate --no-log --lock /var/run/unitares-merge-conductor-setup.lock`;
3. complete human/root review, mark the root PR Ready, and merge it through the
   remaining protected checks;
4. deploy and shadow-check the merged conductor version;
5. reinstall `agent-review`, canary a normal queued PR, then re-enable execution.

Normal autonomous landing is paused for the whole window. This deliberate
maintenance path prevents a required-check deadlock without letting the
control plane authorize its own changes. The independently authenticated
root-approver App is the continuous-operation alternative.
Every root setup/rollback invocation uses `sudo -H`, the root-deployed absolute
Python/script paths, and the explicit root-owned lock above; see the complete
commands in `merge-automation-plan.md`.

`merge:hold` is the unconditional per-PR stop. `merge:escalate` records a
policy/review refusal. To requeue a corrected PR, push a new commit; remove the
escalation label only after addressing the recorded reason.

## 5. Parallel work and single-writer surfaces

Before touching a listed single-writer surface, follow the `AGENTS.md` /
`CLAUDE.md` collision search and `git surface claim` contract. The conductor
will not arm any PR while a repository claim is active, but that merge-time
check does not excuse authors from claiming before edits. Production rollout
must attest each author UID's real `git-surface` state directory and prove from
the conductor account that a claim created in every linked worktree is visible;
the root-owned deploy checkout is not the registry authority.

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

The historical LaunchAgent label and shell filename are retained for report-only
migration shadow. Autonomous execution runs only in the root-attested dedicated
conductor account/LaunchDaemon and delegates model calls to the separate
reviewer UID; the source plist and boundary-manifest contract encode that
separation.
With absent or `0` flags either entrypoint is report-only. Do not restore a separate polling
updater for armed PRs: it races GitHub's native updater and burns a redundant
CI cycle. The conductor requests an update only for a queued draft, because a
draft cannot be armed and therefore cannot use the native armed-PR updater yet.
For an armed head that stays `BEHIND`, a persisted 15-minute timer permits one
guarded fallback update, preventing a failed native refresh from blocking the
serial train indefinitely without racing the normal two-minute path. Both
fallback paths use GitHub's pull-request update REST endpoint with
`expected_head_sha`; the fine-grained service PAT's Pull requests write grant
authorizes this constrained server-side update without granting arbitrary
Contents write.
`unitares-governance-plugin`, where auto-merge is disallowed, remains the
manual `gh pr update-branch` exception.

## 7. Native merge queue

A GitHub merge queue would replace the conductor's serial scheduling and branch
refresh logic. GitHub currently exposes merge queues only to organization-owned
repositories; `cirwel/unitares` is user-owned. The repository workflows already
carry `merge_group` triggers where locally controllable so a future transfer can
adopt the native queue without discarding the independent review check.

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
| Publish but never auto-land | `./scripts/dev/ship.sh --draft-pr "msg"` |
| Queue a completed web/cloud draft | add `merge:auto` |
| Stop conductor action | add `merge:hold` |
| Root/control automation approved | root App adds `merge:root-approved` and passes `agent-root-approval` on the exact head |
| Root/control change without App | stop execution; OS root removes `agent-review`; human merge; shadow, root reinstalls, canary |
| Inspect without writes/models | `python3 scripts/ops/merge_conductor.py --json` |
| One-shot shadow review | `python3 scripts/ops/merge_conductor.py --pr N --review --json` |
| Roll back required check | as OS root, use the plan's `sudo -H` command with `--execute --uninstall-gate --no-log --lock /var/run/unitares-merge-conductor-setup.lock` and the operator GitHub credential |
| Legacy `--auto-merge` caller | deprecated queued-draft alias; it does not arm immediately and lands only after conductor activation |

## Closeout

Delivery still means branch, commit, push, and PR—not “tests passed locally.”
Run the global `git prepr` and `git closeout --strict` gates in their required
order. Report the branch, PR URL, tests, dirty files, unpushed commits, active
surface claims, and lingering repository processes.

A queued draft may merge after the author exits. Report it as queued/pushed,
not merged, unless GitHub explicitly shows it merged at closeout.
