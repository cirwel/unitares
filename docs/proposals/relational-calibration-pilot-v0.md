# Relational calibration pilot v0.2: protocol and threat model

Status: specification only; runtime implementation prohibited

Date: 2026-08-10

Version: 0.2

Amendment: v0.2 keeps the v0.1 behavioral-maturity and distinct-role repairs,
then adds temporal establishment, a complete instrument lock, conservative
experimental-principal accounting, dyadic inference, and a frozen observer
information/target-horizon contract. The v0 capacity query remains immutable;
the v1 instrument-supply read supersedes it for this protocol. No privacy or
implementation gate is relaxed.

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
| Protocol | This preregistration is merged without runtime capability | v0 merged in PR #1574; v0.1 merged in PR #1580; v0.2 remains documentation-only |
| Instrument supply | The one-time aggregate read in [`relational-calibration-maturity-capacity-v1.md`](relational-calibration-maturity-capacity-v1.md) returns `instrument_supply_ready` | preregistered; prior reconnaissance is below the gate |
| Independent-principal capacity | A separate pre-enrollment artifact proves the principal, control-domain, contribution, and assignment-graph gates below without exposing a participant graph | blocked; historical process UUIDs cannot establish it |
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

> Within a frozen observer-information boundary, does the observer forecast a
> consenting subject's server-selected EISV reference measurement after time
> `t` better than preregistered non-relational baselines?

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
- each observer and baseline point error lies in `[0, 1]`.

For an eligible ordered dyad `d`, define point error as the unweighted mean
absolute error across the four normalized dimensions. Define forecast skill as:

```text
skill_d = error_baseline_d - error_observer_d
```

`skill_d` is signed and lies in `[-1, 1]`; positive values favor the observer
and negative values favor persistence. It must never be clipped to `[0, 1]`,
because doing so would erase failures and bias the result upward. Dimensions
must not be reweighted after seeing results.

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
2. **Fleet anchor:** the coordinate-wise median of each enrolled subject's last
   complete eligible measurement available when enrollment closes, sealed at
   that close and before the assignment graph or any target is opened.
3. **Observer-blind permutation:** forecasts are reassigned across eligible
   subjects within the fixed cohort while preserving horizon, source,
   observer-exposure, and control-domain strata plus the assignment graph's
   participant degrees.
4. **Temporal sham:** the observer forecast is compared with the first eligible
   subject measurement in the target capture window shifted exactly 24 hours
   earlier. A missing sham is reported only as coarse sham missingness and does
   not remove the dyad from the primary persistence comparison.

The persistence comparison is primary. The others diagnose leakage,
population-level guessing, and temporal autocorrelation. They cannot replace a
failed primary comparison.

## Unit of analysis and sample-size gate

The unit is one ordered observer-subject dyad in one fixed cohort epoch. The
contribution and privacy unit is an **experimental principal**, not a process
UUID. The dedicated access service conservatively joins every identity linked
by the same strong enrollment credential, declared lineage/thread component,
or independently attested controller into one epoch-scoped principal. The join
is transitive, and absence of a linkage edge never proves independence. Every
remaining component must present controller evidence distinct from every other
component; missing or contradictory evidence fails closed. The advisory runtime
`principal_id` rollup is not sufficient authorization or proof.

One experimental principal may contribute at most four observations to an
epoch, at most two as observer and two as subject, across all of its identities.
An ordered principal dyad may contribute only once. Two identities mapped to
the same principal are self-prediction and are forbidden. The analysis plane
receives only random epoch-scoped principal cluster tokens and coarse,
preregistered nuisance strata; stable UUIDs, credentials, lineage edges, and
the participant graph never enter a result packet.

No confirmatory statistic may be released unless the completed cohort has all
of:

- at least 200 eligible matched dyads;
- at least 100 distinct experimental principals as observers;
- at least 100 distinct experimental principals as subjects;
- at least five independently administered control domains, with no domain
  supplying more than 25% of scored dyad endpoints; and
- no principal exceeding the contribution bounds above.

The v0 values of 50 observers and 50 subjects were arithmetically inconsistent
with 200 dyads: at no more than two contributions in either role, 50 observers
can supply at most 100 dyads, and the same limit applies to 50 subjects. The
correct lower bound is 100 distinct principals in each role. The role sets may
overlap, so 100 total principals is possible only if every principal serves in
both roles at both contribution limits; disjoint role sets require 200. The
control-domain floors are governance robustness constraints, not claims that
five domains represent every federation. A control domain is an independently
administered credential and key-custody root; a process, host label, model name,
or self-declared tag is not sufficient.

