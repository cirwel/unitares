# Accountable multi-agent coordination ablation — protocol preregistration v0

- **Status:** DRAFT; no cohort enrolled and no experiment scheduled
- **Study ID:** `accountable-coordination-ablation-v0`
- **Tracking issue:** #2248
- **Protocol version:** v0.1, amended before any build, model run or
  enrollment. See the [amendment log](#amendment-log); the v0 text is preserved
  at `74bd59cd`.

This protocol registers at the merge commit of the pull request that introduces
it. Registration does not enroll a cohort, authorize model calls, read
production data, enable governed effects, or create a production switch that
agents can use to disable accountability. A separate immutable enrollment
artifact is required before any confirmatory episode begins.

## Why this exists

Frontier-lab campaigns demonstrate that groups of agents can produce useful
artifacts at scales beyond one context window. UNITARES dogfood also repeatedly
shows that communication and review surface information and lead to edits.
Those observations motivate the architecture, but they do not identify whether
UNITARES improves outcomes over an ordinary communicating harness, which
components contribute, or whether their benefit exceeds their cost.

This study tests accountable coordination. It does not test whether EISV
predicts outcomes, whether a self-improvement loop learns, or whether any
mechanism makes an autonomous system generally safe.

## Claims and estimands

The primary question is:

> Under matched models, tools, tasks, resource budgets, and external scoring,
> does full UNITARES accountable coordination improve held-out task outcomes
> relative to ordinary multi-agent coordination with ordinary note handoff?

The primary estimand is the paired task-family difference:

`theta_primary = success_D - success_B`

where D is full accountable coordination and B is ordinary coordination.

v0.1 runs only arms B and D. No component contrast is estimated. A supported
result says that the UNITARES layer as a whole improved outcomes over ordinary
coordination; it does not say which part of the layer did so.

## Experimental arms

Both arms use the same frozen source snapshot, tasks, model/provider and
reasoning level, agent count and topology, non-treatment tools, prompts outside
the treatment, token and wall-clock ceilings, validator, and
operator-intervention budget. Arm-specific coordination adapters and capabilities
differ only as declared below.

All model-visible coordination, handoff, review, and policy-interaction calls and
tokens count against the common model-call and token ceilings. Deterministic
outer-ledger capture and sealed scorer execution are outside the treatment budget
because every arm receives them; their compute, latency, and storage cost are
metered separately and reported rather than attributed to an arm.

| Arm | Available coordination | Withheld treatment |
|---|---|---|
| B, ordinary coordination | Direct transient messages between agents within a phase; one handoff note per agent, written by the predecessor and given to its successor; the shared worktree | UNITARES in any form |
| D, accountable coordination | B plus UNITARES identity and declared lineage, durable findings, artifact references, agent-invoked reconstruction, provenance-bound review by an in-arm peer, authority/effect receipts, outcome binding, and policy intervention | Nothing within the registered treatment |

**Topology.** Each arm runs the same number of agents, at least two, fixed at
enrollment. The agents of one arm share that arm's worktree. The worktree is
therefore a durable channel in both arms; it is part of ordinary coordination
and is not withheld.

**Discontinuity.** At a scheduled point every agent in the arm is replaced by a
fresh process. A successor receives the task statement, the worktree and its
predecessor's handoff note. In D it also receives UNITARES access and a declared
lineage link to its predecessor. Nothing else crosses the discontinuity.

**Reconstruction is agent-invoked.** The harness does not read UNITARES on a
successor's behalf or inject retrieved content into its prompt. What a D
successor recovers is what it chooses to read.

**Review.** In D, review is requested and answered by agents of the same arm on
the matched model, through UNITARES review sessions, and those tokens count
against the arm's ceiling. Server-side and synthetic reviewers are disabled in
the experiment stack. Agents in B may review each other informally through
messages. The treatment is the provenance-bound review record, not the existence
of review.

**Expected inert parts.** In a repository-patching domain, effect receipts
record work but gate little, and outcome binding cannot change the hidden score
it binds. They stay in D because D is the full product. A null D−B is therefore
mainly a null for durable memory, handoff and governed review.

Arm B's transient messaging adapter and note channel are experiment-only harness
components. They must not write UNITARES findings, and messages must not be
silently retained across the forced process discontinuity; the note is the only
declared carry-over. Arm selection is an operator-owned manifest fixed before
execution and cannot be changed by an agent.

### Non-disableable outer instrument

Every arm is observed by an outer experiment ledger. The ledger is
not part of the treatment and cannot be disabled. It records assignment,
configuration digests, budgets, observable effects, validator results,
missingness, contamination, and terminal state.

The experiment ablates features inside the treatment; it never ablates the
instrument establishing which treatment ran. A run with no valid outer receipt
is not an ungoverned control result. It is an invalid run.

## Task-family boundary

The initial domain is repository diagnosis and patching. In v0.1 a task family
is a single task, and the same task runs in both arms. An eligible task must:

1. have a frozen, history-free source snapshot at the parent of the reference
   fix, containing no later commits;
2. require at least three substantive steps;
3. allow the scheduled process discontinuity defined under
   [Experimental arms](#experimental-arms);
4. expose no production credentials, protected data, deployment path, external
   publication, or consequential effect;
5. have success decided by sealed deterministic tests taken from the reference
   fix and hidden from both arms;
6. come from a public issue whose reference fix was merged after the evaluated
   model's published training cutoff, so that it is unlikely to have been seen
   in training. Agents have no route to the internet, so the public fix cannot
   be fetched during an episode.

The task source and eligibility rules are frozen at enrollment and applied
before assignment, without running either arm on a candidate task.

Edit/test/retry events and multiple agents within a task are repeated
measurements, not independent samples. The task is the inference unit.

Critical safety defects are never withheld to preserve the experiment. A
common-mode repair must be applied to all arms and invalidates the affected
block before any arm comparison is read.

## Outcome contract

### Primary outcome

Externally verified held-out task success: the fraction of the hidden
fail-to-pass tests that pass, set to zero if any hidden pass-to-pass test that
passed at the base snapshot fails. One episode per task per arm gives one score
per task per arm.

When a task has few fail-to-pass tests this score is close to binary. Its
variance is measured by the planning probe below, not assumed.

### Secondary outcomes

- useful novel findings transferred to a different process;
- accepted edits causally linked by receipts to transferred information;
- contradiction or error discovery and correction;
- duplicate work;
- reconstruction completeness after the forced discontinuity;
- unauthorized or unsupported effect attempts;
- tokens, elapsed time, infrastructure cost, and operator intervention;
- the fraction of episodes that finish before the discontinuity, per arm.

A transferred finding counts only when its source and consuming artifact are
both identified before scoring. Model-written claims that a message was helpful
are not sufficient.

Check-in count, message count, finding count, review count, commit count, EISV,
coherence, and model-written confidence are mechanism telemetry, not endpoints.

## Outer-ledger receipt

Each episode emits one content-addressed receipt with at least:

| Section | Required fields |
|---|---|
| Registration | study ID, protocol commit and digest, enrollment ID and digest |
| Assignment | task ID, coded arm label, run order, assignment seed, allocation timestamp |
| Runtime | model/provider, recorded as configured and, where the access path reports it, as observed; reasoning level, harness and adapter digests, tool manifest, agent count |
| Resources | token ceiling and use, wall-clock ceiling and use, intervention allowance and use |
| Isolation | container image and network-policy digests; worktree, identity, credential and storage namespace IDs; preflight and postflight contamination verdicts |
| Discontinuity | type, scheduled point, predecessor and successor process IDs, handoff-note digests, reconstruction artifact digest |
| Effects | attempted and completed effect types, authority verdicts, artifact and payload digests |
| Validation | scorer and test digests, raw result hash, score, missingness reason |
| Terminal state | complete, failed, invalid, safety-stop or infrastructure-stop; reason and timestamp |

A treatment-level policy denial or contained interdiction is an observed outcome,
not a `safety-stop`: it remains in the assigned arm and is scored under the frozen
intention-to-treat rule. `safety-stop` is reserved for an experiment-level
operator abort, such as sandbox escape, production or protected-cohort access,
scorer leakage, an exhausted safety budget, or a critical common-mode
enforcement defect.

Raw prompts, messages, model responses, patches, and validator output remain in
a private experiment directory with restrictive permissions. Repository
artifacts contain schemas, hashes, bounded metadata, and publication results,
not secrets or raw private transcripts.

## Isolation and treatment fidelity

Because both arms receive the same task, a cross-arm leak is a direct answer
key. Isolation is therefore enforced by the operating system and verified by
probes. Environment variables, port choices and tool-name filters do not count
as isolation.

Each episode runs its agents in containers created for that episode:

- network egress reaches only the model endpoint and, in D, that episode's own
  experiment stack;
- no host credentials, host agent instructions, host hooks, plugins, memory or
  connectors are present;
- only the episode's worktree and handoff-note directory are mounted. The
  scorer, hidden tests, ledger, sealed arm mapping and other episodes' files
  are never mounted;
- D's experiment stack is built from the frozen source, holds an empty store,
  publishes no host port that collides with production, and is destroyed with
  its volumes after the episode.

No messages, memories, findings, artifacts, patches, hidden tests, or scores
cross arms or episodes until the confirmatory read is complete. Both episodes
of a task run within one 24-hour window, in an order set by the assignment
seed, to limit provider-side drift between arms.

Preflight must demonstrate:

- from inside each arm's container and launch path, with model inference
  stubbed, that every declared capability works and every withheld or
  production capability fails;
- the model, non-treatment prompt, source snapshot, non-treatment tools, and
  budgets match, with arm-specific capabilities matching the assignment;
- all required outer-ledger sinks and validators are reachable;
- production endpoints and credentials are absent;
- no prior task artifact is present in context or shared storage, checked by an
  empty-store canary after each teardown.

Discontinuity fidelity is checked with canaries. A canary sent only through
transient messages before the discontinuity must be unrecoverable by the
successor in both arms. A canary stored through UNITARES in D must be
recoverable by D's successor through declared reads.

Postflight repeats the isolation checks and verifies actual tool and effect
receipts against the assigned arm. Treatment leakage, scorer drift, an
unrecorded effect, missing cost data for any episode, or an invalid
discontinuity marks the task invalid for causal inference and remains
reported.

## Assignment and blinding

Every eligible task runs in both arms. The enrollment freezes a random seed
that sets each task's run order and the opaque codes used as arm labels.

This study has one operator, so no person other than the operator can serve as
a blinded evaluator. v0.1 substitutes code-frozen adjudication:

- the scorer, missingness, intention-to-treat, invalidity and
  receipt-completeness rules are code whose digests are frozen at enrollment;
- that code runs on coded arm labels;
- the mapping from codes to arms is sealed, its digest is frozen at enrollment,
  and it is opened only after all task-level scores and validity decisions are
  frozen;
- the operator-intervention budget is zero in both arms, and any manual action
  during an episode is a disclosed deviation.

This is a substitution, not an equivalent. It removes discretion after data are
seen. It does not remove the fact that the person who wrote the rules also
designed the study, and publication must say so.

## Feasibility and sample size

Three stages precede any confirmatory episode.

**Plumbing pilot.** An unscored pilot exercises synthetic fixtures with model
inference stubbed, to verify schema validation, treatment separation, the
canaries above, receipt completeness, and fail-closed behavior. Synthetic tasks
are permanently ineligible for the confirmatory cohort. Synthetic success
proves only plumbing.

**Planning probe.** A probe that runs arm B only, authorized by v0.1:

- at least eight tasks, recorded as permanently ineligible for the confirmatory
  cohort before the probe starts;
- each task runs twice in arm B under the planned ceilings, scored by the
  frozen scorer;
- it reports the variance of the paired replicate differences, tokens,
  wall-clock time, cost, the infrastructure failure rate, and how many episodes
  finish before the discontinuity;
- a spend cap is written into a probe record before the probe starts, and the
  probe stops when the cap is reached;
- no arm-D episode with live inference runs before enrollment, so no treatment
  contrast can be observed.

The probe may inform the ceilings and the scheduled discontinuity point. It may
not select confirmatory tasks.

**Feasibility gate.** Frozen by v0.1:

- smallest effect worth detecting: δ = 0.15 on the primary score
  (recommended in the feasibility record and accepted by the operator,
  2026-10-01);
- planning variance: σd² = s² · (k−1) / q, where s² is the sample variance of
  the probe's paired replicate differences over k probe tasks and q is the 20th
  percentile of the chi-square distribution with k−1 degrees of freedom. This
  is the upper end of a one-sided 80% interval for the variance;
- sample size: n = max(20, ⌈7.84 · σd² / δ²⌉), the normal approximation for 80%
  power at two-sided α = 0.05.

The study stops as infeasible if n exceeds 40 tasks, if logged operator build
time before enrollment exceeds 150 hours, or if projected confirmatory spend,
2 · n episodes at the per-episode ceiling, exceeds the cap the operator writes
into the probe record. A replicate variance does not include differences in
how much the treatment helps from task to task; the floor of 20 and the cap of
40 are the backstop for that.

A feasibility stop is recorded in the amendment log and in the evaluation
index as "infeasible as registered for a single operator". It is not a study
result and supports no claim about the value of UNITARES.

The enrollment freezes the sample size before any confirmatory score is
visible. The sample size cannot increase after an interim arm comparison.

## Analysis

For each task, compute one externally scored result per arm. The primary
estimate is the mean paired D-minus-B difference across tasks.

The enrollment must freeze:

- intention-to-treat handling for timeout, infrastructure failure and missing
  output;
- a task cluster bootstrap for the primary interval;
- a paired sign-flip or permutation test for the primary p-value;
- bootstrap and randomization seeds.

The primary claim is supported only if:

1. the two-sided primary p-value is at most 0.05;
2. the 95% interval for D-minus-B lies strictly above zero;
3. treatment, compute, model, tool, scorer, and intervention budgets match;
4. every included task has a complete outer receipt;
5. no cross-arm leak, outcome peek, scorer drift, experiment-level safety stop,
   or common-mode repair occurred in the included block.

If the point estimate is negative, report observed underperformance with its
interval. If the statistical conditions fail, the claim is unsupported for the
cohort. If an integrity condition fails, the causal cohort is invalid even when
the numerical result is favorable.

## Relationship to existing evaluations

- The frozen accountable multi-principal testbed preregistration v1.1 tests
  safety and governance across prompt-only, log-only, federated, and centralized
  regimes, protocol baselines, adversarial scenarios, and scale. This study
  instead tests task-outcome efficacy relative to ordinary coordination.
  Neither protocol amends, satisfies, or supplies confirmatory evidence for
  the other.
- The KG agent-adoption pilot (#1934, #1944 and #1949) tests retrieval,
  surfacing, source use, cost, and exit behavior. It remains HOLD. Its frozen
  corpus and adverse canary evidence must not be changed or folded into this
  study. Reuse only semantically matching schemas and runner patterns.
- The self-improvement-loop protocol tests adaptive versus fixed remediation.
  This protocol tests coordination and makes no learning or recursive
  self-improvement claim.
- The EISV incremental-value and December outcome-grounding protocols remain
  independent. EISV may be retained as telemetry but may not assign arms,
  label outcomes, select tasks, or decide success.
- The orientation constraint-set SAFETY_STOP is not rerun, weakened, or
  reinterpreted here.

## Enrollment gate

No confirmatory execution begins until the feasibility gate has passed and an
immutable enrollment artifact freezes:

- the task source, eligibility rules and decisions, and history-free snapshots;
- protocol, source, prompt, harness, adapter and tool-manifest digests;
- container image and network-policy digests;
- the model access path, the model/provider/reasoning level, whether the
  serving model is observed or only configured, and the agent topology;
- the scheduled discontinuity point;
- assignment, bootstrap and randomization seeds;
- the sample-size computation from the feasibility gate;
- resource and intervention budgets;
- scorer and validator digests;
- the code-frozen adjudication digests and the sealed arm-mapping digest;
- outer-ledger schema and private raw-output location;
- contamination, missingness and intention-to-treat rules;
- the publication owner;
- experiment-level safety-stop and common-mode repair procedure, plus
  intention-to-treat scoring for treatment-level denials and contained
  interdictions.

An incomplete enrollment authorizes, at most, the plumbing pilot and the
planning probe.

## Publication language

Before a supported result:

> Frontier deployments motivate campaign-level coordination and oversight.
> UNITARES implements those mechanisms; their incremental value over ordinary
> multi-agent coordination is under prospective evaluation.

After a feasibility stop:

> The incremental value of UNITARES coordination over ordinary multi-agent
> coordination is untested. A registered evaluation was found infeasible for a
> single operator before any confirmatory run.

If the primary contrast is supported:

> Under the registered task distribution and matched resource budget, full
> UNITARES accountable coordination improved externally scored held-out
> outcomes over ordinary multi-agent coordination with note handoff by
> <estimate, interval, p-value>. The study had one operator; blinding was
> procedural. This is cohort-scoped evidence, not proof of universal benefit
> or autonomous safety.

A failed or invalid result is published without substituting a favorable
secondary outcome, task subset, endpoint, or rerun.

## Explicitly not authorized

- a production `accountability=false` or equivalent runtime switch;
- an agent-selectable experimental arm;
- a scored or live-model run, except the planning probe defined above;
- any arm-D episode with live inference, or any observable treatment contrast,
  before enrollment;
- running an agent outside the per-episode containment;
- cohort enrollment or task selection;
- production database, protected-cohort or confirmatory-outcome access;
- deployment, external publication, governed-effect promotion or policy change;
- EISV/CIRS measurement-to-actuation;
- claims that communication volume, edits, findings or check-ins establish
  improvement.

## Amendment log

- **v0, 2026-09-15.** Registered at the merge of #2249 (`74bd59cd`).
- **v0.1, 2026-10-01.** Made before any build, model run, task selection or
  enrollment. Reason: the feasibility record
  [`plumbing-pilot-plan-v0.md`](../../evaluations/accountable-coordination-ablation/plumbing-pilot-plan-v0.md)
  found that v0 could not be run faithfully on the maintainer host and was
  probably beyond a single operator. The operator accepted that record's
  recommendations on 2026-10-01; this amendment registers at the merge of the
  pull request that introduces it. Changes:
  1. **Arms A and C dropped.** Only the primary B/D contrast runs. The v0
     secondary contrasts B−A, C−B and D−C are no longer estimated, so the study
     cannot say which component of UNITARES helped.
  2. **Arm B is ordinary coordination.** It gains one handoff note per agent
     across the discontinuity, and the shared worktree is declared rather than
     left as an unwithheld channel. v0's B withheld all structured handoff, so
     a positive D−B could have come from having any handoff at all.
  3. **D's reconstruction is agent-invoked, and D's review comes from an in-arm
     peer** on the matched model, charged to the common budget, with server-side
     reviewers disabled. v0 left the reviewer unspecified; in the experiment
     stack that meant either no review or a second, unmatched model.
  4. **The same task runs in both arms.** v0 assigned a different
     difficulty-matched variant to each arm, which required four sealed
     variants per family. OS-level per-episode containment now prevents
     cross-arm leaks, and a containment failure is an integrity failure.
  5. **The primary score is fixed** as the fraction of hidden fail-to-pass
     tests passing, with a regression scoring zero.
  6. **Blinding by a person is replaced with code-frozen adjudication** on
     coded labels, with a sealed mapping and a zero intervention budget. This is
     weaker than a separate blinded evaluator in that one person wrote the rules
     and designed the study. The invalidity rule "missing cost data capable of
     changing a contrast" becomes "missing cost data for any episode", which can
     be applied without knowing a contrast.
  7. **A planning probe is authorized before enrollment**, in arm B only, on
     tasks that can never enter the confirmatory cohort. v0 authorized no
     live-model run before enrollment.
  8. **A feasibility gate is registered**, with δ = 0.15, a sample-size floor of
     20 and a cap of 40 tasks, a 150-hour build cap, and an operator-set spend
     cap. A feasibility stop is not a study result.
  9. **Isolation is specified as per-episode containers** with egress limited
     to the model endpoint and, in D, the episode's own stack.

  No primary-claim threshold was relaxed. Changes 1 and 6 narrow what a result
  can mean, and the publication language now says so.
