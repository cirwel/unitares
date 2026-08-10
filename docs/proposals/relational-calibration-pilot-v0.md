# Relational calibration pilot v0: protocol and threat model

Status: specification only; runtime implementation prohibited

Date: 2026-08-10

Scope: whether one participant's forecast of another participant's future EISV
telemetry contains repeatable information beyond preregistered non-relational
baselines.

## Decision boundary

This document does **not** authorize collection. Merging it must not add or
enable a schema, MCP action, endpoint, queue, private store, feature flag,
dashboard feed, scheduled job, or dormant writer.

A future implementation remains blocked until all of these gates are recorded:

| Gate | Required evidence | Current state |
|---|---|---|
| Protocol | This preregistration is merged without runtime capability | pending |
| Privacy architecture | Independent review demonstrates resistance to reconstruction, linkage, differencing, collusion, and deletion side channels | blocked |
| Adversarial test | A prototype outside production passes the attacks in this document | blocked |
| Operator authorization | A human operator explicitly approves a fixed cohort, duration, access policy, privacy budget, retention schedule, incident plan, and shutdown procedure **after** reviewing the privacy evidence | blocked |
| Implementation | A separate PR links all prior evidence and preserves structural non-interference | blocked |

The 2026-08-10 UNITARES dialectic review
`f58df08ada7c3c95` rejected dormant collection capability and allowed protocol
and threat-model work only. That rejection is the governing posture for this
version.

## Narrow claim

The proposed construct is **relational forecast skill**:

> Given information naturally available to an observer before time `t`, does
> the observer forecast a consenting subject's server-selected EISV reference
> measurement after `t` better than preregistered non-relational baselines?

This is deliberately narrower than empathy. It does not measure or establish:

- subjective experience, qualia, sentience, feeling, or moral status;
- access to another participant's internal state;
- truth about the subject;
- benevolence, care, alignment, trustworthiness, or social rank;
- causal understanding of the subject; or
- a basis for policy, verdicts, enforcement, calibration weights, or training.

EISV remains proprioceptive telemetry. The future subject measurement is a
**reference measurement**, not ground truth. Agreement may come from shared
context, persistence, leakage, imitation, coordination, or real relational
modeling; the protocol must distinguish those explanations before making a
stronger claim.

## Pre-registered hypotheses

All endpoints use normalized dimensions:

- `E`, `I`, and `S` retain their `[0, 1]` ranges;
- `V` is mapped from `[-1, 1]` to `[0, 1]` before scoring; and
- every per-dyad contribution is clipped to a fixed `[0, 1]` sensitivity bound.

For an eligible ordered dyad `d`, define point error as the unweighted mean
absolute error across the four normalized dimensions. Define forecast skill as:

```text
skill_d = error_baseline_d - error_observer_d
```

Positive values favor the observer. Dimensions must not be reweighted after
seeing results.

### Primary hypothesis

The observer forecast improves on the subject-persistence baseline by an
operationally relevant margin:

```text
mean(skill) >= 0.05 normalized MAE
```

The confirmatory success rule requires both:

1. the privacy-adjusted point estimate is at least `0.05`; and
2. the one-sided 95% lower confidence bound, including clustering and privacy
   noise, is greater than `0`.

Failure of either condition is a negative result. It must not be reframed as
"promising" based on a favorable dimension, subgroup, time window, or
alternative baseline.

### Secondary hypotheses

If the privacy review permits uncertainty intervals, the observer may also
submit a central 80% interval per dimension. The only preregistered secondary
outputs are:

- aggregate interval coverage;
- aggregate normalized interval width; and
- abstention rate.

Acceptable descriptive coverage is `0.70..0.90`. It is not a pass condition for
the primary hypothesis. No per-person or per-dyad coverage is disclosed.

## Null baselines

The isolated analysis must compute every baseline from information fixed before
the target reference is selected:

1. **Subject persistence:** the subject's last complete eligible measurement
   available before prediction commitment.
