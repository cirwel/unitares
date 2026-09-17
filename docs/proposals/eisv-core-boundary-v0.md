# EISV / core boundary: a neutral checkpoint spine with EISV as a versioned subscriber (v0)

**Status:** DRAFT proposal, 2026-09-17, revised the same day after an advisory
consult, a governed architecture review whose rejection still stands, and a Codex
pull-request review (Section 11). Documentation only. This document changes no runtime
behavior, schema, flag, default, threshold, response shape, or registered
protocol. It authorizes, at most, the two implementation stages named in
[Authorization](#13-authorization): an observational checkpoint seam and an
out-of-process shadow replay, both default-off, lossy, and non-authoritative.
**Scope:** how the accountability record (identity, checkpoint, ordering,
provenance, claims, evidence, objections, outcomes, artifact links) stops being
owned by the EISV state estimator, without disturbing the estimator during its
registered outcome-grounding read.
**Inputs:** the competitive-survival audit
([`../ontology/competitive-survival-audit-2026-09.md`](../ontology/competitive-survival-audit-2026-09.md), #2257);
its independent review
([`../plans/competitive-survival-audit-review-2026-09-17.md`](../plans/competitive-survival-audit-review-2026-09-17.md), #2258);
the Relay re-layering packet ([`relay-substrate-relayering-v0.md`](relay-substrate-relayering-v0.md), #2255);
the incident #2168 capture-stage comparison
([`../evaluations/accountability-journey/capture-stage-2168-v0.md`](../evaluations/accountability-journey/capture-stage-2168-v0.md), #2267);
and the outcome-grounding stop rule
([`eisv-outcome-grounding-stop-rule-v0.md`](eisv-outcome-grounding-stop-rule-v0.md), #1425).

---

## 1. Problem

The product documents say the state estimate interprets the record. The code
says the record is the state estimate. The #2258 review mapped this, and the
relevant facts were re-read at `master` for this proposal:

| Coupling | Where | Consequence |
|---|---|---|
| Every check-in runs the estimator, with no bypass | `execute_locked_update` calls `process_update_authenticated_async` (`src/mcp_handlers/updates/phases.py`); exceptions re-raise | A checkpoint cannot exist unless EISV succeeds |
| The persisted checkpoint row is an EISV row | `core.agent_state`: `entropy`, `integrity`, `volatility`, `coherence`, `regime`, `state_json`; keyed `(identity_id, recorded_at)` | The only durable "a check-in happened" record is an estimator output |
| The prediction key is minted and held by the estimator | `GovernanceMonitor.register_tactical_prediction` into the in-memory `_open_predictions` (`src/governance_monitor.py`) | Prediction-bound outcome grading hangs off monitor memory |
| Outcomes embed estimator state inline | `audit.outcome_events.eisv_e … eisv_regime`; `audit.outcome_prediction_bindings.canonical_eisv_snapshot` | An outcome is stored as an EISV observation, not as a fact about a checkpoint |
| Calibration is also graded without any prediction binding | `_post_update_auto_outcome` records calibration from the check-in's own `ctx.confidence` on auto-emitted outcomes (`phases.py`) | A second grading path exists with no prediction identity at all, so ownership of grading cannot be assigned by moving the registry alone |
| Write authority depends on the estimator | pause refuses the check-in and knowledge `store` / `note` (`check_agent_can_operate`, per #2258) | An unvalidated estimator gates part of the record it should only annotate |
| Export is estimator history | `export` writes the monitor's rolling EISV history, not claims (#2258) | No portable record exists without EISV |
| Post-update side effects are one fixed sequence | `execute_post_update_effects`: health/baselines → CIRS/drift → record state → save baseline → auto outcome → trajectory → phase-5 evidence → lineage | Record writes and estimator writes are interleaved |

Two further facts constrain any change:

1. **The registered 2026-12-01 read depends on this exact coupling, including
   its timing.** The ablation instruments join each outcome to the latest
   `core.agent_state` row at or before `o.ts − lead` and read
   `audit.outcome_events.eisv_*` (`scripts/analysis/eisv_skeptic_report.py`,
   imported by `eisv_ablation_matrix.py`). Keeping the columns and the query is
   not enough: a change to update order, lock duration, exception timing, or
   `recorded_at` can make that join select a different row near a boundary.
   Section 8.1 turns this into a tested contract.
2. **Capture, not EISV, is where the record failed on a real incident.** On
   #2168 the UNITARES arm recovered no positive fact about the unit, and the
   failure decomposed into a missing CI/git producer, nothing surfacing the
   guard's finding to a bound agent, and a connector path defect (#2267 §2).
   Decoupling does not repair capture. It removes the estimator as a
   precondition for capture, which is necessary for the repair and not
   sufficient.

## 2. Decision proposed

Make the **checkpoint** the unit of the record and `checkpoint_id` its living
key. A checkpoint is a neutral, ordered, attributed record that a unit of work
reached a boundary. Claims, evidence, objections, conditions, outcomes, and
artifact links attach to checkpoints. EISV becomes one **subscriber**: a
versioned, path-dependent fold over an identity's ordered checkpoints that
produces **advisory assessments** keyed by `checkpoint_id`. Enforcement becomes
a separate, explicitly enabled consumer of assessments.

EISV stays connected to trajectories. It stops owning them.

**What "living key" means.** A checkpoint is an **immutable occurrence**. It is
never edited, merged, or re-pointed. It is "living" only in the sense that
records keep attaching to it after it is written: a claim filed later, an
objection raised a week on, an outcome that lands after CI finishes. Claims and
outcomes are many-to-many attachments, not alternative spine keys, because
either can be absent, late, or plural for one unit of work.

**Distinct identities, never conflated:**

| Identity | Denotes |
|---|---|
| `attempt_id` | One submitted check-in, whatever happened to it |
| `checkpoint_id` | An accepted attempt; the occurrence records attach to |
| refusal / deferral occurrence | An attempt that a gate refused or deferred, with its class (Section 4.6) |
| `assessor_run_id` | One immutable fold of one assessor over one identity's checkpoints |
| `prediction_id` | A forecast attached to a checkpoint; zero or more per checkpoint |

The durable living key is a **Stage 3** artifact. Before the read, nothing
minted is durable or continuable: Stages 1 and 2 are instruments for testing
whether the contract below is complete, not the first rows of the future record.

## 3. Dependency direction

```
host hooks / SDK / adapters (plugin post-stop, Relay exporter, CI producer)
        │  append
        ▼
┌──────────────────────── core (record) ─────────────────────────┐
│ identity + lineage  ──  checkpoint (id, order, provenance)     │
│ claims · evidence refs · objections/conditions · outcomes      │
│ artifact links                                                 │
└───────────────┬────────────────────────────────────────────────┘
                │ checkpoint.recorded (ordered per identity)
                ▼
      ┌──────────────────────────┐     ┌─────────────────────────┐
      │ assessor adapters        │     │ exporters / bundle      │
      │  own their input snapshot│     │ (no assessor required)  │
      │  eisv-behavioral@N       │     └─────────────────────────┘
      │  (research variants)     │
      └─────────┬────────────────┘
                │ assessment.produced (advisory, keyed by checkpoint_id)
                ▼
      ┌─────────────────────┐
      │ enforcement (opt-in)│ reads assessments; may refuse writes;
      │ pause / circuit brk │ its decisions are recorded
      └─────────────────────┘
```

Rules:

- **Core imports nothing from EISV.** No module under the core boundary imports
  `governance_monitor`, `behavioral_state`, `governance_core` dynamics,
  calibration, or trajectory identity. A checkpoint is valid with zero
  assessors.
- **Core carries no assessor-specific fields.** What an assessor needs beyond
  neutral checkpoint facts (tool-usage windows, calibration scalars, baselines,
  clocks) is captured by that assessor's adapter into an **assessor input
  snapshot** that references `checkpoint_id` and is versioned by the adapter. A
  change to EISV's inputs changes the EISV adapter, never the checkpoint
  contract.
- **Assessors depend on core, never the reverse.** An assessor reads checkpoints,
  its own input snapshots, and its own prior assessments. It cannot write claims,
  outcomes, identity, or another assessor's state.
- **Enforcement depends on assessments and is off unless configured.** It is the
  only component allowed to turn an assessment into a refusal. Its decisions are
  recorded.
- **Outcomes bind to checkpoints, not to assessments.** An outcome row carries no
  assessor state; an analysis joins through `checkpoint_id` and names the
  assessor version it chose.

Target, not current: today every arrow above is reversed or fused. Section 7
states how the arrows move without breaking the read.

## 4. Event contract (v0)

Field names are proposals. The **target semantics** (Stage 3) and the
**pre-read semantics** (Stages 1 and 2) are stated separately, because the
review showed that a non-durable implementation cannot honestly promise the
target's guarantees.

### 4.1 `checkpoint.recorded.v0`

| Field | Target meaning (Stage 3) | Pre-read (Stages 1–2) |
|---|---|---|
| `checkpoint_id` | Durable, immutable id; the living key | Diagnostic only; not continuable, never referenced by any other record, never reused in Stage 3 |
| `idempotency_key` | Client- or adapter-supplied key; a retried or duplicated delivery of an accepted check-in maps to the existing checkpoint | Not provided; duplicates and retries are visible as separate diagnostic records |
| `identity` | `agent_uuid`, salted digest of `client_session_id`, `parent_agent_id`, `tier`, `proof_origin` | `agent_uuid` and `tier` / `proof_origin` only |
| `order` | `seq`: durable per-identity monotonic integer assigned in the same transaction as the checkpoint; `prev_checkpoint_id`; `recorded_at` (UTC, offset-bearing); `seq` is authoritative over any timestamp or UUID time component under clock skew or rollback | `seq` per server process, assigned **after** the per-agent lock is released, in the order the live path completed; resets on restart; `process_run_id` distinguishes runs; multi-instance serving of one identity is out of contract and flagged |
| `provenance` | `epistemic_class`, `producer` (name and version), `transport` | same |
| `report` | Retention policy decided by the operator (Section 12) | Nothing: no text, excerpt, or digest |
| `enforcement_regime` | Active gates and their configuration digest | Active gate names only |
| `attempt_id`, `kind` | Every attempt gets an `attempt_id`; `kind` is `accepted`, `refused`, `deferred`, or `failed` under the occurrence taxonomy (Section 4.6); only `accepted` mints a `checkpoint_id` | Emitted for every kind whose class is already known at an existing return point, as an O(1) enqueue adding no error-path work; kinds not observable that way are listed as unavailable |
| `links` | Cardinality-aware children: predictions, discoveries, dialectic sessions, artifact URIs | None |

### 4.2 Assessor input snapshot (owned by the assessor adapter)

Referenced by `checkpoint_id`; versioned by the adapter (`eisv-inputs.v0`). For
`eisv-behavioral` it lists every input the live estimator reads at update time
that the check-in does not carry. Each field states its **source**, **read
time**, and whether the read is **pure**.

The inventory is part of the deliverable, not an assumption. Known candidates
from `phases.py` and `governance_monitor.py`: the one-hour tool-usage window,
continuity metrics, the fleet-shared calibration scalar, the anomaly baseline,
restored behavioral baseline state, the clock (prediction TTL and gap handling),
and any random or cache-dependent path. An input the seam cannot see without
modifying estimator internals is **named as not captured**. Estimator internals
are not modified before the read to capture it.

**Consequence, stated plainly.** Several decisive inputs are created as locals
inside `GovernanceMonitor.process_update` and discarded: the wall-clock-derived
`effective_dt` and gap-saturation state, the raw one-hour tool-usage statistics,
and the continuity metrics. A seam outside that call cannot capture the values
the estimator actually used. Before the preservation horizon the snapshot is
therefore **partial by construction**, every assessment replayed from it is
`replayable: false`, and parity cannot be exact on any field those inputs reach.
Exact, same-point capture requires instrumenting the estimator, which waits for
the horizon (Section 12, decision 5).

### 4.3 `assessment.produced.v0`

| Field | Meaning |
|---|---|
| `assessment_id`, `checkpoint_id` | One assessment annotates exactly one checkpoint. |
| `assessor` | `name`, `version`, `epoch` (matches `core.agent_state.epoch` semantics), `config_digest` (thresholds, class calibration overlay, shadow/apply flags). |
| `run` | `assessor_run_id`, `resumed_from_assessment_id`, `discontinuity` marker. Any resume that is not bit-identical to the prior state mints a new `assessor_run_id`; runs are immutable and never merged. |
| `input_window` | `from_seq`, `to_seq`, `input_snapshot_version`. |
| `state` | E, I, S, V, Φ, coherence, risk, regime, warmup phase, baselined status, baseline counters; each with its provenance label (`measured`, `derived`, `prior`, `unknown`). |
| `advice` | `action`, `sub_action`, `reason`, `verdict_confidence`, `evidence_basis`. Advisory by definition. |
| `determinism` | `replayable` only when every inventoried input was captured; otherwise the missing inputs are named. |

### 4.4 `outcome.bound.v0` (target only)

`outcome_id`, `checkpoint_id`, the specific `prediction_id` child it grades,
outcome type and score, `verification_source`, producer identity, artifact URIs.
No assessor fields. Several predictions per checkpoint are allowed, because the
phase-5 evidence path can attach predictions to evidence rows as well as the
check-in's own. It does not always mint one per row: an evidence row carrying an
explicit `prediction_id` reuses it, and no id is minted when the check-in has no
confidence. Section 4.6 fixes those cases. Not produced in Stages 1 or 2.

### 4.5 Delivery semantics

**Target (Stage 3):** per-identity total order and exactly-once delivery to each
assessor, from a durable log with idempotent appends. EMA smoothing and the
Welford baseline are order-dependent folds: out-of-order or duplicated delivery
produces a different trajectory, not a noisy copy. A gap stalls that identity's
assessor and raises a named gap; skipping would silently change the fold.

**Pre-read (Stages 1–2):** best-effort, lossy, observational. The live request
path never blocks, retries, waits on, or reads the result of anything in this
proposal. Emission uses a bounded queue that drops on overflow and counts the
drops. The shadow replay stalls on a gap or a `process_run_id` change and
reports it; it never fills or skips. A shadow stream is labelled incomplete
whenever drops, gaps, or restarts occurred.

### 4.6 Occurrence taxonomy and prediction cardinality (fixed before data)

Both are **preconditions**, recorded in this document and confirmed by the
operator before Stage 1 code merges, and in any case before any shadow data is
collected. Leaving them to judgment after shadow observations exist would let
the observations choose the definitions.

Proposed taxonomy for attempt `kind`:

| Kind | Meaning | Example return point today |
|---|---|---|
| `accepted` | The estimator ran and the check-in was recorded | normal completion of `execute_post_update_effects` |
| `refused` | A policy gate declined the check-in | `AGENT_PAUSED` / circuit breaker |
| `deferred` | Accepted for later handling rather than now | none known today; listed so it is not invented later |
| `failed` | Validation, authentication, identity, cancellation, or infrastructure failure | identity refusal contracts, schema validation, estimator exception |

`failed` carries a sub-class from exactly that list. An attempt that fails
before an identity is resolved has no identity; it is recorded without one, not
attributed to a guessed identity.

Proposed prediction cardinality and matching:

- zero or more predictions per checkpoint; the check-in's advertised prediction
  is marked `advertised`, and evidence-row mints are marked `evidence_row` with
  their row index;
- an outcome grades exactly one prediction, named explicitly by
  `prediction_id`; an outcome without one attaches to its checkpoint as an
  ungraded outcome and is never matched to a prediction by time or recency;
- an evidence row that supplies an existing `prediction_id` attaches to that
  prediction and mints nothing; a reused id from another checkpoint is recorded
  as a cross-checkpoint reference, never duplicated;
- no confidence means no prediction; the checkpoint records that none was minted;
- one prediction is graded at most once (the existing exactly-once binding);
- grading without a prediction, such as calibration recorded from an
  auto-emitted outcome and the check-in's own confidence, is recorded as an
  **unbound grading** by the assessor that consumed it, never retroactively bound
  to a prediction.

## 5. Path dependence and policy feedback

Separating the estimator from the record does not make its outputs context-free.

1. **Ordering and baselines.** State at checkpoint *n* depends on every earlier
   accepted checkpoint of that identity, the warmup stage (cold-start prior,
   fixed thresholds, then self-relative scoring), and the baseline that the
   post-update phase saves. Two assessor runs are comparable only on the same
   input window from the same starting state.
2. **Side inputs are not in the check-in.** Section 4.2 makes them an adapter's
   responsibility and requires the snapshot to be the values the live estimator
   actually used, taken at one defined point, not a later reconstruction. Before
   the horizon that is not achievable for inputs local to `process_update`, and
   the snapshot says so rather than approximating them (Section 4.2).
3. **Policy feedback and selection.** Advice reaches the agent and changes its
   next report; a pause refuses later check-ins, removing checkpoints; the
   trajectory-identity enrichment adjusts the risk shown in the response without
   changing the persisted `risk_score`; enforcement also changes check-in timing
   and which outcomes become observable. Recording `enforcement_regime` makes the
   regime visible. **It does not neutralize the feedback**, and no analysis may
   treat regime tagging as a correction for selection.
4. **Retrospective path repair is prohibited.** A restored or re-derived prior
   state is a new assessor run (Section 4.3), never a continuation of the
   original, and never joined into the registered read.

Consequently, offline replay of an alternative assessor over a recorded stream
is a counterfactual about **estimation only**, never about behavior, because the
stream was generated under the live assessor's feedback.

## 6. Storage boundary

| Store | Owner after separation (target) | In Stages 1–2 |
|---|---|---|
| `core.identities`, `core.agents`, sessions, lineage | core | unchanged |
| checkpoint records | core (new, durable) | not in any database; diagnostic sink only |
| assessor input snapshots | the assessor adapter | diagnostic sink only |
| `knowledge.discoveries`, `core.dialectic_sessions` / messages, conditions | core (links to `checkpoint_id` later) | unchanged |
| `audit.outcome_events`, `audit.outcome_prediction_bindings` | core, without `eisv_*` columns | unchanged; `eisv_*` keep being written |
| `core.agent_state` | EISV (becomes the `eisv-behavioral` assessment store behind a compatibility view) | unchanged, keeps being written |
| agent baselines, class calibration overlay, calibration history | EISV | unchanged; nothing in this proposal writes them |
| in-memory prediction registry | core, as checkpoint children | unchanged |
| `audit.events` | core | unchanged; no new rows |

**Diagnostic sink governance (Stages 1–2).** An operator-local NDJSON file per
server process, outside the repository and the database, created with owner-only
permissions, rotated by size, and deleted when the parity plan's window closes.
It holds no report text or digest of it, no `client_session_id` or derivative,
no artifact URIs, and no outcome data. Input-snapshot values are included only
where the preregistered parity plan (Section 8.2) names them as necessary. It is
not exported, not surfaced to agents, not read by any tool, and not a record.

## 7. Compatibility strategy

| Stage | Change | Runtime effect | Gate to enter |
|---|---|---|---|
| **0** (this document) | Proposal and index row | None | Review in Section 11 |
| **1 — observational seam** | Behind a default-off flag, build diagnostic attempt occurrences, checkpoints and EISV input snapshots from values the live path has already computed; emit off-lock through a bounded dropping queue to the diagnostic sink | Flag off: none. Flag on: no work inside the per-agent lock; after release, a copy of already-computed values, id minting, and an enqueue; file append on a separate worker. Verified by Section 8.1 in CI; production equivalence is not claimed from fixtures, which is why the flag stays off on the read-feeding deployment (below) | The Section 8.1 harness exists and passes against current code; Section 4.6 confirmed by the operator |
| **2 — out-of-process shadow replay** | A separate process, never the governance server, replays the diagnostic stream through an isolated `eisv-behavioral` instance and compares with read-only extracts of live rows | None on the server | Stage 1 green on CI and the parity plan preregistered |
| **3 — ownership move** | Durable checkpoints with idempotency and `seq`, checkpoint children for predictions and links, outcomes by `checkpoint_id`, export bundle, `core.agent_state` behind a compatibility view that preserves the legacy join: the registered read selects the latest state across **every** `core.identities` row sharing `o.agent_id`, so Stage 3 needs an explicit `agent_id` → identity → checkpoint mapping and a tie policy for equal `recorded_at` | Schema and response additions | **Not authorized.** After the preservation horizon in Section 9, on an explicit operator decision |
| **4 — authority posture** | Enforcement explicitly configured; advisory by default for new installs | Default change | **Not authorized.** Separate operator decision after Stage 3 |

Compatibility commitments through every stage:

- Tool names, aliases, and response envelopes are unchanged by Stages 1 and 2.
  The flag is not agent-visible and not policy-visible.
- `prediction_id` keeps its current meaning. Stage 3 makes it a child of the
  checkpoint and keeps existing ids resolvable.
- `core.agent_state` and `audit.outcome_events.eisv_*` keep today's semantics
  until the preservation horizon (Section 9); afterwards any change bumps
  `epoch` and is announced as an instrument change.
- The Relay integration's `enforce` default is not changed here. The audit's
  recommendation to make it opt-in (#2257, seven-day action 1) remains a separate
  focused change and is consistent with Section 3.
- Stage 1 and 2 roll back by turning the flag off and deleting the sink; no
  other state exists to unwind.

**Where the flag may be on before the preservation horizon.** Stage 1 and
Stage 2 code may merge with the flag off. Before the horizon it may be enabled
only in CI and on deployments whose data cannot enter the 2026-12-01 read.
Enabling it on the deployment that feeds the read waits for the horizon or an
explicit, recorded operator waiver. What is frozen is the registered instrument
(the estimator, its persisted rows, their timing, and the registered join), not
the repository. The governed review preserved a stricter position, that no
Stage 1 code should merge before the horizon at all (Section 11.2); that
disagreement is an open operator decision (Section 12).

## 8. Verification

### 8.1 Instrument-preservation contract (Stage 1 gate)

Stage 1 must show, with the flag off and on, over a fixture sequence covering
warmup transitions, a pause, a restart with state restore, concurrent
check-ins for one identity, several `core.identities` rows sharing one
`agent_id`, and a check-in gap long enough to saturate the elapsed-time scaling:

- identical tool responses;
- identical rows, row counts, and column values in `core.agent_state`,
  `audit.outcome_events` (including every `eisv_*` value),
  `audit.outcome_prediction_bindings`, and `audit.events`;
- identical post-update effect order and transaction boundaries;
- identical `recorded_at` ordering and identical selection by the registered
  join at `lead` 0 and 30 minutes for every fixture outcome;
- identical estimator inputs as read at update time, identical EMA and Welford
  baseline update values, and identical policy decisions (action, sub_action,
  pause state) for every fixture check-in;
- identical results from the registered read's query functions run against the
  fixture database;
- identical failure behavior when the estimator raises, when PostgreSQL or Redis
  is unavailable, and when the diagnostic sink is full, unwritable, or raises:
  the enabled path is fail-open and can never delay, suppress, retry, or mutate
  a legacy write;
- identical `epoch`, prediction registry contents, TTL state, baseline counters
  and cache contents, and identical outputs for the next *N* updates after the
  fixture ends (so delayed divergence is caught);
- on a deployment that does not feed the registered read, before and after
  enabling the flag, a comparison of check-in latency distribution and of the
  lock hold time.

The harness for every item except the last is written **first, against current
code, with no seam present** (Section 14). It is the baseline Stage 1 must then
match. The latency comparison uses no outcome data.

### 8.2 Shadow parity (Stage 2)

Parity asks whether an isolated replay reproduces the **current
implementation**. Agreement is fidelity to that implementation, not correctness:
a shared bug or wrong ordering replicates perfectly. It therefore has two
separate parts.

**Parity plan, preregistered before any live shadow data is read**, and after
the occurrence taxonomy and prediction matching rules in Section 4.6 are
confirmed: corpus and
window, assessor and adapter versions, per-field tolerances, mismatch taxonomy
(ordering, missing input, clock, restart, drop or gap, unexplained), stopping
rule, owner, and the Stage 3 blocking rule. Discrete fields (action,
sub_action, regime, warmup phase, epoch, baselined status, baseline counters)
require exact equality; continuous fields have per-field tolerances, and any
continuous difference that straddles a decision threshold counts as a mismatch.

**Scientific firewall.** Parity work never reads outcome rows, labels, the
ablation instruments' outputs, or any operational-success signal, and no parity
decision (tolerance, version, taxonomy, or which assessor run to trust) may be
chosen by reference to them. The plan and its results are not inputs to the
registered read.

**Equivalence runs.** Offline fixtures in CI and a live diagnostic stream
replayed out of process (before the preservation horizon, only from a deployment
that does not feed the registered read; Section 7), reported as agreement rates by mismatch class, with
incomplete streams (drops, gaps, restarts) reported separately and never used
as evidence of agreement.

**Independent contract tests.** Hand-specified fold fixtures that check
ordering, restart, and warmup behavior against the contract in Section 4.5,
independent of the current implementation's output.

Unexplained mismatches block Stage 3. Explained mismatches are the inventory of
what the contract is missing.

## 9. Scientific validation

The separation is an engineering change to where records live. **Neither the
spine nor parity establishes EISV validity, predictive value, causal relevance,
calibration, or authority.** All remain governed by the stop rule and the
contract's tested-claims ledger.

- **The registered read is untouched.** The live estimator, its thresholds, its
  persisted rows, its timing, and the join the read uses do not change. No
  shadow output enters the read's cohort.
- **Preservation horizon.** The fields, semantics, and timing the read depends
  on stay unchanged until the operator declares the read's report final,
  including any correction or reproduction inspection, not merely until it is
  first reported. The read already writes an access receipt. Before Stage 3
  starts, the read's inputs are preserved as an immutable extract: source query
  revision, row extract and hash, labels, leads, and analysis environment. This
  preserves inspectability; it authorizes no re-run.
- **Selection is metadata, not a correction.** The read's report names the
  enforcement exposure, deployment dates, and censoring by pause for its cohort
  (Section 5.3).
- **Pre-read capture repairs are a separate hazard.** #2267 names capture
  repairs such as a CI/git producer. A repair that adds or changes outcome
  producers before the read changes outcome availability and possibly the label
  pipeline. Such repairs are outside this proposal and need their own check
  against the stop rule.
- **Versioned assessors make later evaluation cleaner, not easier to pass.**
  Evaluations name the assessor, adapter, run, epoch, input window, and regime
  tuple, and never pool across them.
- **A FAIL at the read does not retire the boundary.** The spine is justified by
  the accountability record; EISV continues as label-free telemetry either way.
- **A PASS does not authorize enforcement.** Authority posture remains Stage 4's
  separate decision.

## 10. Non-goals

- Physically extracting EISV into another package, service, or repository.
- Any schema migration, table, column, index, or constraint change.
- Any default change, including verdict thresholds, pause delivery, the circuit
  breaker, and the Relay `enforce` default.
- Modifying estimator internals, new EISV features, estimator repairs, re-fits,
  or re-runs of any registered or withdrawn analysis.
- Durable ids, idempotency, or cross-instance ordering before Stage 3.
- Recording refused or failed check-ins before the operator decides their
  taxonomy.
- Changing identity tier policy (#807) or the identity/onboarding surface.
- The portable accountability bundle itself. The spine is its substrate; the
  bundle stays its own design.
- A CI or git producer for the record, and any other capture repair.
- Retiring the ODE path, renaming tools, or reducing the alias table.
- An issue tree. Exactly one first implementation issue is proposed.

## 11. Review record

### 11.1 Advisory consult (architecture and science)

One `consult` (purpose `critique`, effort `thorough`, consultation
`93fbdd25-aea9-4b7e-9cd7-770cc429bd94`) against the first draft returned 18
findings, 5 marked blocking, plus 9 omissions. Its authority is advisory; it is
not a governed review. Dispositions:

| # | Finding (severity) | Disposition |
|---|---|---|
| 1 | In-process `seq` and per-process NDJSON cannot deliver exactly-once or durable order (blocking) | **Accepted.** Stages 1–2 are best-effort and observational; exactly-once, idempotency, and durable `seq` moved to Stage 3 (4.1, 4.5). |
| 2 | "Gaps block" contradicts "no runtime change" (blocking) | **Accepted.** The live path never blocks or retries; only the out-of-process replay stalls; lossy streams labelled (4.5). |
| 3 | Non-durable ids cannot become the living key (blocking) | **Accepted, first option.** Pre-read ids are diagnostic-only and never continued (2, 4.1). The alternative, a durable append boundary before the read, is **rejected** as a schema change inside the read window. |
| 4 | The "neutral" checkpoint embedded EISV inputs, recreating the reverse dependency (major) | **Accepted.** Inputs moved to an adapter-owned, versioned input snapshot (3, 4.2). |
| 5 | A shadow can perturb the live instrument without writing (blocking) | **Accepted.** Stage 2 runs out of process; Stage 1 copies only already-computed values and emits off-lock; latency and lock-hold measured (7, 8.1). |
| 6 | Side-input snapshot timing unspecified (major) | **Accepted.** One snapshot point, per-field source, read time, purity; uncapturable inputs named rather than captured by modifying internals (4.2). |
| 7 | Parity tuning could become an outcome proxy (major) | **Accepted.** Preregistered parity plan and a scientific firewall (8.2). |
| 8 | Regime tagging does not protect against selection (major) | **Accepted.** Stated as metadata, not correction (5.3, 9). |
| 9 | Unchanged columns and joins do not preserve the instrument (blocking) | **Accepted.** Instrument-preservation contract as the Stage 1 gate (8.1). |
| 10 | Refusal capture premature and ambiguous (major) | **Accepted at first, then reversed in part by the governed review (11.2, condition 5).** The taxonomy is now fixed before code (4.6) and Stage 1 records refusals only where the class is already known at an existing return point, with no added error-path work. The consult's concern about changing error-path work is kept as that constraint. |
| 11 | Single `prediction_id` field loses phase-5 multiplicity (major) | **Accepted.** Cardinality-aware children in the target; excluded from Stages 1–2 (4.1, 4.4). |
| 12 | Diagnostic file is an ungoverned behavioral log (major) | **Accepted.** Minimization, permissions, rotation, deletion, no text or session derivatives (6). |
| 13 | Ownership asserted without enforcement (minor) | **Partially accepted.** Stage 1 adds an import-boundary test; the module ownership map, mutation permissions, and forbidden cross-store transaction checks belong to Stage 3 and are named there, not built now. |
| 14 | Live output is not an oracle (major) | **Accepted.** Parity reported as agreement with the implementation, plus independent contract tests (8.2). |
| 15 | Float tolerance near discontinuities (minor) | **Accepted.** Exact equality for discrete fields; threshold-straddling differences are mismatches (8.2). |
| 16 | Retrospective path repair (major) | **Accepted.** Immutable runs, new run id on any non-identical resume, never joined into the read (4.3, 5.4). |
| 17 | "After the read is reported" is too weak (major) | **Accepted.** Preservation horizon until the operator declares the report final, plus an immutable read extract (9). |
| 18 | "Identical monitor state" not observable (minor) | **Accepted.** Enumerated state plus the next *N* updates (8.1). |

Omissions: idempotency and retry, lock and transaction boundary, `recorded_at`
and clock semantics, flag scoping and rollback, the scientific firewall, sink
governance, and the validity disclaimer are now addressed in 4.1, 4.5, 6, 7,
8.1, 8.2, and 9. **Pre-read capture repairs** are recorded as a separate hazard
(9) and left to their own check. The **complete estimator input inventory** is
not produced by this document: it is a deliverable of the first implementation issue (14), because
producing it by reading the code now would be the unverified description the
parity test exists to replace.

### 11.2 Governed architecture review

One dialectic `review` session, `134b6a9e32abf9f9`, opened 2026-09-17 against
commit `226cbdc6`. The server spawned an independent reviewer
(`DialecticReviewer_439075bb`, Codex backend, not degraded). Its verdict was
**`agrees=false`** with five conditions. The proposal's author replied once
(`agrees=false`) with the dispositions below. **The session is not resolved**,
and this document does not treat it as resolved: the reviewer's rejection
stands until the reviewer or the operator acts on it.

Reviewer's root cause, accepted as stated: occurrence capture, estimator state
transition, prediction identity, outcome attribution, and policy authority are
conflated in one path-dependent transaction, so refusal, repeated predictions,
and policy-caused censorship are unrepresentable or ambiguous.

| # | Reviewer condition | Disposition |
|---|---|---|
| 1 | Before the preservation horizon, merge only documentation and a preregistered implementation contract; no Stage 1 runtime code, flags, or hooks until an operator-declared preservation release | **Disputed; standing disagreement.** Accepted that the draft contradicted itself (its own thesis condition forbade runtime change before the horizon while authorizing Stage 1) and that default-off code can still alter imports, scheduling, and failure paths. Not accepted that Stage 1 code must wait: the mandate for this proposal authorizes a neutral seam and a shadow implementation. Resolution proposed by the author: code merges flag-off; the flag is enabled before the horizon only in CI and on deployments that cannot feed the read; the read-feeding deployment waits for the horizon or a recorded operator waiver (Section 7). The first issue is the preservation harness against current code, which contains no seam (Section 14). Whether that suffices is an **operator decision** (Section 12). |
| 2 | `checkpoint_id` is an immutable occurrence key, with distinct attempt, accepted-checkpoint, refusal/deferral, and assessor-run identities; claims and outcomes attach many-to-many | **Accepted** (Section 2). |
| 3 | Instrument preservation mechanically testable: writes, timestamps and order, estimator inputs and EMA/Welford updates, policy decisions, query results, failure behavior; enabled path fail-open and unable to delay, suppress, retry, or mutate legacy writes | **Accepted** (Section 8.1). |
| 4 | Preregister prediction cardinality and outcome matching before any shadow data | **Accepted** (Section 4.6). |
| 5 | Stage 1 emits distinguishable attempted, refused, and deferred occurrences or shows why they are unavailable | **Accepted with a constraint** (Sections 4.1, 4.6): only where the class is known at an existing return point, with no added error-path work; unavailable kinds are listed. This reverses in part the consult's finding 10. |

### 11.3 Pull-request review by Codex

An explicit Codex review of PR #2278 at commit `226cbdc6` (`codex exec`,
read-only sandbox, spawned through the agent orchestrator, exit 0) returned
**`VERDICT: FINDINGS(5)`**. Each finding was checked against the cited source
before disposition; all five hold.

| # | Finding | Disposition |
|---|---|---|
| 1 | The seam cannot capture the inputs the estimator used: `effective_dt`, raw tool-usage statistics, and continuity metrics are locals inside `GovernanceMonitor.process_update`, so "same-point snapshot" contradicts "not captured" | **Accepted.** The pre-read snapshot is declared partial by construction, replays are `replayable: false`, parity cannot be exact on fields those inputs reach, and exact capture waits for post-horizon instrumentation (4.2, 12). |
| 2 | Work inside the per-agent lock changes contention, and the estimator's elapsed-time scaling depends on wall time, so enabling Stage 1 can move later `recorded_at` values and transitions; fixtures cannot prove production equivalence | **Accepted.** Stage 1 does no work inside the lock (4.1, 7); fixtures cover a saturating gap (8.1); production equivalence is not claimed, which reinforces keeping the flag off on the read-feeding deployment (7). |
| 3 | The registered join selects the latest state across every identity row sharing `o.agent_id`, so per-identity checkpoints need an explicit legacy mapping and tie policy | **Accepted.** Added to Stage 3's compatibility requirement (7) and to the 8.1 fixtures. |
| 4 | Phase 5 does not always mint one prediction per evidence row: explicit ids are reused, and nothing is minted without confidence | **Accepted.** Statement corrected (4.4); reuse and no-confidence rules fixed (4.6). |
| 5 | Prediction binding is not the only grading path: auto outcomes record calibration from the check-in's own confidence with no prediction id | **Accepted.** Coupling table corrected (1); unbound grading defined and assigned to the consuming assessor (4.6). |

## 12. Open operator decisions

1. **Sequencing before the horizon (standing disagreement).** The governed
   reviewer holds that only documentation and a preregistered implementation
   contract may merge before the preservation horizon. This proposal holds that
   Stage 1 and 2 code may merge flag-off and run in CI and on deployments that
   cannot feed the read, with the read-feeding deployment waiting for the horizon
   or a recorded waiver. Both positions are recorded in 11.2; the operator
   chooses.
2. **Occurrence taxonomy and prediction matching (Section 4.6).** Confirm or
   amend before Stage 1 code merges.
3. **Parity standard.** Tolerances, window, stopping rule, and the acceptable
   explained-mismatch rate, fixed in the preregistered parity plan. Given 4.2,
   decide whether a partial, non-replayable parity before the horizon is worth
   running at all, or whether Stage 2 waits for instrumentation.
4. **Report-text retention.** The report is transient today and truncated in
   excerpts (#2267 §4.3). The target can carry nothing, a salted digest, an
   excerpt, or the full text under a retention policy.
5. **Estimator instrumentation after the horizon.** Whether to instrument
   `process_update` so the input snapshot is exact, bumping `epoch`.
6. **Preservation horizon.** What counts as the read's report being final.
7. **Post-read authority posture** for the maintainer deployment and for new
   installs, taken after Stage 3 rather than bundled with it.

## 13. Authorization

Merging this document authorizes Stage 1 and Stage 2 only: default-off, lossy,
observational, non-authoritative, with no database write, no default change, no
response change, no work inside the per-agent lock, and no modification of
estimator internals. Before the preservation horizon the flag may be enabled only
in CI and on deployments that cannot feed the 2026-12-01 read, unless the operator
records a waiver. Stage 1 code does not merge before the harness in Section 14
exists and the Section 4.6 rules are confirmed. If the operator adopts the
reviewer's stricter sequencing (Section 12, decision 1), this authorization
narrows to that harness and the documentation until the horizon. Stage 3 and
Stage 4 require a new, explicit operator decision recorded against this document
after the horizon.

## 14. First implementation issue

**Title:** Instrument-preservation harness for the check-in path: pin current
EISV writes, timing, inputs, decisions, and registered-join selection in CI

**Why this first.** Both reviews converge on it. It changes no runtime code,
contains no seam and no flag, and so is acceptable under either sequencing
position in Section 12. Every later stage is gated on matching it, and it is the
only way to show that a seam changed nothing.

**Scope (tests and fixtures only):**

- A fixture sequence through the real check-in path covering: warmup
  transitions into fixed thresholds and self-relative scoring; a pause and a
  refused follow-up; a restart with state restore; concurrent check-ins for one
  identity; several `core.identities` rows sharing one `agent_id`; a gap long
  enough to saturate elapsed-time scaling (with the clock injected, not slept);
  phase-5 evidence rows with and without explicit `prediction_id` and with no
  confidence; and an auto-emitted outcome that records calibration.
- Canonicalized captures of: tool responses; rows and values in
  `core.agent_state`, `audit.outcome_events` (every `eisv_*`),
  `audit.outcome_prediction_bindings`, and `audit.events`; post-update effect
  order; policy decisions; the prediction registry, TTL state, baseline counters
  and caches; calibration records; and outputs of the next *N* updates.
- The registered read's own query functions from
  `scripts/analysis/eisv_skeptic_report.py` run against the fixture database,
  asserting which state row each fixture outcome selects at `lead` 0 and 30. No
  production data and no outcome discrimination statistics are read or computed.
- Failure-path fixtures: estimator exception, PostgreSQL unavailable, Redis
  unavailable.
- A written inventory, committed with the harness, of every estimator input read
  at update time, marking which are locals inside `process_update`.

**Acceptance:** the harness passes deterministically on `master` in CI (no
wall-clock or ordering flakiness across repeated runs); it fails when any pinned
value is perturbed in a deliberate mutation check; it adds no runtime import,
flag, or behavior.

**Out of scope:** the seam, the flag, the diagnostic sink, the shadow replay, and
any durable identifier.
