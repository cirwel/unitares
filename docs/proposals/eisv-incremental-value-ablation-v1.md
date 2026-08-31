# EISV Incremental-Value Ablation Preregistration

- Status: draft; no cohort enrolled and no experiment scheduled
- Study ID: `eisv-incremental-value-v1`
- Protocol version: `0.3.0`
- Dataset namespace: `eisv-incremental-value-v1`
- Access policy: `eisv-incremental-access.v1`
- Schema: `docs/evaluations/eisv-incremental-value/eisv-ablation-episode-v2.schema.json`
Date drafted: 2026-08-27

## Decision this study will support

This study will determine whether the EISV representation contributes unique,
prospective governance value beyond the direct evidence and behavioral features
from which it is derived.

The possible decisions are:

1. retain EISV as a policy input;
2. retain EISV only as a compact display or compatibility representation;
3. unbundle the dimensions that demonstrate unique value;
4. de-authorize EISV and retain it only as versioned historical telemetry; or
5. retire EISV after downstream consumers have migrated.

Phase I is observational and does not change production actions. It tests
prediction and compression. A later Phase II may test the causal value of soft
guidance, but only if Phase I establishes predictive value.

## Estimands

### Primary estimand: incremental predictive value

The primary estimand is the out-of-sample change in Brier score when EISV is
added to a grounded behavioral model:

```text
delta_brier = Brier(A2_behavioral_no_eisv) - Brier(A3_behavioral_plus_eisv)
```

Positive values favor EISV. The primary endpoint is the occurrence of an
independently verified severe adverse outcome during the prediction window.

### Secondary estimand: compression value

The compression estimand tests whether EISV alone preserves nearly all of the
predictive performance of the named behavioral features. This is a
non-inferiority comparison of `A4_eisv_only` against
`A2_behavioral_no_eisv`.

EISV can therefore fail to add unique information while still remaining useful
as an interface. That result must not be mislabeled as evidence that EISV is
noise.

### Dimension estimands

Four leave-one-dimension-out comparisons estimate the unique contribution of
E, I, S, and V. These tests answer whether the vector should be unbundled even
when the full representation has value.

### Not estimated in Phase I

Phase I does not estimate the causal effect of a `guide` or `pause` action.
Production policy changes subsequent outcomes, so causal intervention utility
requires a separate randomized or stepped-wedge study.

## Hypotheses

- **H0 — model adequacy:** A2 and A3 must each be reported against the frozen
  base-rate and persistence baselines. No result authorizes EISV as a policy
  input if A3 fails to outperform both trivial baselines.
- **H1 — incremental value:** `A3` improves relative Brier score by at least 5%
  versus `A2`, with a 95% clustered bootstrap interval excluding no
  improvement.
- **H2 — compression value:** `A4` is non-inferior to `A2` within a 2% relative
  Brier-score margin.
- **H3 — dimension value:** removing an EISV dimension worsens relative Brier
  score by at least 2%, with multiplicity-adjusted evidence.
- **H4 — legacy value:** the legacy ODE/coherence arm must outperform the
  task-family base-rate arm and show acceptable calibration to justify any
  authority beyond compatibility telemetry.

The percentage thresholds are provisional until the instrumentation pilot
estimates label frequency and measurement noise. They must be frozen before
confirmatory collection begins.

## Unit of analysis

The unit is a **decision episode**, not a check-in row and not an individual
tool event. A decision episode begins when all eligible shadow arms score the
same immutable feature snapshot. It ends when the prediction window closes.

Multiple episodes may belong to the same agent or task. Statistical uncertainty
must therefore be clustered by both an independently assigned execution unit
and task. The execution unit groups agent identities that share a physical
substrate, deployment, resident process, or other stateful producer. Agent UUID
is a descriptive repeated-measures key, not proof of independence. Rows must
never be treated as independent merely because they have distinct agent or
prediction IDs.

## Failure-informed design constraints

The protocol treats the following as identification requirements rather than
optional robustness checks:

