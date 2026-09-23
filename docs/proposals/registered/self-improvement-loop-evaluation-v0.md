# Self-improvement loop evaluation - protocol preregistration v0

**Status: DRAFT.** This protocol registers at the merge commit of the PR that
introduces it. It does not start an experiment, schedule a job, or authorize
automatic deployment. Before any confirmatory episode begins, a separate
enrollment record must freeze the task corpus, sample size, assignment seed,
arm artifacts, intervention budget, model configuration, baseline commit, and
read date. Amendments after enrollment apply only to a new cohort.

## Why this exists

Closing defects after a dogfood or CI failure shows that an operational
feedback path exists. It does not show that the system learned, and a simple
loop-on versus loop-off comparison cannot distinguish adaptation from an
effective fixed automation.

An adversarial design review on 2026-08-21 identified that confound and
required three arms: no loop, fixed non-adaptive automation, and an otherwise
matched adaptive loop. The primary comparison in this protocol is therefore
adaptive versus fixed. The no-loop arm estimates the broader value of having
an intervention path, but cannot identify learning by itself.

This protocol does not test whether EISV predicts outcomes. It does not reopen
or anticipate the registered 2026-12-01 EISV outcome-grounding read. EISV may
be retained as telemetry, but it is not an outcome label, treatment assignment,
or primary endpoint here.

## Claim lanes

| Lane | Question | Evidence status |
|---|---|---|
| O - operational closure | Did a failure receive a disposition and, when appropriate, a durable reviewed update? | Descriptive receipt audit. This can establish that a loop operates. |
| F - fixed automation effect | Does a frozen remediation policy beat observation-only operation? | Secondary fixed-versus-no-loop comparison. |
| A - adaptive effect | Does outcome-conditioned policy updating improve unseen outcomes beyond the frozen policy? | Primary adaptive-versus-fixed comparison. This is the only lane that may support a self-improvement claim. |

Passing lane O does not imply passing lane A. A high volume of edits, PRs,
findings, or check-ins is activity, not improvement.

## Experimental arms

All arms start from the same release, configuration, model/provider, toolsets,
task-family distribution, reviewer policy, wall-clock allowance, and compute
budget.

| Arm | Allowed behavior | Forbidden behavior |
|---|---|---|
| N - no loop | Record failures, outcomes, and dispositions. | No remediation change before the held-out read, except a common-mode emergency fix applied to every arm. |
| F - fixed | Apply a remediation policy whose rules, action library, thresholds, and ordering are frozen at enrollment. The policy may map an observed failure class to a predeclared action. | Changing that mapping, its weights, action library, or thresholds in response to cohort outcomes. |
| A - adaptive | Use the same action mechanism and review gate as F, but update action selection from prior discovery-block outcomes. | Accessing held-out tasks or scores, bypassing review, exceeding the matched intervention budget, or importing changes from another arm. |

Each discovery block provides the same number of reviewed intervention slots to
F and A. An explicit no-op disposition occupies a slot. This prevents a larger
change budget from masquerading as adaptivity. Both arms use the same CI and
human safety review. Reviewers may accept or reject a proposed change for
safety, scope, and test adequacy, but may not supply arm-specific optimization
advice.

A critical safety repair may be applied to all three arms only as a common-mode
change. The current block is then invalidated and the next block starts from the
new shared baseline. Safety is never withheld to preserve an experiment.

## Units, isolation, and data split

The inference unit is an independent task family, not an edit-test-retry event.
Repeated variants from one family remain one cluster.

Before enrollment, each eligible family is divided into:

- A discovery set visible during the intervention phase.
- Three sealed, difficulty-matched held-out variants, one per arm.
- An external scoring rule fixed before any arm sees the discovery outcomes.

The assignment seed maps held-out variants to arms and is recorded before the
first discovery episode. Evaluators are blind to arm identity. A task is
eligible only when success can be scored externally by a deterministic test,
an independent rubric, or another outcome source that does not consume the
agent's self-report.

Arms run in separate repositories or immutable worktrees with separate runtime
state. No commits, prompts, memories, findings, or task transcripts cross arms
until the confirmatory read is complete. Shared infrastructure may carry only
the frozen baseline and outcome envelopes. Any cross-arm leak invalidates the
affected block and is reported.

## Intervention and causal receipts

Every discovery failure receives exactly one disposition. Every deployed
change must be traceable through the following chain:

| Receipt | Required fields |
|---|---|
| Failure | `failure_id`, task-family id, arm, block, timestamp, external outcome, failure class |
| Disposition | `failure_id`, `action`, `no_action`, `duplicate`, or `invalid`; reason; policy version; reviewer |
| Persistent update | `update_id`, disposition id, commit or configuration digest, changed surface, tests/checks, approval |
| Deployment | `deployment_id`, update ids, arm, baseline digest, deployment timestamp, rollback state |
| Held-out read | task-family id, sealed variant id, deployment id, scorer digest, score, missingness reason |
| Effect summary | cohort id, adaptive-minus-fixed estimate, interval, test result, exclusions, receipt coverage |