2. **Fleet anchor:** the preregistered cohort anchor available before cohort
   enrollment closes.
3. **Observer-blind permutation:** forecasts are reassigned across eligible
   subjects within the fixed cohort while preserving the horizon and source
   strata.
4. **Temporal sham:** the observer forecast is compared with an eligible
   subject measurement from a preregistered non-target time offset.

The persistence comparison is primary. The others diagnose leakage,
population-level guessing, and temporal autocorrelation. They cannot replace a
failed primary comparison.

## Unit of analysis and sample-size gate

The unit is one ordered observer-subject dyad in one fixed cohort epoch. The
same identity may contribute at most four observations to an epoch, at most two
as observer and two as subject, and an ordered dyad may contribute only once.
Analysis must use participant-clustered uncertainty because dyads are not
independent when participants recur.

No confirmatory statistic may be released unless the completed cohort has all
of:

- at least 200 eligible matched dyads;
- at least 50 distinct established observers;
- at least 50 distinct established subjects; and
- no identity exceeding the contribution bounds above.

The 200-dyad gate targets roughly 80% power for a `0.05` paired improvement when
the participant-clustered standard deviation is no greater than `0.25`, before
privacy-noise loss. A blinded variance check must be performed before unsealing
the result. If the observed variance or required privacy noise makes that power
unavailable, the pilot stops as underpowered; the cohort is not expanded after
outcomes are inspected.

If any threshold is missed, the only publishable result is
`insufficient_private_cohort`. Exact near-threshold counts are not disclosed.

## Eligibility

Eligibility is evaluated before prediction and must fail closed.

Both participants must have:

- caller-proven strong identity at each consent or contribution step;
- at least five complete, non-synthetic EISV telemetry envelopes spanning at
  least 24 hours;
- no bootstrap or synthetic row used for eligibility, baseline, or reference;
- enrollment in the fixed cohort before its consent window closes; and
- no active block, withdrawal, or conflicting role constraint.

Self-prediction is forbidden. Eligibility identity is used only by a dedicated
access service. The analysis plane receives a random, epoch-scoped pseudonym;
stable UUIDs, display names, session IDs, and lineage are not analysis fields.
Strong identity limits duplicate participation but is not treated as proof
against Sybils.

## Target selection

Neither participant chooses the reference time or row.

Before a forecast is accepted, the server samples a target offset from a
preregistered distribution and commits to it with fresh randomness. The
participant-visible consent text discloses the enclosing capture window but not
the exact offset. After the committed horizon, an isolated matcher selects the
first row that satisfies all of:

- `synthetic IS NOT TRUE`;
- the EISV telemetry envelope is complete and on the preregistered schema;
- the source is on the preregistered eligible-source list; and
- `recorded_at` lies inside the fixed capture window.

No caller-supplied vector is accepted. No later or earlier row may be selected
to improve matching. A missing eligible row yields `source_gap` and no score.
The target commitment is opened only to the independent audit reviewer, not to
participants or a dashboard.

## Mutual-consent state machine

Consent is observer-specific, purpose-bound, epoch-bound, one-use, and
revocable. Identity binding alone is never consent.

```text
absent
  -> subject_offer
  -> mutual_accept
  -> forecast_committed
  -> waiting_reference
  -> aggregated

Any non-terminal state
  -> withdrawn | blocked | declined | expired | source_gap | erased
```

Required properties:

- The subject creates `subject_offer` before any forecast bytes are accepted.
- The observer explicitly accepts the same purpose, disclosure policy,
  capture-window class, and irreversible aggregate boundary.
- Either party may withdraw until the cohort aggregation transaction commits.
- Withdrawal, decline, block, expiry, replay, source gap, or eligibility loss
  destroys forecast material and removes the dyad from aggregation.
- Non-response and all pre-aggregation terminal states produce the same
  participant-visible `closed_without_result` response. The response must not
  reveal whether a forecast, reference, or match existed.