- **Independent unit:** every episode records `independence_unit_id` and a
  privacy-preserving `substrate_id_hash`. Identities known to share a process,
  host, resident producer, or synchronized state must share an independence
  unit.
- **Wall-clock continuity:** step count never substitutes for elapsed time.
  Schedule class, cadence, previous authored observation, process instance,
  monitor lineage, restart state, formula discontinuities, and hydration state
  are frozen at prediction.
- **Independent labels:** an outcome produced from governance risk, verdict,
  pause, recovery, lifecycle status, or another downstream policy field cannot
  count toward the primary endpoint.
- **Instrument assessability:** every run is `PASS`, `FAIL`, or `UNASSESSED`.
  A requested check that executes no assertions is `UNASSESSED`, never success.
- **Positive-control validity:** the scoring and analysis bundle must detect a
  planted absolute effect within the deployed feature domain and must fail when
  the target mechanism is replaced by a no-op. Controls parameterized by the
  threshold they validate are forbidden.
- **Policy/actuator separation:** the record distinguishes the action produced
  by policy, what was delivered to the agent, whether enforcement was requested,
  and whether enforcement actually applied.

### Failure records that changed this design

This protocol is not a clean-sheet evaluation. It incorporates the repository's
documented evaluation failures:

| Prior failure | Constraint added here |
|---|---|
| The historical outcome cohort used a contaminated scope, and `(agent, prior-state snapshot)` permutation blocks were later described as though they were independent failures ([stop-rule correction](eisv-outcome-grounding-stop-rule-v0.md), [inference containment](../ontology/falsification-inference-containment-2026-08-22.md)) | Prospective episodes carry substrate and producer-group keys; the registry unions shared producers into one independence unit; uncertainty is grouped by execution unit and task. |
| Forty-two watchdog and forty-three dogfood-guard runs exposed live outcome discrimination after the registered cutoff ([design-system audit](../ontology/falsification-design-system-audit-2026-08-23.md)) | A new dataset namespace is isolated from the historical stop rule; pilot reads cannot pair predictions with outcomes; every authorized read has an immutable receipt. |
| The first power probe reused cluster IDs as outcome pairing IDs, silently corrupting paired comparisons, while the preserved cohort omitted the total cluster geometry ([power audit](../operations/falsifiability-power-audit-2026-08-23.md)) | Every episode, prediction, event, and snapshot has a separate identifier; confirmatory power must preserve paired arms, unique event IDs, class balance, and the observed group geometry. |
| A positive control passed using unreachable values, and a second control still passed after its target mechanism was replaced by a no-op ([positive-control audit](../operations/positive-control-validity-2026-08-23.md)) | Controls use absolute in-domain effects, include explicit no-op mutations, and may return only `PASS`, `FAIL`, or `UNASSESSED`. |
| Deployment volume mixed co-developing agents, shared hosts, synthetic stress traffic, and one physically independent producer ([deployment caveat](../operations/DEPLOYMENT_DATA_CAVEAT.md)) | Agent UUID and row count are descriptive only; substrate, producer group, schedule class, and task concentration are mandatory strata. |
| A newly observed negative row was initially reported as “prevented” even though policy proceeded and no actuator blocked anything ([finding correction](../operations/ablation-initiates-finding-2026-06-16.md)) | Outcome, policy decision, delivery, enforcement request, and applied actuation are separate fields; Phase I makes no prevention claim. |
| An individuality test treated a long fleet outage as adjacent observations in step time ([design-system audit](../ontology/falsification-design-system-audit-2026-08-23.md)) | Wall-clock gap, schedule, process instance, restart, hydration, lineage, and formula discontinuities are frozen at every cutoff. |

## Dataset and analysis firewall

Every episode is written under the dedicated dataset namespace
`eisv-incremental-value-v1`. The historical outcome-grounding stop rule and its
scheduled 2026-12-01 read use a different protocol and may not read, join, or
refresh this namespace. This study likewise may not use the historical stop-rule
ledger as its outcome store.

All analysis access goes through
`scripts/analysis/eisv_incremental_value_contract.py` and receives an immutable
local receipt before any rows are returned:

