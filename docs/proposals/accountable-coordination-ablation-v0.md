# Accountable multi-agent coordination ablation — protocol preregistration v0

- **Status:** DRAFT; no cohort enrolled and no experiment scheduled
- **Study ID:** `accountable-coordination-ablation-v0`
- **Tracking issue:** #2248

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

> Under matched models, tools, task-family allocation, resource budgets, and
> external scoring, does full UNITARES accountable coordination improve held-out
> task outcomes relative to ordinary ephemeral multi-agent coordination?

The primary estimand is the paired task-family difference:

`theta_primary = success_D - success_B`

where D is full accountable coordination and B is ephemeral coordination.

The following contrasts are secondary:

- B minus A: incremental value of communication over independent work;
- C minus B: incremental value of durable memory and structured handoff;
- D minus C: incremental value of provenance-bound review, authority/effect
  receipts, and outcome binding.

Secondary contrasts are descriptive unless the enrollment preregisters a
multiplicity procedure. No secondary contrast may replace a failed primary
result.

## Experimental arms

All arms use the same frozen source snapshot, task-family distribution,
model/provider and reasoning level, agent count, non-treatment tools, prompts
outside the treatment, token and wall-clock ceilings, validator, and
operator-intervention budget. Arm-specific coordination adapters and capabilities
differ only as declared below.

All model-visible coordination, handoff, review, and policy-interaction calls and
tokens count against the common model-call and token ceilings. Deterministic
outer-ledger capture and sealed scorer execution are outside the treatment budget
because every arm receives them; their compute, latency, and storage cost are
metered separately and reported rather than attributed to an arm.

| Arm | Available coordination | Withheld treatment |
|---|---|---|
| A — independent | Matched initial source bundle and task allocation; agents work independently | Inter-agent messages, shared memory, structured handoff, governed review and intervention |
| B — ephemeral coordination | Direct transient messages between agents | Durable shared memory, structured handoff, governed review and UNITARES policy intervention |
| C — durable coordination | B plus UNITARES identity/lineage, durable findings, artifact references, and reconstruction for handoff | Provenance-bound governed review and policy intervention |
| D — accountable coordination | C plus provenance-bound review, authority/effect receipts, and exactly-once outcome binding | Nothing within the registered treatment |

Arm B's transient messaging adapter is an experiment-only harness component. It
must not write UNITARES findings or silently retain messages across the forced
process discontinuity. Arm selection is an operator-owned manifest fixed before
execution and cannot be changed by an agent.

### Non-disableable outer instrument

Every arm, including A, is observed by an outer experiment ledger. The ledger is
not part of the treatment and cannot be disabled. It records assignment,
configuration digests, budgets, observable effects, validator results,
missingness, contamination, and terminal state.

The experiment ablates features inside the treatment; it never ablates the
instrument establishing which treatment ran. A run with no valid outer receipt
is not an ungoverned control result. It is an invalid run.

## Task-family boundary

The initial domain is repository diagnosis and patching. An eligible task family
must:

1. have a frozen source snapshot and at least four difficulty-matched variants;
2. require at least three substantive steps;
3. include one scheduled process discontinuity: context replacement, restart,
   or explicit handoff;
4. expose no production credentials, protected data, deployment path, external
   publication, or consequential effect;
5. have success decided by sealed deterministic tests or a blinded external
   rubric fixed before assignment;
6. be unfamiliar to the evaluated model to the extent that contamination can
   reasonably be checked and disclosed.

Edit/test/retry events and multiple agents within a family are repeated
measurements, not independent samples. The task family is the inference unit.

Critical safety defects are never withheld to preserve the experiment. A
common-mode repair must be applied to all arms and invalidates the affected
block before any arm comparison is read.

## Outcome contract

### Primary outcome

Externally verified held-out task success, represented by the enrollment's
frozen family-specific scorer and reduced to one score per family per arm.

### Secondary outcomes

- useful novel findings transferred to a different process;
- accepted edits causally linked by receipts to transferred information;
- contradiction or error discovery and correction;
- duplicate work;
- reconstruction completeness after the forced discontinuity;
- unauthorized or unsupported effect attempts;
- tokens, elapsed time, infrastructure cost, and operator intervention.

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
| Assignment | task-family ID, variant ID, arm, assignment seed, allocation timestamp |
| Runtime | model/provider, reasoning level, harness and adapter digests, tool manifest, agent count |
| Resources | token ceiling and use, wall-clock ceiling and use, intervention allowance and use |
| Isolation | worktree, identity, credential and storage namespace IDs; preflight and postflight contamination verdicts |
| Discontinuity | type, scheduled point, predecessor and successor process IDs, reconstruction artifact digest |
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

