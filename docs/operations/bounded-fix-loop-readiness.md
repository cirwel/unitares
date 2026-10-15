# Bounded fix-loop readiness

Audit date: 2026-10-14. Classification: **fix / tooling / documentation**.
This is an execution plan using existing workflows, not a scheduler, new tool,
instrument, model channel, enrollment, or RSI actuator. The feature freeze in
force at the audit date was lifted by the operator on 2026-10-15 (#2816; KG
decision `2026-10-15T06:03:44.876094+00:00`). The lift alone authorizes
nothing here: new tools, actions, instruments and model channels stay
operator-reserved.
Deployment-specific evidence and classifications belong in the operator's
private fleet repository, not in the public automation census example.

## Readiness verdict

The existing path can discover defects and deliver reviewed fixes. Scheduled
jobs and successful shell exits alone do not establish closed-loop reliability,
and operational closure does not establish adaptation. Before unattended
operation, reconcile the existing census with scheduler state, terminal
receipts, expected artifacts, and accountable owners. A healthy `/health`
response covers the server's advertised checks, not every automation.

The October audit exposed three repairable evidence defects: plists were
reported active without a scheduler read; stale start receipts could outlive
their process or overwrite newer completions; and the dialectic canary compared
a raw session credential with its redacted audit reference. Repairs preserve
the original evidence and the existing safety gates. Canary audit lookup
success is not evidence of terminal review success.

## Dependency order

| Step | Work using existing mechanisms | Exit evidence |
|---|---|---|
| 1. Establish truth | Run `unitares-automations census`; compare launchd/systemd state, Hermes execution ledger, relevant GitHub runs, logs and output artifacts. Preserve intentional pauses and historical receipts. | Dated inventory with observed, configured, unknown and intentionally paused states separated. |
| 2. Repair evidence and remove dead wiring | Fix the existing census and canary; unload jobs whose removal is already an operator decision; archive recoverable configuration. Investigate duplicates by backend, target, state directory and schedule before disabling anything. | Regression tests, owned draft PRs, deploy receipt, then positive observation on the installed revision. |
| 3. Establish ownership | For each in-scope job, name the accountable principal, verifier, expected output, cadence, timeout, write surface and escalation target in private overrides. Confirm the owner can receive failures; a label alone is insufficient. | Reviewed metadata, no unresolved competing writer or scheduler for that surface. |
| 4. Demonstrate bounded closure | Use the existing discovery → disposition → claimed branch → focused fix → tests → draft PR → existing review/queue → owning deployment command → observed result path. | One complete failure-to-deployment-to-observation receipt chain, with rollback tested where material. |
| 5. Freeze a fixed baseline | Hold the action library, decision rules, budgets and review policy constant. Classify each failure exactly once as action, no-action, duplicate or invalid. | Frozen artifacts and externally verified outcomes; descriptive lane O only until enrollment. |
| 6. Enroll the registered comparison | Fill and freeze the separate enrollment artifact described below, only under the required scientific/operator authority. | Complete immutable enrollment, isolated arms, sealed held-out tasks and a named blinded evaluator. |
| 7. Execute and read once | Run matched discovery blocks; freeze deployments before held-out reads; apply the registered analysis and stop rules. | Full receipts, primary adaptive-minus-fixed estimate, interval, p-value, exclusions and safety record. |

Suggested initial operational bounds, to freeze before unattended use: **one
defect and one owned PR at a time; at most two edit/test attempts; a 30-minute
work window; no additional metered spend; no deployment outside the existing
owner/review/merge/deploy policy**. These are proposed operational ceilings, not
changes to the registered evaluation. Expiry means leave a durable disposition
and stop. A missing owner, unknown command outcome, failed dependency, scope
expansion, active surface claim, governance pause, failed required check or
security-sensitive finding also stops the write path. Do not repeat an effect
whose outcome is unknown before checking its receipts.