The 200-dyad threshold is an arithmetic floor, not a power guarantee. Before
enrollment, the graph-construction rule, planned degree and horizon/exposure
strata, participant and control-domain dependence grid, clipping rule, frozen
simulator, and differential-privacy mechanism must be sealed. Simulation over
that complete design must show at least 80% power for the inferential component
(the one-sided lower bound exceeds zero) when the true mean improvement is
`0.05`. It must separately report the probability of satisfying the full
two-part success rule over the frozen true-effect grid
`{0, 0.025, 0.05, 0.075, 0.10}`. Lower-bound power must not be mislabeled as
full-rule power: at a true effect exactly equal to the `0.05` point-estimate
threshold, an approximately unbiased estimate crosses that threshold only
about half the time.

After enrollment closes and the principal join is sealed, the rule instantiates
the exact assignment graph. Before any forecast commitment, the frozen
simulator must retain at least 80% inferential power at `0.05` on that actual
graph and record its full-rule curve, or the pilot stops. A blinded
nuisance-variance check may suppress the result before unsealing; it may not
expand the cohort, replace a principal, or change the graph after any forecast
outcome is inspected.

Primary uncertainty must use a reviewed dyadic cluster-robust procedure that
treats recurrence of the same experimental principal in either role as shared
dependence. Its graph-preserving bootstrap or randomization procedure must keep
principal degrees and the preregistered horizon, source, exposure, and
control-domain strata fixed. The one-sided confidence bound must incorporate
the declared privacy mechanism's noise distribution. Ordinary row-level,
observer-only, subject-only, or process-UUID clustering is invalid.

If any threshold is missed, the only publishable result is
`insufficient_private_cohort`. Exact near-threshold counts are not disclosed.

Before implementation review, the separate
[instrument-supply preregistration](relational-calibration-maturity-capacity-v1.md)
must return `instrument_supply_ready`. That read counts only process-identity
telemetry supply. It does not establish experimental principals, control-domain
independence, consent, role availability, privacy readiness, or permission to
enroll anyone. Those properties require the separate pre-enrollment artifact.

## Eligibility

Eligibility is evaluated before prediction and must fail closed.

At the latest eligible pre-enrollment state, both participants must have:

- caller-proven strong identity at each consent or contribution step;
- server-persisted `behavioral_eisv.warmup.is_baselined = true`;
- `behavioral_eisv.warmup.baseline_confidence >= 0.8` and Welford
  `baseline_stats` counts of at least 25 for each of `E`, `I`, `S`, and `V`,
  all read from the same state row;
- at least 25 non-synthetic rows with 25 distinct update counters under the
  exact v0.2 instrument, spanning at least 24 hours and at least six distinct
  UTC hour buckets before enrollment, all in the current uninterrupted,
  non-decreasing update-counter run;
- the v0.2 maturity-version tuple: `baseline_target = 30`, behavioral
  `v_formula_version = 2`, and the server-reported `is_baselined` flag above;
- a complete, non-synthetic `eisv.telemetry.v1` envelope carrying the
  preregistered measurement and derivation provenance;
- no bootstrap or synthetic row used for eligibility, baseline, or reference;
- enrollment in the fixed cohort before its consent window closes; and
- no active block, withdrawal, or conflicting role constraint.

The v0 five-envelope rule is withdrawn as a sufficient condition. In the
deployed behavioral implementation, five updates only end the zero-data portion
of baseline confidence; they do not establish the self-relative measurement
regime.
`is_baselined` becomes true at confidence `0.8` (currently about update 25),
while confidence reaches `1.0` at update 30. The protocol keys on the persisted
server flag and freezes the accompanying numeric/version checks so a later code
change cannot silently reinterpret this cohort.

Update count alone is not temporal evidence: the deployed write limiter permits
many updates in one hour. v0.2 therefore restores the useful part of the old
duration guard and requires six distinct UTC hour buckets across at least 24
hours under one instrument. Twenty-four hours is four times the longest frozen
target offset below. It is an eligibility floor, not proof that a baseline is
stationary.

The descriptive `warmup.phase` string is not an eligibility predicate. It
remains `warming_up` until update 30 even though self-relative scoring becomes
eligible at `is_baselined = true` around update 25. Substituting the phase label
would silently change the maturity contract.

