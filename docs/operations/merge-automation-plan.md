# Autonomous Merge Conductor

**Status:** implemented, report-only by default. Autonomous execution remains
flag-gated until the rollout gates below are met.

## Outcome

Routine PRs no longer need a human to click Ready, Update branch, and Merge.
The author declares its work complete; a separate reviewer process judges the
exact Git head; deterministic gates enforce repository policy; GitHub performs
the protected merge. Human attention is reserved for changes that redefine the
gate or production authority.

```text
author -> queued draft -> current branch + CI -> independent review(s)
       -> SHA-bound agent-review status -> Ready + native auto-merge

root/control change ---------------------> operator root approval -> 2 reviews
conflict / red CI / disagreement --------> hold or escalation
```

The implementation is `scripts/ops/merge_conductor.py`. The installed
`com.unitares.pr-babysitter` LaunchAgent keeps its historical label and filename
for an in-place upgrade, but `scripts/ops/pr-babysitter.sh` is now only a
compatibility entrypoint for the conductor. An already-installed plist has no
conductor flags and therefore selects the safe report-only default as soon as
the new entrypoint lands.

## Trust boundaries

The conductor does not let an authoring model merge its own work.

- A `codex/*` branch is reviewed by Claude first.
- A `claude/*` branch is reviewed by Codex first.
- The attributable defaults are Claude `opus` and Codex `gpt-5.6-sol`;
  `UNITARES_MERGE_CLAUDE_MODEL` and `UNITARES_MERGE_CODEX_MODEL` override them.
  Claude's provider envelope records every routed model (including helper
  models). Codex currently records the explicit request and emits a provenance
  warning because its CLI JSON stream does not report the routed model.
- Low-risk non-runtime changes need one review.
- Runtime/dependency changes need two fresh review contexts: the opposite host
  first, then a synthesis/review on the author host.
- Unknown branch provenance is an escalation, not a guessed identity.
- Reviewer tools are disabled/read-only. PR prose and patches are explicitly
  delimited as untrusted evidence with a fresh unpredictable boundary/verdict
  nonce. This prevents replayed/static patch JSON or a copied terminator from
  being parsed as the verdict; it does not make a model immune to in-context
  instruction attack, so unanimous review and deterministic gates still carry
  the decision.
- A malformed, inconsistent, unavailable, or incomplete verdict cannot approve.
- Before any model call, the resident verifies that the installed Claude and
  Codex CLIs still advertise every isolation flag on which the adapters rely.

This is contextual and procedural separation under the current single-operator
deployment, not cryptographic separation: author agents and the resident may
use the same `cirwel` GitHub credential. The SHA-bound status prevents stale
approval, but a sufficiently privileged process could forge that status.
Root labels are stricter: the latest label event must carry the configured
root-approver GitHub App ID; a shared-user event cannot authorize root
automation. Before treating mutually distrustful ordinary-change authors as in scope,
run the conductor as a dedicated GitHub App and pin the required check to that
App ID. That hardening is not required for the repository's documented
single-operator threat model, but the distinction must remain explicit.
Within the conductor, a success status is never a review cache for an unarmed
PR: it always runs a fresh current-process review before arming.

Reviewers return one of `approve`, `deny`, `needs_evidence`, or `escalate`.
Only unanimous `approve` satisfies the gate.

## Queue intent

Draft still means the author has not granted merge authority by itself. A PR
enters the conductor only when it contains:

- the marker `<!-- unitares-merge-intent: autonomous -->`, which `ship.sh`
  adds on its default route; or
- the `merge:auto` label, useful for web/cloud-agent PRs and existing drafts; or
- `merge:root-approved`, but only when its latest label event comes from the
  App ID configured by `UNITARES_MERGE_ROOT_APPROVER_APP_ID`.

`ship.sh --draft-pr` creates an unqueued draft. `merge:hold` overrides every
other signal and is the per-PR kill switch. `merge:escalate` parks a PR after a
policy or review refusal.

## Deterministic gates

Before spending a model review or changing PR state, the conductor requires:

1. A user-private non-blocking process lock under `~/.cache/unitares/` prevents
   overlapping five-minute/manual cycles, and no other PR may be armed. The
   train is serial, so one merge cannot invalidate a second concurrently
   approved head.
2. No active repository surface claim. An unreadable claim registry also
   blocks; absence is never inferred from a failed probe.
3. Same-repository PR targeting the configured protected branch, recognized
   agent branch prefix, and confirmed mergeability (not unknown or conflicting).
4. Branch current with `master`. A `BEHIND` queued draft cannot use GitHub's
   armed-PR updater yet, so the conductor requests one update and leaves review
   for a later cycle. An already-armed PR is first left to GitHub's native
   updater. If the same head remains behind for 15 minutes, a user-private
   persistent timer permits one guarded `update-branch` fallback and resets.
5. All non-review CI checks complete successfully.
6. All review conversations resolved. More than 100 threads is an explicit
   partial-read refusal, not a silent zero.
7. At most 80 changed files, 8,000 changed lines, and a 120 KB review patch.
   Larger, partial, or binary-only evidence is escalated instead of truncated
   into a false approval.