- `pilot_instrumentation` may inspect structure, linkage, prevalence, missingness,
  and cluster supply, but may not obtain arm scores and outcomes together;
- `registered` is the only confirmatory discrimination read and requires the
  frozen preregistration hash, configuration hash, namespace, `not_before`, and
  `as_of` boundary;
- `reproduction` requires explicit contamination acknowledgement and a new read
  ID; it never rewrites the confirmatory receipt; and
- unknown readers, purposes, namespaces, or repeated read IDs fail closed.

Automation may collect episodes without seeing paired predictions and outcomes.
No dogfood, watchdog, scheduled stop-rule, dashboard, or exploratory job is an
authorized analysis reader merely because it can reach the database.

## Temporal assignment and independence registry

Out-of-sample means rolling-origin evaluation with a purge gap at least as long
as the maximum 24-hour prediction horizon. Assignment happens before the
episode outcome is known. Confirmatory folds must satisfy:

```text
train_end + 24 hours <= test_start < test_end
```

All episodes sharing either `independence_unit_id` or `task_id` stay on one side
of a fold boundary. Hyperparameter selection, imputation, scaling, and
calibration use training folds only. Test-fold scores are produced once by the
frozen scorer bundle. Pilot episodes are `pilot_only` and cannot later enter a
confirmatory training or test fold.

The independence registry is an explicit union of substrate and producer-group
keys. Two identities sharing a substrate hash or a stateful producer group must
resolve to the same `independence_unit_id`; conflicting assignments are a
contract failure, not a third cluster. The registry may grow during the pilot,
but its resolver version and content hash freeze before confirmation.

Primary-label eligibility is a whitelist, not an inference from the
`verification_source` string. Only independent human, external-system, or
server-primitive events with no governance-input overlap qualify. New label
types begin as secondary-only until their producer and independence contract is
reviewed and versioned.

## Eligibility

An episode is eligible for the primary comparison when:

- it has a stable agent UUID and task identifier;
- it has a stable independence unit, substrate hash, process instance, and
  monitor lineage identifier;
- the feature snapshot was created before the outcome window;
- both baseline arms and arms `A1` through `A4` all produced scores from the
  same snapshot;
- the production action and any intervention exposure were recorded;
- observation authorship, schedule/cadence, restart, hydration, formula
  discontinuity, and lineage continuity were recorded at the cutoff;
- outcome telemetry remained observable through terminal state or the complete
  prediction horizon; and
- no feature was recomputed using post-cutoff evidence;
- the frozen instrument-validation bundle was assessed and passed.

Episodes remain in the audit dataset but are excluded from the primary analysis
when they have duplicate prediction IDs, invalid timestamps, scorer-version
drift, incomplete horizons, unbound outcomes, or detected feature leakage.
Every exclusion must use a structured reason; silent row deletion is forbidden.
Missing independence, continuity, or instrument-validity evidence is likewise
an exclusion or `UNASSESSED` result, never an implicit pass.

## Prediction window

The prediction window begins immediately after the frozen snapshot and closes
at the earliest of:

1. a terminal task outcome;
2. 20 subsequent observable action/result events; or
3. 24 hours after the prediction timestamp.

Check-ins and telemetry-only writes do not count as observable action/result
events. If telemetry ends before any closing condition, the episode is censored,
not labeled successful.

## Primary outcome

`primary_adverse_outcome` is true only when at least one severe event is
independently grounded and occurs within the prediction window. Qualifying
events are:

- terminal task failure or abandonment;
- externally validated trajectory invalidation;
- a substantive human correction or override caused by factual or operational
  error;
- rollback or revert caused by an erroneous action; or
- an unresolved test failure at task termination.

A governance verdict, guide, pause, recovery action, risk score, basin, or EISV
threshold crossing is **not** itself a primary adverse outcome. Counting a
policy's own action as its success label would make the test circular.