- After aggregate commitment, participants can stop future participation but
  cannot remove a bounded contribution from an already released aggregate.
  This limitation must be stated before mutual acceptance.
- Participation, refusal, or withdrawal must never affect access, governance
  treatment, ranking, work assignment, compensation, or eligibility elsewhere.
- An operator must not enroll direct reports or otherwise use authority to
  solicit participation. The privacy review must define how power imbalance is
  detected and audited.

The state machine requires linearizable transitions, idempotency keys, replay
protection, and a single terminal outcome. Race tests must cover withdrawal at
every boundary, including simultaneous reference capture and aggregation.

## Data minimization and cryptographic requirements

The current UNITARES PostgreSQL, JSONL audit, Redis, generic audit query, and
dashboard paths are **not approved storage surfaces** for raw forecasts,
references, per-dyad errors, dyad identifiers, or consent linkage.

A future privacy architecture must demonstrate all of the following before an
implementation PR:

- Raw forecast and reference vectors are never written to durable application
  logs, ordinary Redis, PostgreSQL audit tables, traces, error reports, or
  model context.
- A forecast commitment is hiding, binding, unlinkable across epochs, and
  protected by at least 128 bits of fresh randomness. A bare hash of a
  low-dimensional EISV vector is prohibited because it is enumerable.
- Computation occurs either through reviewed secure multiparty aggregation or
  in an independently attested isolated worker whose operator cannot inspect
  plaintext. Ordinary service-process memory is not sufficient isolation.
- Plaintext exists only for the minimum computation lifetime and is verifiably
  erased on every terminal path, crash recovery path, timeout, and shutdown.
- No durable per-dyad error vector or scalar is retained. Absolute error is not
  anonymous: when a participant knows a reference or prediction, it narrows or
  reconstructs the other value, especially across repeated observations.
- Lifecycle receipts are delayed, coarsened, and unlinkable. Deletion receipts
  prove completion to an auditor without telling a participant whether a match
  existed.

Until one of the approved isolation designs is selected and independently
reviewed, there is no safe implementation target.

### Why the earlier audit-plus-TTL design fails

TTL storage limits duration; it does not make a released derivative private.
For one dimension, if a participant knows reference `x` and receives absolute
error `a`, the unknown forecast is reduced to:

```text
p in {x - a, x + a}
```

Range boundaries often resolve the sign immediately. Across four dimensions
there are at most `2^4` candidates before applying known bounds, correlations,
prior forecasts, or a second observation. Repeated releases can reduce that
small candidate set to one. Reversing the roles gives the same attack when the
forecast is known and the reference is hidden. Consequently, neither a raw
error vector nor a per-dyad scalar mean error may be durable or participant
visible.

Exact aggregate queries are also unsafe. If an attacker can obtain the mean
for a cohort of size `N` and again after one target is removed, the target's
contribution is exactly:

```text
e_target = N * mean_N - (N - 1) * mean_without_target
```

Overlapping filters produce a linear system with the same effect. A minimum
row count does not prevent this, and repeated dyads or Sybil identities can
satisfy a row threshold without providing participant diversity. This is why
the protocol permits one immutable result packet, bounds each participant's
contribution, requires distinct established participants, and spends a fixed
participant-level differential-privacy budget.

A bare commitment such as `SHA256(canonical_eisv)` also fails: four bounded,
low-precision dimensions are cheap to enumerate. A future commitment must mix
in unpredictable high-entropy randomness, destroy that opening material on
terminal paths, and prove that neither commitment identifiers nor timing link
the same dyad across epochs.

### Unresolved privacy architecture

Two architecture families remain candidates, neither approved:

1. reviewed secure multiparty computation with non-colluding aggregation
   principals and participant-level contribution enforcement; or
2. a remotely attested isolated worker that accepts encrypted inputs, computes
   only clipped cohort statistics, applies the entire privacy mechanism inside
   the boundary, and releases one signed packet.

