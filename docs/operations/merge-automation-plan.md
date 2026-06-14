# Merge Automation Plan — branch protection + operator-armed auto-merge

**Status:** plan (not yet applied). Sibling of `ci-issue-surfacing.md`; both are
steps in a surface → fix → land relay.

## Goal and the line we are NOT crossing

Make a change autonomous up to **"green and ready-for-review,"** then stop. The
agent surfaces → fixes → gets CI green → marks the draft PR ready → pings the
operator. **The operator stays the merge gate.** This honors `CLAUDE.md`'s
delivery contract verbatim ("every session lands its work as a draft PR; the
operator is the merge gate; no auto-merge by default").

What this plan *adds* is a **safe, opt-in way to grant merge-when-green on a
single PR** — so the operator's merge action can be "arm auto-merge once" at
review time instead of "watch for green, then click merge." Arming is an
**operator action**, the same deliberate tier as "mark ready." The agent does
not arm auto-merge on its own.

```
agent:    surface ── fix ── CI green ── mark ready ──┐
                                                     │  (agent stops here)
operator: review ──────────────────────── arm auto-merge (optional) ── GitHub merges on green
```

## Prerequisite: branch protection with required checks

Auto-merge ("merge when green") is only meaningful if `master` actually
*requires* the checks. Without required checks, GitHub's auto-merge would merge
the instant mergeability is satisfiable — i.e. immediately — which is not what
we want. So branch protection is the load-bearing prerequisite.

### Required check contexts

From the PR-triggered workflows today (`pull_request` → `master`):

| Workflow | Job → check-run context | Required? |
| --- | --- | --- |
| Tests | `smoke` | ✅ required |
| Tests | `test (3.12)` | ✅ required |
| Documentation Validation | `validate` | ✅ required |
| Surface Findings (experiment) | `surface` | ❌ advisory — never required |

`surface` is intentionally **not** required: findings are advisory and the job
never `--fail-on`s, so gating merge on it would be a category error.

> **Verify the exact context strings before applying.** Matrix and reusable
> workflows can alter the rendered name (e.g. `test (3.12)` vs `test`). Confirm
> against a real run:
>
> ```bash
> gh api repos/CIRWEL/unitares/commits/<pr-head-sha>/check-runs \
>   --jq '.check_runs[].name'
> ```

### Apply (operator / admin — repo-settings change, not done by the agent)

```bash
gh api -X PUT repos/CIRWEL/unitares/branches/master/protection \
  --input - <<'JSON'
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["smoke", "test (3.12)", "validate"]
  },
  "enforce_admins": false,
  "required_pull_request_reviews": null,
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
JSON
```

- `strict: true` = "branch must be up to date before merging" (forces a rebase
  on a stale PR — this is what turns a silently-conflicting auto-merge into a
  re-run). Drop to `false` if the rebase churn outweighs the safety on a
  low-traffic `master`.
- `required_pull_request_reviews: null` keeps the *human review* requirement
  off at the protection layer — the merge gate is enforced socially via the
  draft-PR contract, not by a required approver count. Set it if you want
  GitHub to also block merge without an approval.
- `enforce_admins: false` lets the operator break-glass merge past a stuck
  check. Flip to `true` for strict parity.

To enable native auto-merge at all, the repo setting must allow it:

```bash
gh api -X PATCH repos/CIRWEL/unitares --field allow_auto_merge=true
```

## Arming auto-merge (operator-invoked, per-PR, opt-in)

Two equivalent paths, both already in the toolbox:

- **`ship.sh --auto-merge`** — documented in `CLAUDE.md` as "only when the
  operator explicitly asks." This plan does not change that default; it just
  makes the `--auto-merge` path *safe* by ensuring required checks exist.
- **GitHub native** — `gh pr merge <n> --auto --squash`, or the MCP
  `enable_pr_auto_merge` tool. GitHub holds the merge until all required checks
  pass, then merges; if a check fails or a new commit lands, the merge waits.

Either way the merge only happens **after** the required contexts are green, and
only because the operator armed it on that specific PR.

## Interaction with single-writer surfaces — hard rule

Auto-merge must **not** be armed on a PR that touches a single-writer surface
(migrations, identity/onboarding, `docs/ontology/plan.md`, hot RFC docs, large
test-layout deletions — see `CLAUDE.md` "Before Starting Work on a Single-Writer
Surface") without the cross-branch coordination check first. Merge-when-green
removes the human pause that currently catches a slot/semantic collision at
merge time. For these surfaces, keep merge fully manual so the operator does the
`gh pr list --search` collision check at the moment of merge.

A future guard could make this structural: a workflow step that inspects the
diff's paths and refuses to arm (or posts a "manual-merge-only" label) when a
single-writer surface is touched. Out of scope for this plan; noted as the
natural next hardening.

## What stays a human decision

- **Whether a PR should merge at all** — never automated here.
- **Arming auto-merge** — operator action, per-PR.
- **Anything touching a single-writer surface** — manual merge, with the
  collision check.

## Rollout

1. Confirm exact check-run contexts on a real PR head (`check-runs` query above).
2. Apply branch protection + `allow_auto_merge` (operator/admin).
3. Try the loop on one low-risk PR: let the agent drive to ready-for-review,
   then operator arms `gh pr merge --auto --squash` and confirms GitHub merges
   on green.
4. Only after that burn-in, consider the single-writer-surface arming guard.