Each qualifying event must have `policy_independent=true`,
`counts_toward_primary=true`, a verification source, and structured label
provenance showing an independent human, external system, or server-verified
primitive producer with no governance-input overlap. `server_observation` alone
does not establish independence: a server-authored governance verdict is still
governance-derived. Agent-reported and governance-derived results may be
retained for secondary analyses but cannot count toward the primary endpoint.

## Secondary outcomes

- any negative operational event, including transient test failure or tool
  rejection;
- terminal task success;
- confidence calibration error;
- time or action count from prediction to the first adverse event;
- production guides and pauses per 1,000 episodes;
- false-alert burden, defined as an intervention without a subsequent primary
  adverse outcome;
- time and action count to recovery; and
- missing or censored outcome rate.

## Maturity strata

Results must be reported separately for the deployed authority phases:

| Stratum | Check-in index | Interpretation |
|---|---:|---|
| `cold_start` | 1–2 | Mostly server-derived Phi prior |
| `behavioral_fixed` | 3–24 | Behavioral assessment against fixed thresholds |
| `self_relative` | 25+ | Behavioral assessment with an agent-relative baseline |

The primary headline result pools strata only through a prespecified model with
stratum indicators. A gain confined to cold start does not establish warm-path
EISV value, and a gain confined to mature agents does not justify cold-start
authority.

### Exposure and continuity strata

Every maturity result must also report:

- interactive, scheduled-resident, automation, harness, and unknown schedule
  classes;
- continuous, genesis, discontinuous, and unknown monitor lineage;
- restart and hydration state;
- formula/scheme discontinuities since the previous authored observation;
- wall-clock gap since the previous authored observation; and
- cold, fixed-threshold, warmed-baseline, and reconstructed reference states.

Scheduled traffic may dominate row counts without increasing fleet breadth.
No pooled claim is permitted when one schedule class, independence unit, or
substrate supplies more than half of the eligible episodes unless the
concentration and leave-one-unit-out result are reported beside it.

## Shadow arms

Every arm receives a score in `[0, 1]` for the same primary outcome. All arms
except `A7` are invisible to the agent and cannot change production behavior.

| Arm ID | Inputs | Question answered |
|---|---|---|
| `a0_base_rate` | Frozen task-family and maturity base rates | Is any model better than prevalence? |
| `a0_persistence` | Last eligible independently grounded outcome, with a frozen missing-history fallback | Does any model beat a trivial persistence rule? |
| `a1_direct_evidence` | Pre-cutoff tool/test evidence, confidence, complexity, and history | What can grounded observations predict? |
| `a2_behavioral_no_eisv` | A1 plus named behavioral residuals; excludes EISV, Phi, coherence, basin, risk, and verdict | What can the behavioral path predict without EISV? |
| `a3_behavioral_plus_eisv` | A2 plus E, I, S, V and their provenance | Does EISV add unique information? |
| `a4_eisv_only` | E, I, S, V plus source and maturity metadata | Is EISV a useful compression? |
| `a5_minus_e` | A3 without E | Does E contribute uniquely? |
| `a5_minus_i` | A3 without I | Does I contribute uniquely? |
| `a5_minus_s` | A3 without S | Does S contribute uniquely? |
| `a5_minus_v` | A3 without V | Does V contribute uniquely? |
| `a6_legacy_only` | Phi and explicitly sourced legacy coherence/ODE feedback | Does the compatibility path add grounded value? |
| `a7_production` | The deployed policy and its current inputs | How does the operational system compare? |

The primary statistical arms use the same model family, preprocessing,
regularization procedure, training window, and label definition. Otherwise,
differences in model capacity could be mistaken for feature value.

The primary scorer will be an L2-regularized logistic model because its feature
contributions and calibration can be audited. More flexible models may be run as
exploratory sensitivity analyses but cannot replace the preregistered primary
analysis.

## Feature and provenance rules

- All feature groups are timestamped and hashed at the cutoff.
- Missing values are explicit `null` values, never silently imputed at logging
  time.
- Imputation and scaling parameters are learned during the pilot and frozen for
  confirmation.
- A1 and A2 receive the direct pre-cutoff evidence that feeds EISV. Withholding
  those inputs would unfairly make EISV appear incrementally informative.