8. Immediately before Ready/arm, the conductor rechecks head and base SHA,
   intent/hold/root labels, mergeability, CI, review threads, surface claims,
   review status, and the one-armed invariant. Commit-status lookup paginates
   rather than treating the first 100 records as complete.

The approval is published as the `agent-review` commit-status context on the
reviewed SHA. Every execute cycle first verifies that branch protection is
strict, requires this context, and that repository auto-merge is enabled. If
GitHub refreshes the branch, the new SHA has no approval and cannot merge until
the conductor reviews it again.

## Risk policy

| Tier | Examples | Gate |
|---|---|---|
| Low | explicitly recognized ordinary documentation | one opposite-host review |
| Medium | tests, Python/BEAM/dashboard/runtime, scripts/skills, dependencies, and every unclassified path | two reviews |
| Root/control | migrations, identity/auth, GitHub workflows, repository test/CI configuration, releases, branch protection, delivery contract, conductor and its tests | verified root-approver App label, then two reviews; otherwise manual |

The root path list lives in code and is unit-tested. Adding or weakening an
exception modifies the conductor itself, so that change is recursively a root
decision.

## Operating modes

Classification-only shadow is the default and makes no GitHub changes or model
calls:

```bash
python3 scripts/ops/merge_conductor.py --json
```

One-shot report-only review invokes models but does not comment, set status,
ready, or merge:

```bash
python3 scripts/ops/merge_conductor.py --pr 123 --review --json
```

Execution can be requested explicitly after rollout:

```bash
UNITARES_MERGE_CONDUCTOR_EXECUTE=1 \
  python3 scripts/ops/merge_conductor.py --json
```

The five-minute LaunchAgent reads the same environment flags. Its template
ships with both set to `0`; an old installed plist has neither variable and
therefore gets the same report-only default.

`UNITARES_MERGE_ARMED_STALL_S` changes the native-update fallback threshold
(default 900 seconds). Lock and stall state live under `~/.cache/unitares/` by
default and are mode `0600`.

## Rollout

This order is load-bearing:

1. Land the conductor bootstrap through the existing root/human gate. The gate
   may not authorize its own installation.
2. Deploy the new script and compatibility entrypoint, leaving execution off.
   This retires the redundant polling updater; the repository's native
   auto-update setting continues refreshing already-armed PRs.
3. Create/update the four labels:

   ```bash
   python3 scripts/ops/merge_conductor.py --execute --install-labels --no-log
   ```

   Root/control PRs remain manual unless a separate approval service is
   installed and its GitHub App ID is set in
   `UNITARES_MERGE_ROOT_APPROVER_APP_ID`.

4. Run classification-only shadow for at least 24 hours. Compare every
   `would_review`, wait, hold, and escalation with the actual PR state.
5. Run a report-only model review on at least one low-risk Codex PR and one
   low-risk Claude PR if both are available.
6. Migrate or finish existing open PRs. Installing the status gate while
   unqueued PRs remain would intentionally block them.
7. Install the SHA-bound required context:

   ```bash
   python3 scripts/ops/merge_conductor.py \
     --execute --install-gate --branch master --no-log
   ```

8. Install the new plist template, bootout/bootstrap it, and verify a
   report-only cycle. Then set
   `UNITARES_MERGE_CONDUCTOR_EXECUTE=1` and reload it. Leave
   `UNITARES_MERGE_CONDUCTOR_REVIEW=0`; execute mode performs reviews
   automatically.
9. Canary one low-risk PR. Confirm the comment, commit status, Ready transition,
   native auto-merge, post-merge CI, and deployment health before queuing more.

Do not install the required status before the conductor is deployed and able to
write it. Do not enable execution before the required status exists.

## Rollback and recovery

Fleet stop:

1. Set `UNITARES_MERGE_CONDUCTOR_EXECUTE=0` and reload the LaunchAgent.
2. Disable any outstanding request with `gh pr merge --disable-auto <n>`.
3. Add `merge:hold` to PRs that must remain parked.

If an armed PR remains `BEHIND` after both GitHub's native updater and the
guarded fallback, disable auto-merge, inspect the conflict/check state, run one
manual `gh pr update-branch <n>` only when mergeable, and requeue it after the
fresh CI head is visible. The JSONL log records the first-wait duration and the
fallback request.

A failed review is bound to its head SHA. Correct the patch and push a new
commit to obtain a fresh review. `--retry-review` exists for a confirmed
transient reviewer failure on the unchanged SHA; it must not be used to shop
for a more favorable answer after a substantive disagreement.

The audit trail consists of:

- `data/logs/merge-conductor.jsonl` for each cycle;
- the SHA-bound GitHub commit status;
- a structured PR comment containing reviewer/model provenance and findings;
- GitHub's Ready, auto-merge, and merge events.

## Native merge queue

GitHub's merge queue remains the preferable long-term scheduler, but GitHub
currently offers it only for organization-owned repositories. `cirwel/unitares`
is user-owned, so the conductor provides the missing serial queue. If the repo
moves to an organization, retain the independent `agent-review` gate and replace
the local serialization/update logic with the native queue after a canary.