A local helper file is not a KG note, a report is not a deployment, and a PR is
not a persistent update until its accepted artifact is deployed in the named
arm. Individual update receipts establish provenance, not that one update
caused one later success. Causal inference is made only at the randomized
arm-level comparison.

## Outcomes and estimand

The primary outcome is externally verified held-out task success. The enrollment
record defines success for every task family before assignment.

For each family, compute the held-out success mean within each arm. The primary
estimand is the mean paired family-level difference:

`theta_A = success_adaptive - success_fixed`

The fixed-versus-no-loop difference is secondary. Other secondary measures are
severity-weighted failure rate, regression rate, intervention cost, latency,
rollback count, and safety incidents. Secondary measures are descriptive unless
an enrollment record explicitly registers a multiplicity correction before the
cohort begins.

EISV, coherence, check-in count, finding count, commit count, and model-written
confidence are not endpoints.

## Analysis and decision rule

The enrollment record freezes a sample size using historical or pilot variance
that contains no confirmatory-arm outcomes. Sample size may not increase after
any confirmatory score is read.

The registered primary statistic is the paired family-level mean difference.
Uncertainty is a family-cluster bootstrap with 10,000 resamples. The hypothesis
test is a paired sign-flip randomization test with 10,000 draws. Both seeds are
frozen in the enrollment record.

The adaptive self-improvement claim is supported only when all conditions hold:

1. The two-sided primary p-value is at most 0.05.
2. The 95% interval for `theta_A` lies strictly above zero.
3. No confirmatory task or score leaked before its arm's deployment froze.
4. Intervention slots, compute, model configuration, and reviewer policy were matched.
5. Every deployed update and held-out score has the complete receipt chain.
6. No safety stop was triggered.

If conditions 1 or 2 fail, adaptive self-improvement is not supported for this
cohort. If the point estimate is below zero, the result is reported as observed
adaptive underperformance with its interval, not softened to "inconclusive."
If conditions 3 through 6 fail, the cohort is invalid for the causal claim and
the operational receipts remain reportable.

The no-loop arm cannot rescue a failed adaptive-versus-fixed comparison. If F
beats N while A does not beat F, the supported conclusion is that fixed
automation helped; learning was not demonstrated.

## Read and stop rules

- There is one confirmatory read after the registered sample completes.
- Operational dashboards may monitor safety and missing receipts, but may not
  display arm outcome comparisons during enrollment.
- Missing outcomes retain their preregistered intention-to-treat handling; an
  arm-specific failure is not silently removed as infrastructure noise.
- A scorer defect discovered without viewing arm labels invalidates the family
  across every arm. A defect discovered after unblinding invalidates the cohort.
- No rerun, alternate endpoint, favorable task subset, or additional task
  family may replace a failed confirmatory result.
- A new cohort requires a new enrollment record, new held-out variants, and an
  explicit new premise. More of the same data does not revise this cohort.

## Enrollment gate

No experiment may start until one immutable enrollment artifact records:

- Baseline release and commit digest.
- Model, provider, reasoning level, toolsets, and resource ceilings.
- Task families, eligibility rules, discovery/held-out manifests, and scorer digests.
- Sample-size calculation and confirmatory read date.
- Assignment, bootstrap, and randomization seeds.
- Fixed-policy artifact and action-library digests.
- Adaptive-policy initial artifact and allowed update surface.
- Per-block intervention budget and standardized reviewer instructions.
- Receipt schema and storage location.
- Safety-stop conditions and common-mode repair procedure.
- Named blinded evaluator and publication owner.

Failure to fill any field means the system may run a plumbing pilot but may not
call the result confirmatory.

## Publication language

Lane O only:

> The system closed observed failures through reviewed persistent updates. This
> demonstrates an operating feedback path, not improvement on unseen tasks.

Lane F supported, lane A not supported:

> Frozen automation improved held-out outcomes relative to observation-only
> operation. The adaptive arm did not outperform the frozen policy, so learning
> was not demonstrated.

Lane A supported:

> Under the registered task distribution and matched intervention budget, the
> adaptive policy improved externally scored held-out outcomes over the frozen
> policy by <estimate, interval, p-value>. This is cohort-scoped evidence of
> adaptation, not proof of open-ended or generally recursive self-improvement.

## What this protocol forbids claiming

- That a loop exists because jobs are scheduled or findings are emitted.
- That defect closure proves learning.
- That loop-on versus loop-off identifies adaptivity.
- That more commits, check-ins, or interventions imply more improvement.
- That training-set repair is held-out improvement.
- That EISV or self-reported confidence is external ground truth.
- That one supported cohort proves indefinite, autonomous, or recursive improvement.

## Amendment log

- (none - v0 as registered)