Subject maturity is semantically load-bearing because the subject supplies the
future reference measurement. Observer maturity is a conservative
established-participant and anti-Sybil condition that keeps both cohort roles
under one auditable contract. Neither is evidence of empathy, experience,
moral status, reliability, or worth.

Self-prediction is forbidden at the experimental-principal boundary. Strong
identity limits duplicate participation but is not treated as proof against
Sybils or shared control. The access service enforces the conservative
principal join above before producing epoch-scoped analysis tokens.

## Measurement-phase and provenance lock

The last eligible subject measurement available before prediction commitment
and the future target must both satisfy the maturity contract above and carry
the same preregistered provenance tuple:

```text
(envelope schema,
 primary EISV source,
 behavioral observation source,
 derivation kind,
 derivation formula version,
 derivation history window,
 behavioral V formula version,
 behavioral baseline target,
 E/I/S/V EMA alphas)
```

For the v0.2 pilot and instrument-supply gate, the only compatible tuple is
frozen to
`(eisv.telemetry.v1, behavioral, behavioral, behavioral_sensor,
behavioral_sensor.v1, 10, 2, 30, 0.12, 0.08, 0.15, 0.10)`, with an empty
`derivation.missing_inputs` array. The alpha order is `E`, `I`, `S`, `V`. A
future protocol may preregister another source stratum, but may not pool unlike
instruments after seeing results.

Every eligible row must also satisfy the same-row integrity contract:

- persisted behavioral, envelope behavioral-smoothed, and primary `E/I/S/V`
  values are numeric, in range, and agree within `0.0001` per dimension; the
  persisted raw, envelope raw, and derivation-computed `E/I/S` triplets satisfy
  the same contract (the tolerance covers deployed four-decimal persistence);
- persisted and envelope EMA alphas are numeric, equal, and match the frozen
  tuple;
- persisted and envelope update count, baseline confidence, baseline target,
  and `is_baselined` state agree;
- `observed_at` parses as a timestamp and is within five seconds of the
  append-only row's `recorded_at`; and
- `measurement_id` parses as a UUID and is unique among non-synthetic state
  rows through the cohort cutoff.

Any cold-start-to-baselined transition, schema change, source change, missing
provenance field, formula/configuration change, value mismatch, timestamp
mismatch, duplicate measurement ID, or incomplete derivation between the
persistence reference and target yields `source_gap` and no score. The isolated
analysis may retain only the epoch-scoped tuple needed to enforce this rule; it
must not expose stable participant identities or provenance histories.

v0.2 retains the future normalized raw EISV reference as its target and tests
incremental skill against subject persistence. A within-subject residual or
change-score target is a different estimand and requires a new protocol,
cohort, and privacy budget; it cannot be substituted after outcomes are seen.

## Target selection

Neither participant chooses the reference time or row.

At forecast commitment time `t0`, the server samples uniformly from the four
discrete offsets `{30, 90, 180, 360}` minutes with fresh randomness and commits
to the draw. The eligible capture window is exactly `[t0 + offset,
t0 + offset + 60 minutes]`. The participant-visible consent text discloses the
overall 30-minute-to-seven-hour capture envelope but not the draw. After the
committed horizon, an isolated matcher selects the first row that satisfies all
of:

- `synthetic IS NOT TRUE`;
- the EISV telemetry envelope is complete and on the preregistered schema;
- the subject remains mature under the frozen v0.2 contract;
- the source and complete provenance tuple match the pre-commitment persistence
  reference; and
- `recorded_at` lies inside the fixed capture window.

No caller-supplied vector is accepted. No later or earlier row may be selected
to improve matching. A missing eligible row yields `source_gap` and no score.
The target commitment is opened only to the independent audit reviewer, not to
participants or a dashboard.

### Observer information boundary

The experiment supplies the observer only the subject identity needed for the
consent offer and the fixed protocol disclosure. It must not retrieve or reveal
subject EISV, governance state, baselines, other forecasts, cohort anchors,
target commitments, or experiment-derived subject content. Permissible forecast
inputs are limited to information independently authorized and already visible
to the observer before the subject offer: the observer's own state/history,
pre-existing subject-authored artifacts, and pre-existing shared work context.
The experiment does not copy that content into its measurement plane.

Before assignment, the isolated service records only a coarse exposure
manifest:

- direct-interaction recency bucket (`none`, `<1h`, `1..24h`, `1..7d`, `>7d`);
- pre-existing shared-work-context boolean;
- same-control-domain boolean; and
- any subject-telemetry access event during the preceding 24 hours.