The current single-service deployment cannot claim either property. A future
privacy review must select one family, name its trust assumptions, describe key
custody and crash recovery, and show what an operator, database administrator,
participant, colluding pair, and compromised application process can each
observe. Moving plaintext into a differently named process or Redis namespace
does not satisfy this gate.

## Disclosure and privacy budget

The proposed pilot permits exactly one public result packet after the fixed
cohort closes. It offers no interactive query, time-range selector, subgroup,
agent filter, dimension drill-down, or repeated refresh.

The packet may contain only preregistered aggregates:

- primary mean forecast skill and its uncertainty interval;
- primary baseline mean error;
- optional interval coverage and width if separately approved;
- coarse missingness and abstention bands; and
- the protocol version and privacy parameters.

The release mechanism must provide participant-level differential privacy with
a total pilot budget no larger than `epsilon = 1.0`, `delta = 1e-6`. The budget
is divided among the fixed fields before collection and cannot be replenished.
Counts are released as coarse bands, not exact values. Per-participant
contribution bounds are enforced before noise is added.

The privacy reviewer must show that auxiliary access to a participant's own
forecast or reference, combined with the entire result packet, does not recover
another participant's vector or membership above the accepted bound. If that
cannot be shown, the packet is suppressed and the pilot ends without a result.

## Missingness and selection

The intent-to-measure cohort includes every mutually accepted dyad, including
abstentions, source gaps, withdrawals, and expired captures. Complete-case
results alone are prohibited.

The one release packet uses coarse, privacy-protected bands for:

- mutual acceptances that never commit a forecast;
- committed forecasts with no eligible reference;
- pre-aggregation withdrawals;
- matcher or deletion failures; and
- scored contributions.

The pilot stops without a relational-skill claim if:

- fewer than 80% of mutually accepted dyads yield an eligible reference;
- source-gap or withdrawal rates differ by more than 10 percentage points
  across preregistered eligibility strata after privacy adjustment; or
- missingness cannot be bounded without disclosing a small group.

No imputation method may be selected after seeing outcomes.

## Anti-gaming and validity controls

- Participants never see forecasts, references, per-dyad errors, exact target
  offsets, or match status.
- The target offset is committed before forecast submission and cannot be
  changed after observing telemetry.
- Forecast submission closes before the earliest possible target time.
- A subject's post-commitment actions may change the reference. The protocol
  therefore measures prediction under participation awareness, not an
  untouched latent state. This limitation is mandatory in every report.
- Rate limits and contribution bounds apply by established identity and epoch.
- Dyad assignment and null permutations are fixed before the result is opened.
- The confirmatory analysis is immutable after enrollment starts; exploratory
  analysis requires a new cohort and privacy budget.
- Collusion probes, canary cohorts, and temporal shams must be evaluated by the
  independent reviewer, never exposed as participant rankings.

## Structural non-interference

Relational data is an experimental measurement plane only. A future CI gate
must prove that its package has no import, call, database-write, event-consumer,
or feature dependency into:

- EISV state mutation or baseline updates;
- behavioral or thermodynamic calibration;
- risk scoring, basin selection, or coherence calculation;
- policy, verdict, pause, guide, reject, or recovery decisions;
- agent comparison, ranking, assignment, or trust tiers;
- model prompts, retrieval, memory, or training inputs; and
- enforcement or governed-effect execution.

The dependency direction is one-way: an isolated matcher may read an eligible
reference through a narrow, read-only interface. Governance code must never
read relational outputs. Runtime credentials must make the prohibited writes
impossible, not merely undocumented.

## Adversarial review matrix

The independent review must test at least these attacks before operator
authorization:

| Threat | Required test | Pass condition |
|---|---|---|
| Auxiliary-reference reconstruction | Give an attacker its own vectors, public packets, and plausible external reference access | No other vector or membership inferred above the declared privacy bound |
| Sign and repeated-observation recovery | Combine absolute errors or packet fields across epochs | No per-dyad value exists; epoch unlinkability prevents joining |
| Cohort differencing | Compare every allowed release and failure response | Only one fixed packet exists; suppressed cohorts reveal no exact delta |
| Colluding observer and subject | Share all participant-visible data | Cannot learn a third participant's vector or cohort membership |
| Sybil inflation | Create many fresh or synthetic identities | Eligibility and contribution gates reject them; established-history checks are not bypassable |
| Timing manipulation | Delay forecast or trigger chosen check-ins | Committed target and first-eligible matching remain immutable |
| Consent coercion | Operator or peer conditions access on participation | Solicitation is rejected and recorded without affecting the subject |
| Withdrawal race | Withdraw at every state transition under concurrency | Exactly one terminal state; no scored contribution after successful withdrawal |
| Replay | Reuse grants, commitments, sessions, or epoch aliases | Fail closed and erase payload without revealing prior match state |
| Cross-identity access | Read status or data using another bound identity | Uniform denial with no existence oracle |
| Source degradation | Redis, matcher, reference source, or isolated worker fails | No fallback to ordinary storage; payload is erased or remains cryptographically inaccessible |
| Logging and tracing | Force exceptions containing every sensitive value | No raw or derived per-dyad value reaches logs, audit, traces, or crash reports |
| Deletion side channel | Compare timing and receipts across terminal paths | Participant-visible responses are indistinguishable within the declared bound |
| Governance coupling | Inject relational events and outputs into the live system | No EISV, calibration, policy, verdict, ranking, prompt, or enforcement consumer exists |

Any failed row blocks the pilot. Tests cannot be waived by relabeling the
feature "shadow-only".

## Stop criteria and shutdown

Collection must stop immediately and all unreleased material must be destroyed
if any of these occurs:

- unauthorized access, raw-value logging, cross-identity disclosure, or a
  deletion failure;
- privacy-budget exhaustion, a second release attempt, or a successful
  differencing/reconstruction attack;
- a consent, replay, idempotency, target-selection, or contribution-bound
  invariant fails;
- relational data reaches any prohibited governance or model-input path;
- reference completion falls below 80% or differential missingness exceeds 10
  percentage points;
- the cohort misses its preregistered sample-size gate;
- privacy noise or clustered variance leaves the primary test underpowered; or
- the primary success rule fails.

Shutdown is fail-closed and irreversible for the cohort: revoke runtime
credentials, destroy ephemeral keys and unreleased payloads, disable the worker,
write a redacted incident record, and require a new protocol version plus new
operator authorization before any restart. A failed statistical result closes
the hypothesis; it is not permission to collect a larger post-hoc cohort.

## Required approval record

The following record must remain blank in this specification PR. A future
operator may complete it only after the privacy review and adversarial evidence
exist, in a separate commit and PR:

```text
Privacy review artifact:
Adversarial test artifact:
Approved cohort definition:
Approved duration:
Approved access policy:
Approved privacy budget:
Approved retention/deletion plan:
Approved incident and shutdown plan:
Approving human operator:
Approval timestamp:
Approval scope/expiry:
```

A generic "proceed", an earlier feature request, code review, or merge of this
document is not this approval. The approving operator must see the completed
evidence and explicitly authorize the bounded pilot described by its immutable
protocol version.

## What a useful negative result looks like

The pilot earns its keep even if it never runs or the primary hypothesis fails:

- failure to construct a private measurement establishes that current
  architecture cannot support relational telemetry without surveillance risk;
- insufficient cohort size establishes a federation-capacity bound;
- high missingness establishes that the reference process is not stable enough;
- no lift over persistence bounds the value of relational prediction; and
- successful privacy and non-interference tests establish reusable federation
  infrastructure without turning EISV into an interpersonal score.

Those are all informative outcomes. None licenses a claim about machine
empathy or qualia.