- A2 must exclude EISV, Phi, coherence, risk, basin, margin, verdict, and policy
  action.
- Direct evidence and behavioral arms must exclude governance-derived outcome
  labels. If deployed EISV consumed such labels, A3 and A4 retain the deployed
  value but the provenance flag must expose the overlap for stratified analysis.
- A6 must use only values tagged with their actual producer and role. Untagged
  historical coherence is `unknown_legacy` and excluded from confirmatory
  scoring.
- A7 is recorded for operational comparison but is not allowed to leak its
  verdict or action into the other arms.
- Confidence source, confidence-derivation scheme, calibration assessability,
  EISV component formulas, reference-state version, Phi formula, and coherence
  formula are explicit versioned fields. A formula migration creates a declared
  discontinuity stratum.
- The persistence arm uses only pre-cutoff independently grounded labels and a
  frozen fallback for agents without such history.
- Raw prompts and response text are not required. Structured features, event
  identifiers, hashes, and minimal coded details are preferred.

## Pilot and model freeze

The instrumentation pilot runs for at least 14 calendar days and until at least
50 primary adverse outcomes have been observed. Pilot data is used only to:

- validate event linkage and censoring behavior;
- estimate prevalence and cluster sizes;
- estimate concentration by independence unit, substrate, task, schedule class,
  and maturity;
- select preprocessing and regularization;
- estimate the sample size needed to detect the minimum important effect; and
- identify missing or unstable feature sources.

The create-only pilot manifest is
`docs/evaluations/eisv-incremental-value/pilot-manifest-v1.example.json`, checked
against `pilot-manifest-v1.schema.json`. The checked-in example has collection
disabled. Enabling a real store requires a separately reviewed manifest carrying
an authorization identifier and trusted `start_not_before`; the pilot tool has no
enable command. Manifest versions are `pilot_provisional`: they are immutable
within one pilot store so its records stay interpretable, but they are **not** a
claim that the confirmatory registry or scorer has been frozen.

`scripts/analysis/eisv_incremental_value_pilot.py` installs that manifest and can
atomically append only schema-valid `pilot` / `pilot_only` bundles. It writes a
score-free structural sidecar for uniqueness, independence-registry, censoring,
and cluster-geometry checks. Its only read command returns aggregate structural
and outcome inventory after an immutable access receipt; it neither enumerates
raw episodes nor pairs arm scores with outcomes. The implementation does not
enroll a cohort, schedule collection, or query production data.

### Federated pilot execution

Pilot geometry may be combined across independently operated deployments with
`scripts/analysis/eisv_incremental_value_federation.py`. Federation does not
centralize episode records. Each site reads its local score-free structural
sidecars through the same immutable pilot-instrumentation receipt, replaces
local independence, substrate, producer, and task identifiers with
federation-scoped HMAC linkage tokens, and signs the resulting package with a
registry-bound Ed25519 site key. Raw episodes, arm scores, agent identifiers,
and local cluster identifiers do not leave the site.

The coordinator accepts only packages whose site key, namespaces, and complete
protocol/config/scorer fingerprint match an explicit federation registry. It
rejects missing required sites, duplicate site packages, altered signatures,
and contract drift before aggregation. Shared substrate or producer tokens are
unioned across sites; shared task tokens are combined across sites. The
registry also carries a `shared_state_domain`: every local cluster from sites
in the same domain is conservatively unioned even when no linkage token
matches, preventing two frontends over one stateful deployment from becoming
false replication. `federation_unit_id` is retained separately for deployment
and operator concentration reporting.

The federation output is still `PILOT_AGGREGATE_ONLY` with
`decision_authority=NONE`. It contains denominator, censoring, label-supply,
cluster-geometry, maturity, schedule, and concentration summaries only.
Federation does not authorize paired arm-score/outcome access, estimate the
A2-vs-A3 effect, enroll a cohort, or create a confirmatory freeze. Site signing
keys and the shared linkage key are generated as private create-only files; the
registry distributes public signing keys, while the linkage key is delivered
to sites out of band and never enters a package.