Any subject-telemetry access makes the dyad ineligible. The remaining manifest
fields are nuisance strata for assignment and graph-preserving inference, not
subgroup outputs. No raw content, stable relationship edge, exact interaction
timestamp, or per-dyad manifest leaves the isolated boundary. Shared context
can still explain apparent skill; every report must retain that limitation. A
missing or unverifiable manifest makes the dyad ineligible.

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
references, per-dyad errors, dyad identifiers, consent linkage,
principal-resolution attestations, or controller/control-domain linkage.

A future privacy architecture must demonstrate all of the following before an
implementation PR:

- Raw forecast and reference vectors are never written to durable application
  logs, ordinary Redis, PostgreSQL audit tables, traces, error reports, or
  model context.
- Principal resolution accepts only purpose-bound enrollment attestations. Its
  transitive identity/controller/domain map is epoch-scoped, inaccessible to
  analysis, governance, model context, and ordinary operators, and destroyed
  after the aggregate or any terminal failure. A hash or stable pseudonym of a
  UUID, credential, lineage edge, or controller edge is still linkable and is
  not anonymization.
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
satisfy a row threshold without providing principal diversity. This is why the
protocol permits one immutable result packet, bounds each experimental
principal's contribution, requires distinct principals, and spends a fixed
experimental-principal-level differential-privacy budget.

A bare commitment such as `SHA256(canonical_eisv)` also fails: four bounded,
low-precision dimensions are cheap to enumerate. A future commitment must mix
in unpredictable high-entropy randomness, destroy that opening material on
terminal paths, and prove that neither commitment identifiers nor timing link
the same dyad across epochs.

### Unresolved privacy architecture

Two architecture families remain candidates, neither approved:

1. reviewed secure multiparty computation with non-colluding aggregation
   principals and experimental-principal-level contribution enforcement; or
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

The release mechanism must provide experimental-principal-level differential
privacy with a total pilot budget no larger than `epsilon = 1.0`,
`delta = 1e-6`. The budget is divided among the fixed fields before collection
and cannot be replenished.
Counts are released as coarse bands, not exact values. All linked identities of
one principal are adjacent as one privacy unit, and principal contribution
bounds are enforced before noise is added.

Privacy adjacency is add/remove one experimental principal and every dyad
incident to it. Each principal has at most four incident signed skill values in
`[-1, 1]`; the mechanism must account for that whole bounded contribution and
for the released denominator. It may not claim row-level sensitivity, clamp
negative skill to zero, or add noise only after an exact mean has escaped the
isolated boundary. The exact mechanism and budget allocation remain part of the
pre-enrollment privacy/power artifact.

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
- Rate limits and contribution bounds apply by experimental principal and epoch.
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
- identity, lineage/thread, process-binding, principal-rollup, authorization,
  or access-control mutation;
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
| Principal fan-out | Spawn many process UUIDs under one controller, lineage, thread, or credential | Conservative transitive joining yields one capped experimental principal; missing controller proof fails closed |
| Resolver repurposing | Reuse enrollment attestations or epoch links as a durable controller/relationship graph | Purpose-bound resolver credentials cannot write governance or identity state; the map is erased at cohort close and cannot be joined across epochs |
| Control-domain pseudoreplication | Supply most dyad endpoints from one operator or administrative domain | Five-domain and 25% endpoint-concentration gates block the confirmatory result |
| Burst maturity | Accumulate 25 updates in a short loop | Same-instrument history still spans 24 hours and six UTC hour buckets |
| Instrument substitution | Change alphas, formula/configuration, values, timestamps, or measurement IDs | Same-row and reference-to-target locks fail closed as `source_gap` |
| Context leakage | Reveal subject telemetry or experiment-added subject context before commitment | Access audit makes the dyad ineligible; only the frozen coarse exposure manifest enters isolation |
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
- a temporal-establishment, instrument-integrity, experimental-principal, or
  control-domain invariant fails;
- principal-resolution evidence leaks, persists past its epoch, or reaches an
  identity, governance, authorization, model, or ranking surface;
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
- failure of the independent-principal gate bounds the cohort this federation
  can support under the frozen eligibility and control-domain criteria;
- high missingness establishes that the reference process is not stable enough;
- no lift over persistence bounds the value of relational prediction; and
- successful privacy and non-interference tests establish reusable federation
  infrastructure without turning EISV into an interpersonal score.

Those are all informative outcomes. None licenses a claim about machine
empathy or qualia.
