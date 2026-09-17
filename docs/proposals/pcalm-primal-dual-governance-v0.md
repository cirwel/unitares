# PC-ALM as a lens for primal-dual governance

Status: proposed research design, 2026-09-15
Scope: a non-authoritative, replay-first test of persistent constraint pressure
in UNITARES. This document authorizes no live policy or EISV change.

## Decision in one sentence

Do not transplant PC-ALM into UNITARES. Test one narrower idea from it: whether
a leaky, non-negative dual accumulator over explicitly named governance
constraint residuals adds stable and outcome-relevant information beyond the
existing behavioral state, risk, and oscillation instruments.

The first implementation, if separately approved, is an offline replay. Any
prospective instrument remains shadow-only. It must not change `action`,
`sub_action`, risk, EISV, basin, margin, CIRS, or agent-facing guidance.

## Why this question exists

[Augmented Lagrangian Predictive Coding (PC-ALM)](https://pub.sakana.ai/pc-alm/)
uses a dual variable at each layer to accumulate local constraint error. In the
linear case, those dual variables converge to the backpropagation credit signal
while each layer communicates only with its neighbours. The useful design idea
is not “governance without backpropagation”. UNITARES does not train a neural
network. It is that a persistent local residual can carry information about a
global constraint which an instantaneous error loses.

UNITARES already has three superficially similar mechanisms:

1. behavioral EISV uses EMAs and Welford baselines;
2. the legacy ODE has a damped E-I accumulator, `V`;
3. `src/monitor_lambda.py` uses a PI controller to adapt the ODE drift coupling
   named `lambda1`.

Those are not PC-ALM dual variables. The name collision is especially risky:
UNITARES `lambda1` is a bounded gain multiplying ethical-drift magnitude in the
legacy ODE's S dynamics. PC-ALM's lambda is a Lagrange multiplier associated
with a layer constraint. Likewise, UNITARES “dual-log architecture” means two
evidence streams, not dual optimization.

The comparison is still worth making because the current instruments answer
different questions:

- EMA asks whether a measurement has recently moved.
- Welford deviation asks whether it differs from the agent's established norm.
- CIRS asks whether verdict dynamics are oscillating.
- A dual accumulator would ask whether one declared constraint has remained
  unsatisfied, by how much, and for how long.

That last quantity may be useful. It may also be a renamed moving average. The
experiment must be able to return that negative answer.

## Ontology and authority boundary

Let `x_t` be the measurements available after check-in `t`. An operator-declared
constraint is a named function

```text
g_k(x_t) <= 0
```

with provenance identifying its inputs, units, reference, and owner. Its
one-sided violation is

```text
r_k,t = max(0, g_k(x_t))
```

The candidate shadow state follows the exact solution of a leaky accumulator
under a piecewise-constant residual:

```text
decay = exp(-leak_k * dt)
drive = alpha_k * r_k,t * (1 - decay) / leak_k
z_k,t+1 = clamp(decay * z_k,t + drive, 0, z_max,k)
```

For `leak_k = 0`, the continuous limit is
`z_k,t+1 = clamp(z_k,t + alpha_k * dt * r_k,t, 0, z_max,k)`.
The exact interval update avoids making pressure depend on how an equivalent
wall-clock interval is partitioned into check-ins.

`z` is called **constraint pressure**, not coherence, integrity, risk, verdict,
or a validated Lagrange multiplier. In an offline or unsurfaced shadow there is
no responding primal system, so `z` is only an observer state. Calling it a
dual variable becomes justified only after an explicit action variable responds
to it and a closed-loop experiment establishes that interpretation.

The live authority boundary remains unchanged:

- behavioral EISV is proprioceptive measurement;
- behavioral assessment and its documented backstops determine policy;
- legacy ODE quantities remain diagnostic/compatibility telemetry according to
  their existing provenance;
- deviation is information, not guilt;
- only the existing policy stack may issue `proceed` or `pause`.

Constraint pressure must never be folded into E, I, S, V, risk, confidence,
coherence, or an existing evidence label during this experiment. Doing so would
create the outcome it is supposed to predict and make the evaluation recursive.

## What may become a constraint

A quantity is eligible only when all of these are recorded before replay:

1. **Named invariant.** The constraint states what must remain bounded and why.
2. **Decision ownership.** The operator, an existing contract, or a prior
   recorded decision owns the reference value. The experiment does not invent
   a threshold after seeing data.
3. **Provenance.** Every input has a source and an authority role. Legacy
   `C(V_ODE)` is ineligible as behavioral health evidence.
4. **Cadence semantics.** `dt`, missing observations, duplicate check-ins, and
   restarts have defined behaviour.
5. **Release semantics.** Leak or explicit resolution allows pressure to fall;
   old failures cannot become permanent punishment.
6. **No self-label.** The residual is not also used to create the outcome label
   against which it is evaluated.

Plausible first candidates are persistent distance above an already-owned risk
boundary, persistent distance beyond a behavioral-baseline deviation budget,
or unresolved evidence debt with an independently observable closure event.
These are examples, not selected constraints. The first two risk duplicating
existing risk and CIRS; the third has better conceptual separation but requires
a reliable evidence-debt producer.

No constraint may be selected merely because it produces an attractive trace.

## The experiment

### Phase 0 — inventory, no scoring

Produce a table of candidate constraints with their owner, formula, data
availability, cadence, missingness, expected response, and possible outcome
leakage. Reject any candidate whose reference is unowned or whose producer
cannot distinguish “not observed” from zero.

This phase reads existing data and writes no new governance record.

Initial source inventory, 2026-09-15:

| Candidate residual | Existing owner/source | Availability and release | Initial disposition |
|---|---|---|---|
| distance above a configured risk boundary | `GovernanceConfig` thresholds and resolved risk | available per decision, but risk provenance changes across authority stages; releases below the boundary | mechanically replayable, scientifically weak because it likely duplicates risk and CIRS |
| distance beyond the existing 1.5σ behavioral deviation budget | `src/behavioral_assessment.py` over Welford baselines | available only once the agent is baselined; releases on return to the declared band | best existing mechanics-only candidate, but likely redundant with behavioral risk |
| unresolved claim/evidence debt | corroboration fields such as `claim_only` and `evidence_weight` | observations exist, but no single durable claim identifier plus independently observed closure transition was found | best conceptual candidate; ineligible until the lifecycle producer exists |
| verdict flips or CIRS resonance | verdict history and CIRS | available, but already accumulated by CIRS and may feed server-authored outcome events | reject: duplicate and recursive |

This inventory selects no outcome-bearing constraint. Synthetic residuals are
sufficient to validate the state transition. An evidence-debt replay must wait
for an explicit claim lifecycle contract; absence of that producer is not zero
debt.

### Phase 1 — deterministic offline replay

Implement a pure replay over frozen check-in sequences. The replay accepts
timestamped residuals and a parameter manifest and returns timestamped pressure.
It does not import the MCP server, write PostgreSQL/Redis, or call a model.

Required invariants:

- identical inputs and manifest produce byte-stable output;
- checkpoint and restore equal uninterrupted replay;
- splitting one interval into equivalent sub-intervals changes results only
  within a declared numerical tolerance;
- missing data is represented as missing, not zero violation;
- non-negative pressure cannot cross below zero;
- bounded inputs cannot produce NaN, infinity, or unbounded windup;
- `alpha=0` produces decay-only state;
- `leak=0` produces monotone accumulation until the explicit cap;
- shuffled agent identities do not change per-trajectory results.

Compare constraint pressure with the inputs already available to policy:
behavioral risk, EISV, baseline deviations, verdict history, and CIRS features.
High correlation is not automatically failure, but a candidate that is an
invertible or near-deterministic transform of an existing instrument has added
no new measurement.

### Phase 2 — preregistered retrospective evaluation

Before reading outcome discrimination, record:

- the frozen cohort and cutoff;
- trusted outcome predicate and fixture exclusions;
- candidate constraints and parameter grid;
- baseline models;
- dependence-aware resampling unit;
- missing-data rules;
- smallest operationally relevant improvement;
- multiplicity correction;
- stop rule.

The primary comparison is incremental predictive value over the existing
behavioral/risk baseline on independently verified outcomes. Report calibration
and proper scoring rules, not only the best AUC from a parameter sweep. Preserve
all unsuccessful candidates in the result.

This phase cannot establish governance benefit. It tests whether the proposed
state contains non-redundant temporal information.

### Phase 3 — prospective shadow, only after Phase 2

If the registered retrospective result clears its predeclared bar, a separate
change may add a default-off prospective shadow. It computes after the live
decision has been fixed and emits a versioned audit record such as:

```json
{
  "event_type": "constraint_pressure_shadow.v0",
  "applied": false,
  "constraint_id": "operator-owned-versioned-id",
  "residual": 0.0,
  "pressure": 0.0,
  "alpha": 0.0,
  "leak": 0.0,
  "dt": 0.0,
  "input_provenance": {},
  "missing_inputs": [],
  "manifest_digest": "..."
}
```

The event must say `applied: false`. The shadow must run on a copy or pure input
record and must have a test proving that enabling it causes zero differences in
the live response apart from the new audit event.

### Phase 4 — closed-loop study, new decision required

Only a surfaced intervention can test the PC-ALM-like claim that accumulated
constraint error improves distributed coordination. That requires a distinct
protocol: what an agent sees, which action it may change, random or otherwise
identified exposure, recovery behaviour, and independently adjudicated
outcomes. Shadow traces alone cannot identify closed-loop gain or benefit.

No result from Phases 0–3 authorizes Phase 4 or a policy threshold.

## PC-ALM correspondence: exact and inexact parts

| PC-ALM | Candidate UNITARES experiment | Status of analogy |
|---|---|---|
| layer constraint residual | named governance constraint residual | structural analogy |
| Lagrange multiplier | leaky constraint pressure | observer only until closed loop |
| neighbouring-layer messages | locally available agent/evidence signals | weaker; locality topology is not yet specified |
| global differentiable loss | operator-owned governance invariants | not equivalent |
| primal activation dynamics | agent action/behaviour | absent from shadow phases |
| equilibrium recovers BP credit in linear networks | no corresponding exactness claim | deliberately none |

UNITARES is non-stationary, asynchronous, partly observed, and includes human
authority. It has neither a single differentiable global loss nor a linear
network equilibrium. PC-ALM's exact result therefore does not transfer.

## Existing mechanisms this must not silently replace

### Behavioral state

`src/behavioral_state.py` computes EMA-smoothed EISV and Welford self-baselines.
Those remain the primary proprioceptive path. Constraint pressure is not a fifth
EISV coordinate during this experiment.

### Legacy ODE `V`

`governance_core/dynamics.py` defines ODE `V` as a damped E-I imbalance
accumulator. It is a research/diagnostic state, distinct from behavioral V and
from a per-constraint pressure. Reusing it would collapse different units and
destroy constraint identity.

### Existing `lambda1` PI controller

`src/monitor_lambda.py` accumulates void-frequency error in `pi_integral` and
adapts the ODE gain `lambda1`. It is the closest existing control pattern, but
its target and actuator are legacy-path concepts. It should be a comparison
baseline, not the insertion point and not the state container for this work.

### CIRS

CIRS already detects flips and oscillatory/resonant behaviour. A pressure
instrument that merely tracks the same verdict sequence is redundant and risks
forming a feedback loop. CIRS features must be included in the baseline
comparison, and no prospective pressure event may be ingested as a bad outcome.

## Failure modes to design for

- **Windup:** pressure remains high after the underlying issue is resolved.
- **Cadence capture:** frequently checking agents accumulate more pressure for
  identical wall-clock behaviour.
- **Missing-as-healthy:** absent evidence becomes a zero residual.
- **Double counting:** risk contributes to pressure and pressure is then folded
  back into risk.
- **Self-fulfilling labels:** a controller event is persisted as a bad outcome
  and later “predicts” itself.
- **Oscillation:** surfaced pressure causes alternating over-correction.
- **Goodhart pressure:** agents optimize the measured constraint while degrading
  an unmeasured objective.
- **Permanent moralization:** a historical accumulator is read as character or
  guilt rather than recoverable system state.
- **Federation fiction:** central computation is described as local merely
  because each record belongs to an agent.
- **Parameter fishing:** alpha, leak, and caps are selected on the evaluation
  outcomes and reported as if preregistered.

Anti-windup, leak, provenance, restart equivalence, negative controls, and CIRS
comparison are requirements, not later hardening.

## Stop conditions

Stop after Phase 1 if no candidate constraint has an owned reference and an
independent resolution/outcome path, or if pressure is numerically dominated by
check-in cadence.

Stop after Phase 2 if the preregistered result does not clear the operator-owned
minimum improvement, if the apparent gain disappears against dependence-aware
controls, or if pressure is effectively a transform of an existing instrument.
Bank that result as evidence about this mechanism; do not retire the broader
capability of persistent constraint reasoning.

Stop a prospective shadow immediately if it changes a live response, enters an
EISV/outcome producer, loses provenance, cannot restore state exactly, or
creates material request-path latency.

## Smallest honest implementation seam

If Phase 0 produces an eligible constraint, the smallest first artifact is:

```text
src/constraint_pressure.py               pure state transition, no I/O
scripts/analysis/replay_constraint_pressure.py
tests/test_constraint_pressure.py        numerical and restart invariants
```

The pure state transition and synthetic invariant tests landed with this design;
the replay script remains blocked on selection of an eligible real constraint.
The primitive is intentionally not wired into `UNITARESMonitor`. A later
prospective shadow would call the same pure transition only after the decision
is complete, behind a default-off flag, and persist a versioned
non-authoritative audit event.

## Open decisions

The operator must choose or delegate, before outcome-bearing work:

1. the first constraint and who owns its reference;
2. whether wall-clock or observation-count dynamics match its semantics;
3. the smallest operationally relevant incremental improvement;
4. the trusted outcome class and dependence unit;
5. acceptable state-retention and privacy boundaries;
6. whether a successful observer study warrants a closed-loop experiment.

Until then, the recommendation is **inventory and replay design only**.