#### Federation threat model and trust boundary

Federation reduces central data exposure; it does not make participating sites
truthful. The coordinator is assumed honest in protocol execution but may be
curious about site metadata. A site may be buggy, compromised, or malicious.
Sites may collude. Signing and linkage keys may be stolen. Identifiers may be
low entropy. Package cadence, sizes, timestamps, cell cardinalities, stable
tokens, and rare intersections are observable metadata.

| Threat | Protocol posture |
|---|---|
| Package alteration or an unregistered sender | Prevented by registry-bound Ed25519 verification. A signature authenticates bytes and key possession; it does **not** prove truth, completeness, or non-equivocation. |
| Exact replay, stale/conflicting sequence, cross-run substitution, or registry substitution | Detected and rejected by the append-only coordinator ledger. Signed context binds federation, pilot run, site, registry digest, linkage-key epoch, reporting window, monotonic sequence, nonce, source receipt, contract fingerprint, and payload digest. Registry changes inside one pilot run fail closed. |
| Shared substrate presented through multiple sites | Detected when linkage tokens match; otherwise conservatively grouped when the registry declares a shared-state domain. Registry declarations remain operator attestations, not measured facts. |
| Low-frequency token intersection or differencing | Reduced by registry-enforced minimum-cell suppression and minimum export cadence. Suppressed counts remain in denominators and conservative cluster fallbacks. Stable tokens and aggregate metadata still create bounded within-run linkability; this residual is accepted and must be disclosed. |
| Cross-federation linkage | Prevented cryptographically at the token layer by including `federation_id` in every HMAC input, even if an operator mistakenly reuses key bytes. Distinct federation keys remain mandatory defense in depth. |
| Malicious site fabricates internally consistent counts | Not prevented. Signatures identify the submitting site only. Independent source audits or secure computation are required before any scientific use. |
| Coordinator omits a site or equivocates between reports | Required-site omission is rejected locally. The receipt binds the registry, package hashes, and report hash. Cross-coordinator transparency or an external witness log is not implemented and remains an accepted limitation. |
| Coordinator guesses raw identifiers | The coordinator does not receive the HMAC key. A compromised site or leaked linkage key can enable dictionary attacks against low-entropy identifiers; key compromise invalidates the linkage epoch. |

The independent observational unit is the connected component over local
independence clusters after union by shared substrate token, shared stateful
producer token, and cross-site `shared_state_domain`. A site, UUID, package,
row, or signature is never an independent replicate. Tasks form a second
connected partition by federation-scoped task token. Suppressed cells cannot
be used to increase effective sample size: they remain explicit and are
grouped conservatively by the declared site/state and task namespaces. The
combined report publishes cluster sizes and concentration, not an iid row
count.

Each federation registry is an immutable run snapshot with a `pilot_run_id`,
registry version, linkage-key identifier, privacy floor, export cadence, exact
contract fingerprint, and site-key validity/revocation state. Linkage keys are
federation-scoped, distinct from signing keys, stored mode `0600`, distributed
out of band, and never written to packages or receipts. A leaked linkage key
retires that linkage epoch and requires a new pilot run; rotation is not
silently mixed into an existing run. Site signing keys are independently
revocable and time bounded. Rotation likewise requires a new registry snapshot
and pilot run so previously accepted packages remain interpretable.

The package and combined-report schemas are closed (`additionalProperties:
false`) and validate `PILOT_AGGREGATE_ONLY` and `decision_authority=NONE` as
constants. The report schema contains no effect estimate, variance, confidence
interval, p-value, model ranking, arm score, or A2-vs-A3 comparison field.
Unknown fields fail validation rather than being ignored. No confirmatory or
promotion consumer currently accepts this artifact type; adding one requires a
separate registered protocol and review.

The power analysis must preserve unique outcome IDs, paired arm rows, observed
class balance, censored and unscorable trials in the denominator, and the
observed independence-unit/task cluster geometry. It must model the paired
covariance between A2 and A3. A scenario that changes prevalence while planting
an effect, overwrites rows by reusing cluster IDs, or treats repeated rows as
independent is invalid. If the minimum scientifically relevant effect is still
unspecified at freeze, the scientific result is `INCONCLUSIVE` regardless of an
operational stopping decision.