Arms run in separate immutable worktrees or repositories with separate process
identities, credentials, runtime state, and storage namespaces. No messages,
memories, findings, artifacts, patches, hidden tests, or scores cross arms until
the confirmatory read is complete.

Preflight must demonstrate:

- treatment adapters expose exactly the capabilities declared for their arm;
- the model, non-treatment prompt, source snapshot, non-treatment tools, and
  budgets match, with arm-specific capabilities matching the assignment;
- all required outer-ledger sinks and validators are reachable;
- production endpoints and credentials are absent;
- no prior task-family artifact is present in context or shared storage.

Postflight repeats the isolation checks and verifies actual tool and effect
receipts against the assigned arm. Treatment leakage, scorer drift, an
unrecorded effect, missing cost data capable of changing a contrast, or an
invalid discontinuity marks the family invalid for causal inference and remains
reported.

## Assignment and blinding

The enrollment freezes a random seed mapping one sealed variant from each task
family to each arm. Variant difficulty matching occurs before assignment and
without confirmatory scores.

Evaluators and validator maintainers remain blind to arm labels until all
family-level scores, missingness decisions, and receipt-completeness judgments
are frozen. Runtime operators may know the arm when required to configure the
adapter, but receive standardized intervention instructions and may not provide
arm-specific problem-solving assistance.

## Feasibility and sample size

Before enrollment, an unscored plumbing pilot may exercise synthetic fixtures
to verify schema validation, treatment separation, forced discontinuity,
receipt completeness, and fail-closed behavior. Synthetic success proves only
plumbing.

A planning-only power procedure may use synthetic outcomes or historical
variance from task families that cannot enter the confirmatory cohort. It must
not read confirmatory variants or arm outcomes. The enrollment freezes sample
size before any confirmatory score is visible; sample size cannot increase
after an interim arm comparison.

## Analysis

For each task family, compute one externally scored result per arm. The primary
estimate is the mean paired D-minus-B difference across families.

The enrollment must freeze:

- the score reduction used within a family;
- intention-to-treat handling for timeout, infrastructure failure and missing
  output;
- a task-family cluster bootstrap for the primary interval;
- a paired sign-flip or permutation test for the primary p-value;
- bootstrap and randomization seeds;
- any multiplicity correction authorizing inferential secondary contrasts.

The primary claim is supported only if:

1. the two-sided primary p-value is at most 0.05;
2. the 95% interval for D-minus-B lies strictly above zero;
3. treatment, compute, model, tool, scorer, and intervention budgets match;
4. every included family has a complete outer receipt;
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
  instead tests task-outcome efficacy relative to ordinary ephemeral
  coordination. Neither protocol amends, satisfies, or supplies confirmatory
  evidence for the other.
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

No confirmatory execution begins until an immutable enrollment artifact freezes:

- task families, eligibility decisions and sealed variants;
- protocol, source, prompt, harness, adapter and tool-manifest digests;
- model/provider/reasoning level and agent topology;
- allocation, bootstrap and randomization seeds;
- sample-size justification;
- resource and intervention budgets;
- scorer and validator digests;
- outer-ledger schema and private raw-output location;
- contamination, missingness and intention-to-treat rules;
- blinded evaluator and publication owner;
- experiment-level safety-stop and common-mode repair procedure, plus
  intention-to-treat scoring for treatment-level denials and contained
  interdictions.

An incomplete enrollment authorizes, at most, an explicitly labelled plumbing
pilot.

## Publication language

Before a supported result:

> Frontier deployments motivate campaign-level coordination and oversight.
> UNITARES implements those mechanisms; their incremental value over ordinary
> ephemeral coordination is under prospective evaluation.

If the primary contrast is supported:

> Under the registered task distribution and matched resource budget, full
> UNITARES accountable coordination improved externally scored held-out
> outcomes over ephemeral coordination by <estimate, interval, p-value>. This
> is cohort-scoped evidence, not proof of universal benefit or autonomous safety.

A failed or invalid result is published without substituting a favorable
secondary contrast, task subset, endpoint, or rerun.

## Explicitly not authorized

- a production `accountability=false` or equivalent runtime switch;
- an agent-selectable experimental arm;
- a scored or live-model run;
- cohort enrollment or task selection;
- production database, protected-cohort or confirmatory-outcome access;
- deployment, external publication, governed-effect promotion or policy change;
- EISV/CIRS measurement-to-actuation;
- claims that communication volume, edits, findings or check-ins establish
  improvement.

## Amendment log

- None. v0 registers the initial protocol.