Success means externally verified resolution of the original failure without
regression after deployment. A green unit test, PR, summary, KG entry or EISV
estimate is not that outcome. Preserve failed attempts and no-ops; never rewrite
history into success. Roll back only the loop's own deployed change through
the owning mechanism; never undo another session's work.

## Immutable safety surfaces

The loop may read these surfaces but may not modify them, their tests, or the
configuration that changes their effect. A needed repair becomes an explicitly
reserved draft proposal outside the loop's action library.

- Auth, credential handling, identity binding and enforcement: OAuth, REST/MCP
  access gates, `src/mcp_handlers/identity/`, middleware identity checks,
  effect grants, attestations and lease authorization. Use the complete list
  from `scripts/dev/review_policy.json` at the frozen baseline.
- Governance enforcement: risk/void/pause/recovery thresholds and mechanisms,
  `config/governance_config.py`, `src/governance_monitor.py`, verification
  floors, corroboration weights and every symbol/test in
  `scripts/dev/governance_sensitivity_manifest.tsv`.
- Review and delivery authority: the review policy/gate, CI requirements,
  merge queue, ownership/claim rules, deployment approval and rollback rules,
  global/repository agent contracts, and the GitHub App credential boundary.
- Scientific rules: registered protocols, enrollment manifests, scorers,
  eligibility, exclusions/missingness, held-out data, assignment/analysis seeds,
  sample size, budgets, read dates and stop rules. In particular, preserve the
  separate **2026-12-01 EISV outcome-grounding read** and the forecast trial.
- Safety/accountability records: original failures, dispositions, audit and
  deployment receipts, rollback records, cross-arm isolation, secret stores,
  human escalation destinations and operator stop controls.

Freeze these by commit/configuration digest in the enrollment artifact. The
repairing policy cannot redefine its own validator, relax a stop, add actions,
grant itself authority, or select a favorable outcome definition.

## Registered adaptive-versus-fixed evaluation

[`self-improvement-loop-evaluation-v0.md`](../proposals/registered/self-improvement-loop-evaluation-v0.md)
registered at the merge of #1806, `c68d699790d765403ee774b95f1cbda0ca12d6b3`,
on 2026-08-22. Its original DRAFT header is historical. Registration is not
enrollment; no enrollment is recorded in the registry at this audit.

The three arms are **N** (observation only), **F** (frozen remediation policy)
and **A** (outcome-conditioned action selection). F and A share the same action
mechanism, reviewer policy, compute/model configuration and intervention slots;
a no-op consumes a slot. A may update only its declared selection surface from
prior discovery blocks. Neither arm sees held-out tasks or scores before its
deployment freezes, and no prompts, memories, commits or findings cross arms.

Before starting, freeze baseline and arm digests, task-family and split
manifests, external scorer digests, sample-size calculation, read date,
assignment/bootstrap/randomization seeds, fixed action library, adaptive update
surface, resource ceilings, reviewer instructions, receipt schema/storage,
safety stops/common-mode repair procedure, blinded evaluator and publication
owner. Do not invent missing values during the run. Incomplete enrollment
permits at most an explicitly authorized plumbing pilot, not a confirmatory
claim.

The unit is the independent task family. Primary outcome: externally verified
held-out success; primary estimand: paired family-level **A minus F**. Use the
registered 10,000-resample family-cluster bootstrap and 10,000-draw paired
sign-flip test. Support requires two-sided p ≤ 0.05, a 95% interval strictly
above zero, complete receipts, matched budgets, no held-out leak and no safety
stop. F versus N is secondary and cannot rescue a failed A-versus-F result.
Report a negative estimate as observed adaptive underperformance with its
interval. Safety repairs are common-mode and invalidate the current block;
leaks, invalid receipts and scorer defects follow the registered invalidation
rules. One confirmatory read; no favorable subset, endpoint substitution,
sample-size extension or rerun to rescue failure.

Until that comparison succeeds, describe verified repairs as **operational
closure**, not demonstrated learning or recursive self-improvement.