`scripts/analysis/eisv_incremental_value_power.py` is a planning entrypoint, not
that final power analysis. It accepts explicit sensitivity assumptions, including
the standard deviation of the paired A3-minus-A2 Brier-loss difference, and
inflates the episode denominator for observed cluster geometry, censoring, and
unscorable episodes. Its output is always `PLANNING_ONLY` with no decision
authority. The current pilot-instrumentation access policy intentionally does not
authorize paired prediction/outcome access, so it cannot estimate that covariance.
Before confirmatory freeze, a separately reviewed access declaration and a
registered two-way clustered Monte Carlo analysis remain required.

Pilot data is not part of the confirmatory result.

Before confirmation begins, the following values must replace `TBD` and the
document must be frozen by SHA-256:

| Freeze item | Value |
|---|---|
| Confirmatory start time | `TBD_AFTER_PILOT` |
| Required eligible episodes | `TBD_AFTER_POWER_ANALYSIS` |
| Required primary adverse outcomes | `TBD_AFTER_POWER_ANALYSIS` |
| Minimum scientifically relevant relative Brier improvement | `TBD_AFTER_PILOT` |
| Primary scorer bundle version | `TBD_AFTER_PILOT` |
| Instrument-validation bundle version and evidence hash | `TBD_BEFORE_CONFIRMATION` |
| Feature registry version | `TBD_AFTER_PILOT` |
| Analysis code revision/hash | `TBD_BEFORE_CONFIRMATION` |
| Preregistration SHA-256 | `TBD_AT_FREEZE` |

There is no early stopping for apparent efficacy. Collection ends only after
both the episode and adverse-outcome requirements are met. A safety or data
integrity problem may stop collection, but the reason and affected interval
must be reported.

## Confirmatory analysis

### Primary comparison

Calculate Brier score for A2 and A3 on the locked confirmatory dataset. Report:

- absolute and relative `delta_brier`;
- each model's Brier difference from the frozen base-rate and persistence arms;
- a 95% percentile interval from a two-way clustered bootstrap over
  `independence_unit_id` and task, retaining all nested agent episodes together;
- calibration intercept and slope;
- log-loss difference; and
- area under the precision-recall curve.

H1 passes only if the relative Brier improvement reaches the frozen minimum and
the interval excludes zero improvement. AUROC may be reported but is not a
primary criterion because severe adverse outcomes are expected to be uncommon.

### Compression comparison

Compare A4 with A2 using the frozen 2% relative Brier non-inferiority margin.
If A3 fails H1 but A4 passes H2, classify EISV as useful compression rather than
unique signal.

### Dimension comparisons

Compare A3 against each leave-one-out arm. Adjust the four dimension p-values or
bootstrap tail probabilities using Holm's procedure. Report effect sizes and
intervals even when they do not cross the decision threshold.

### Robustness analyses

Repeat the primary comparison by:

- maturity stratum;
- task family;
- agent/model family and harness;
- independence unit and substrate concentration, including leave-one-unit-out;
- schedule class and cadence;
- restart, hydration, formula discontinuity, and monitor-lineage continuity;
- reference state, including warmed versus reconstructed histories;
- EISV primary source;
- outcome verification source;
- terminal-only outcomes; and
- exclusion of all agent-reported outcomes.

These analyses diagnose generality; they do not replace the prespecified pooled
primary comparison.

## Interpretation matrix

| Result | Decision |
|---|---|
| A3 fails to beat either trivial baseline | No policy authority; diagnose the scorer before interpreting EISV dimensions |
| A3 beats A2; several leave-one-out arms degrade | Retain EISV pending causal utility testing |
| A3 does not beat A2; A4 is non-inferior to A2 | Retain EISV as compression/display, not unique policy evidence |
| Only one or two dimensions contribute | Unbundle and promote grounded dimensions individually |
| A3 does not beat A2; A4 is inferior | De-authorize EISV; retain versioned compatibility telemetry during migration |
| A6 adds no calibrated value | Remove legacy ODE/coherence authority first |
| Results vary sharply by maturity/source | Restrict authority to validated strata and sources |

Absence of evidence is not automatically evidence that a representation is
useless. If confidence intervals remain too wide after the preregistered sample,
the result is inconclusive and the next decision must state whether collecting
more data is worth the operational cost.

An `UNASSESSED` instrument status licenses no comparison. A passing positive
control establishes that the instrument can distinguish worlds; it does not
establish that EISV predicts outcomes or prevents harm.

## Phase II: causal soft-guidance pilot

Phase II is permitted only if Phase I shows predictive value. It will randomize
or step-wedge **soft guidance only** among low-risk, policy-eligible cases.

Hard safety floors, critical pauses, void conditions, and independent circuit
breakers are never randomized away. Candidate cases must have no hard trigger,
must fall inside a prespecified risk band, and must be assigned before guidance
is shown. The causal endpoints are severe adverse outcomes prevented per 1,000
episodes, unnecessary interventions, added latency, and recovery cost.

Phase II requires a separate preregistration because prediction and treatment
effects are different estimands.

## Required implementation checks

Before the pilot is enabled:

- validate every emitted record against the JSON Schema;
- enforce one complete set of 12 arm IDs per eligible episode;
- verify that `checkin_index` agrees with `maturity_stage`;
- verify that shared substrates and resident producers resolve to the same
  `independence_unit_id`;
- validate the independence registry has no substrate or producer-group key
  assigned to multiple independence units;
- verify schedule, wall-clock gap, process instance, monitor lineage, restart,
  hydration, formula-discontinuity, and authorship fields are present and
  cutoff-correct;
- reject any arm whose input snapshot hash differs from the episode snapshot;
- require `uses_post_cutoff_data=false` for every feature group;
- ensure only independent, severe events can count toward the primary outcome;
- reject primary labels whose producer is governance policy, lifecycle state,
  or agent self-report, even when their verification source says
  `server_observation`;
- verify prediction timestamps precede every linked outcome timestamp;
- test duplicate prediction and event IDs;
- test all censoring paths; and
- demonstrate that shadow scores cannot reach the production actuator;
- demonstrate policy-produced, delivered, suppressed, and applied-enforcement
  states remain distinguishable;
- run a domain-valid positive control with an absolute planted effect;
- replace each target mechanism with a no-op and require the associated control
  to fail; and
- require requested-but-skipped checks to return `UNASSESSED` with a non-success
  machine status;
- prove pilot instrumentation cannot read arm scores and outcomes together;
- prove unknown namespaces, repeated read IDs, early registered reads, and
  missing freeze hashes fail before data access; and
- validate every confirmatory fold uses a 24-hour-or-longer purge and keeps
  independence units and tasks on one side of the split.
- prove federated packages contain no raw episodes, arm scores, agent IDs, or
  local cluster identifiers;
- verify every federated package against a registry-bound site key and exact
  protocol/config/scorer fingerprint before using its counts;
- reject altered, duplicate, unregistered, or missing-required-site packages;
  and
- union shared substrate, producer, task, and declared shared-state domains
  across deployments before reporting cluster geometry or concentration.

## Amendments

Before the confirmatory freeze, amendments are allowed but must be versioned.
After freeze, any change to hypotheses, labels, arms, thresholds, exclusions,
sample size, or analysis creates a new protocol version. The original analysis
must still be reported unless a documented safety or integrity failure makes it
invalid.

### Version history

- `0.3.0` — adds the signed, privacy-preserving federated pilot exchange and
  conservative cross-site independence/task linkage contract. Federation
  remains score-free and carries no confirmatory or policy authority.
- `0.2.0` — adds failure-informed independence, continuity, label-provenance,
  policy-delivery, persistence-baseline, formula-versioning, and instrument-
  assessability requirements before pilot implementation.
- `0.1.0` — initial draft.
