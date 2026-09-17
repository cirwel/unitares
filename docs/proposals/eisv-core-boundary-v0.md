# EISV / core boundary: a neutral checkpoint spine with EISV as a versioned subscriber (v0)

**Status:** DRAFT proposal, 2026-09-17. Documentation only. This document
changes no runtime behavior, schema, flag, default, threshold, response shape,
or registered protocol. It authorizes, at most, the two implementation stages
named in [Authorization](#authorization): a neutral checkpoint seam and a shadow
subscriber, both default-off and non-authoritative.
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
| The prediction key is minted and held by the estimator | `GovernanceMonitor.register_tactical_prediction` into the in-memory `_open_predictions` (`src/governance_monitor.py`) | Outcome binding, the record's only grading mechanism, hangs off monitor memory |
| Outcomes embed estimator state inline | `audit.outcome_events.eisv_e … eisv_regime`; `audit.outcome_prediction_bindings.canonical_eisv_snapshot` | An outcome is stored as an EISV observation, not as a fact about a checkpoint |
| Write authority depends on the estimator | pause refuses the check-in and knowledge `store` / `note` (`check_agent_can_operate`, per #2258) | An unvalidated estimator gates part of the record it should only annotate |
| Export is estimator history | `export` writes the monitor's rolling EISV history, not claims (#2258) | No portable record exists without EISV |
| Post-update side effects are one fixed sequence | `execute_post_update_effects`: health/baselines → CIRS/drift → record state → save baseline → auto outcome → trajectory → phase-5 evidence → lineage | Ordering of record writes and estimator writes is interleaved |

Two further facts constrain any change:

1. **The registered 2026-12-01 read depends on this exact coupling.** The
   ablation instruments join each outcome to the latest `core.agent_state` row
   at or before `o.ts − lead` and read `audit.outcome_events.eisv_*`
   (`scripts/analysis/eisv_skeptic_report.py`, imported by
   `eisv_ablation_matrix.py`). Changing what, when, or how those rows are written
   before the read is instrument drift.
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
      ┌─────────────────────┐          ┌─────────────────────────┐
      │ assessors           │          │ exporters / bundle      │
      │ eisv-behavioral@N   │          │ (no assessor required)  │
      │ (research variants) │          └─────────────────────────┘
      └─────────┬───────────┘
                │ assessment.produced (advisory, keyed by checkpoint_id)
                ▼
      ┌─────────────────────┐
      │ enforcement (opt-in)│ reads assessments; may refuse writes;
      │ pause / circuit brk │ its refusals are recorded as checkpoints
      └─────────────────────┘
```

Rules:

- **Core imports nothing from EISV.** No module under the core boundary imports
  `governance_monitor`, `behavioral_state`, `governance_core` dynamics,
  calibration, or trajectory identity. A checkpoint is valid with zero
  assessors.
- **Assessors depend on core, never the reverse.** An assessor reads checkpoints
  and its own prior assessments. It cannot write claims, outcomes, identity, or
  another assessor's state.
- **Enforcement depends on assessments and is off unless configured.** It is
  the only component allowed to turn an assessment into a refusal. Its
  decisions are themselves recorded, so the record shows what the gate
  suppressed.
- **Outcomes bind to checkpoints, not to assessments.** An assessment may be
  graded by an outcome through the checkpoint it annotated. The outcome row does
  not carry assessor state.

Target, not current: today every arrow above is reversed or fused. Section 7
states how the arrows move without breaking the read.

## 4. Event contract (v0)

Three events. Field names are proposals; types are normative for the shadow
stage.

### 4.1 `checkpoint.recorded.v0`

| Field | Meaning |
|---|---|
| `checkpoint_id` | Server-minted, immutable, time-ordered UUID (v7). The living key. Minted once per accepted check-in, inside the per-agent lock, before any assessor runs. |
| `identity` | `agent_uuid`, `client_session_id` digest (never the raw string), `parent_agent_id` when declared, identity `tier` and `proof_origin` as resolved. |
| `order` | `seq` (per-identity monotonic integer assigned under the agent lock), `prev_checkpoint_id`, `recorded_at` (UTC, offset-bearing), `server_instance`. |
| `provenance` | `epistemic_class` (existing five values), `producer` (hook, SDK, adapter name and version), `transport` (`mcp`, `rest`, `stdio`). |
| `report` | `digest` of the submitted report text, `retention` (`transient` today), declared `complexity` / `confidence` as submitted. |
| `side_inputs` | Digest and, in shadow, the values of every non-checkpoint input the current estimator reads at update time (tool-usage window, continuity metrics, fleet calibration scalar, anomaly baseline, clock). |
| `enforcement_regime` | Which gates were active when this checkpoint was accepted (`none`, `circuit_breaker`, …) and the gate configuration digest. |
| `status` | `accepted` or `refused` (with the refusing component and reason). A refused check-in is still a checkpoint. |
| `links` | Optional references: `prediction_id`, discovery ids, dialectic session ids, artifact URIs (commit SHA, PR, CI run, issue). |

### 4.2 `assessment.produced.v0`

| Field | Meaning |
|---|---|
| `assessment_id`, `checkpoint_id` | The assessment annotates exactly one checkpoint. |
| `assessor` | `name` (`eisv-behavioral`), `version`, `epoch` (matches `core.agent_state.epoch` semantics), `config_digest` (thresholds, class calibration overlay, shadow/apply flags). |
| `input_window` | `from_seq`, `to_seq`, `prior_assessment_id`: the path this fold consumed. |
| `state` | E, I, S, V, Φ, coherence, risk, regime, warmup phase and baselined status, each with its provenance label (`measured`, `derived`, `prior`, `unknown`). |
| `advice` | `action`, `sub_action`, `reason`, `verdict_confidence`, `evidence_basis`. Advisory by definition. |
| `determinism` | `replayable` true only when every side input was captured; otherwise the missing inputs are named. |

### 4.3 `outcome.bound.v0`

`outcome_id`, `checkpoint_id` (the checkpoint whose prediction is graded),
`prediction_id`, outcome type and score, `verification_source`, producer
identity, and artifact URIs. No assessor fields. An analysis that needs the
assessor state at grading time joins through `checkpoint_id` to the assessment
it wants, and names the assessor version it chose.

### 4.4 Delivery semantics

- **Per-identity total order.** Assessors consume an identity's checkpoints in
  `seq` order, exactly once. EMA smoothing and the Welford baseline are
  order-dependent folds; out-of-order or duplicated delivery produces a
  different trajectory, not a noisy copy of the same one.
- **Gaps block, they do not skip.** A missing `seq` stalls that identity's
  assessor and raises a named gap. Skipping would silently change the fold.
- **Restart is part of the contract.** Today the monitor restores from
  persisted state and baselines. An assessor declares its resume point as
  `prior_assessment_id`; a restart that resumes from a different point is a
  different path and must be labelled so.
- **Refusals are ordered like accepted checkpoints.** An assessor declares
  whether it folds refused checkpoints. The current estimator never sees them,
  so `eisv-behavioral@current` declares `folds_refused: false`.

## 5. Path dependence and policy feedback

Separating the estimator from the record does not make its outputs context-free.
Three effects must stay visible in the design rather than be lost at the seam:

1. **Ordering and baselines.** State at checkpoint *n* depends on every earlier
   accepted checkpoint of that identity, the warmup stage (cold-start prior,
   fixed thresholds, then self-relative scoring), and the baseline that the
   post-update phase saves. Two assessors, or the same assessor on two
   deliveries, are comparable only on the same `input_window`.
2. **Side inputs are not in the check-in.** The current update reads live
   state outside the report: the one-hour tool-usage window, continuity
   metrics, the fleet-shared calibration scalar, the anomaly baseline, and the
   clock (prediction TTL and gap handling). A replay without those values is a
   different instrument. The contract therefore captures them as `side_inputs`
   and marks assessments non-replayable when any are absent.
3. **Policy feedback.** Advice reaches the agent and changes its next report; a
   pause refuses later check-ins and so removes checkpoints from the stream; the
   trajectory-identity enrichment adjusts the risk shown in the response
   without changing the persisted `risk_score`. The checkpoint stream is
   therefore shaped by the enforcement regime that was active. Consequences:
   - `enforcement_regime` is recorded on every checkpoint;
   - assessments produced under different regimes are never pooled;
   - offline replay of an alternative assessor over a recorded stream is a
     counterfactual about **estimation only**, never about behavior, because
     the stream was generated under the live assessor's feedback.

## 6. Storage boundary

| Store | Owner after separation | In this proposal's authorized stages |
|---|---|---|
| `core.identities`, `core.agents`, sessions, lineage | core | unchanged |
| checkpoint records | core (new) | not persisted to the database; shadow sink only |
| `knowledge.discoveries`, `core.dialectic_sessions` / messages, conditions | core (links to `checkpoint_id` added later) | unchanged |
| `audit.outcome_events`, `audit.outcome_prediction_bindings` | core, without `eisv_*` columns in the target | unchanged; `eisv_*` keep being written through the read |
| `core.agent_state` | EISV (becomes the `eisv-behavioral` assessment store; later exposed through a compatibility view) | unchanged, keeps being written |
| agent baselines, class calibration overlay, calibration history | EISV | unchanged; shadow must not write them |
| in-memory prediction registry | core (keyed by `checkpoint_id`) in the target | unchanged |
| `audit.events` | core | unchanged; shadow emits no rows |

The shadow sink is an append-only, operator-local NDJSON file per server
instance, outside the repository and outside the database, behind a flag that
defaults off. It is deliberately not durable infrastructure: the point of the
shadow stage is to test the contract, not to create a second record.

## 7. Compatibility strategy

Staged so that nothing an agent, adopter, analysis, or registered read depends on
moves before the operator decides it should.

| Stage | Change | Runtime effect | Gate to enter |
|---|---|---|---|
| **0** (this document) | Proposal and index row | None | Review in Section 11 |
| **1 — neutral seam** | Build a `CheckpointRecord` inside the agent lock after identity resolution and before `process_update_authenticated_async`; capture `seq`, `prev_checkpoint_id`, side inputs, regime; hand it to a no-op sink by default | None with the flag off; with it on, only the shadow sink file is written | Stage-1 issue accepted |
| **2 — shadow subscriber** | An isolated `eisv-behavioral` assessor instance consumes the seam's stream and writes assessments to the shadow sink; parity harness compares them with live rows | Reads live state; writes only the shadow sink | Stage-1 no-behavior-change test green on CI |
| **3 — ownership move** | `checkpoints` table, `checkpoint_id` on outcomes, claims and dialectic links, export bundle from the record, `core.agent_state` behind a compatibility view | Schema and response additions | **Not authorized.** After the 2026-12-01 read is reported, and on an explicit operator decision |
| **4 — authority posture** | Enforcement explicitly configured; advisory by default for new installs | Default change | **Not authorized.** Separate operator decision after Stage 3 |

Compatibility commitments that hold through every stage:

- Tool names, aliases, and response envelopes are unchanged by Stages 1 and 2.
- `prediction_id` keeps its current meaning. In Stage 3 it becomes a field of
  the checkpoint, and existing ids stay resolvable.
- `core.agent_state` and `audit.outcome_events.eisv_*` keep being written with
  today's semantics at least until the 2026-12-01 read is reported; afterwards
  any change bumps `epoch` and is announced as an instrument change.
- The Relay integration's `enforce` default is not changed here. The audit's
  recommendation to make it opt-in (#2257, seven-day action 1) remains a
  separate focused change and is consistent with Section 3.

## 8. Shadow parity test

Parity asks one question: **given the checkpoint stream the seam captures, does
an isolated subscriber reproduce the live estimator?** If it cannot, the event
contract is missing an input and Stage 3 would silently change the instrument.

**Isolation requirements (a failing isolation check fails the test):**

- the shadow assessor has its own monitor, behavioral state, and baseline
  objects; module singletons it would otherwise share (the calibration checker,
  the tool-usage tracker, the baseline cache) are read through a snapshot and
  never mutated;
- no writes to PostgreSQL, Redis, the knowledge graph, calibration files, or
  `audit.events`; enforced by running the shadow path with those clients
  replaced by write-refusing doubles in tests, and by a live-mode assertion on
  connection usage;
- no access to outcome rows. Parity compares assessment to assessment. It never
  joins outcomes, so it cannot become an interim outcome-discrimination read
  under the stop rule.

**Two layers:**

1. **Offline, deterministic (CI).** Recorded fixture streams, including warmup
   boundaries, a restart with state restore, a pause and refusal, a gap, and a
   concurrent-check-in race, run through the live update path and the shadow
   path. Expected: identical `action` and `sub_action`; identical warmup phase;
   floats equal within a declared tolerance; every mismatch attributed to a named
   missing side input.
2. **Live shadow (operator deployment, flag on).** Dual-run over a fixed window.
   Mismatches are classified as ordering, side-input, clock, restart, refused
   checkpoint, or unexplained. Unexplained mismatches block Stage 3.

The tolerance, the live window length, and the acceptable rate of explained
mismatches are **operator decisions** (Section 12), fixed before the live shadow
runs and not revised after its output is seen.

## 9. Scientific validation

The separation is an engineering change to where records live. It is not
evidence about EISV and must not be reported as such.

- **The registered read is untouched.** The live estimator, its thresholds, its
  persisted rows, and the join the read uses do not change before the read is
  reported. Shadow output is never part of the read's cohort.
- **The claim status does not move.** Outcome grounding stays governed by the
  stop rule and the contract's tested-claims ledger; this document neither
  strengthens nor weakens any row. Parity is a fidelity property of the
  instrument's implementation, not validity of what it measures.
- **Versioned assessors make future evaluation cleaner, not easier to pass.**
  Every assessment carries assessor version, epoch, config digest, input window,
  and enforcement regime. Evaluations name those tuples and never pool across
  them. An alternative assessor evaluated offline over recorded streams inherits
  Section 5's limit: estimation counterfactual only.
- **A FAIL at the read does not retire the boundary.** Under the stop rule a
  FAIL closes scheduled outcome-prediction reads. The spine is justified by the
  accountability record regardless of the read's result, and EISV continues as
  label-free telemetry either way.
- **A PASS does not authorize enforcement.** It reopens outcome-grounding
  questions; authority posture remains Stage 4's separate decision.

## 10. Non-goals

- Physically extracting EISV into another package, service, or repository.
- Any schema migration, table, column, index, or constraint change.
- Any default change, including verdict thresholds, pause delivery, the
  circuit breaker, and the Relay `enforce` default.
- New EISV features, estimator repairs, re-fits, or re-runs of any registered or
  withdrawn analysis.
- Changing identity tier policy (#807) or the identity/onboarding surface.
- The portable accountability bundle itself. The spine is its substrate; the
  bundle stays its own design.
- A CI or git producer for the record. #2267 names it as a capture repair; it is
  enabled by `artifact links` but not designed here.
- Retiring the ODE path, renaming tools, or reducing the alias table.
- An issue tree. Exactly one first implementation issue is proposed.

## 11. Review record

This section records the adversarial design review of this draft and its
dispositions. It is filled in as the review proceeds and preserves objections
that were not resolved.

*(pending)*

## 12. Open operator decisions

1. **Parity standard.** Float tolerance, live shadow window, and the acceptable
   explained-mismatch rate for Section 8.
2. **Refused check-ins as checkpoints.** Recording them makes the record show
   what the gate suppressed; it also changes stream completeness, which matters
   for any future assessor that folds them.
3. **Report-text retention.** The report is transient today and truncated in
   excerpts (#2267 §4.3). The spine can carry a digest only, an excerpt, or the
   full text under a retention policy.
4. **`prediction_id` relation.** Field of the checkpoint (one prediction per
   checkpoint) or child records (several predictions per checkpoint, as the
   phase-5 evidence emitter already mints).
5. **Side-input capture versus accepting non-replayability** for inputs that are
   expensive to snapshot.
6. **Post-read authority posture** for the maintainer deployment and for new
   installs, taken after Stage 3 rather than bundled with it.

## 13. Authorization

Merging this document authorizes Stage 1 and Stage 2 only, each default-off and
non-authoritative, and nothing that writes to the database, changes a default,
or alters a response. Stage 3 and Stage 4 require a new, explicit operator
decision recorded against this document after the 2026-12-01 read is reported.

## 14. First implementation issue

**Title:** Neutral checkpoint seam: capture `CheckpointRecord` v0 inside the
check-in lock behind a default-off shadow flag, with a no-behavior-change test

**Scope:**

- Add a `CheckpointRecord` dataclass matching Section 4.1 in a new core module
  with no imports from EISV modules (enforced by an import-boundary test).
- Build it in `execute_locked_update` after identity resolution and before the
  estimator call: mint `checkpoint_id` (UUIDv7), assign in-process `seq` and
  `prev_checkpoint_id` per identity, capture `side_inputs` values that the
  estimator reads in the same request, and record `enforcement_regime`.
- Record refused check-ins at their refusal point with `status: refused`.
- Hand the record to a sink: no-op by default; NDJSON file when a new flag
  (default off) is set. No database, Redis, knowledge-graph, or response change.
- Document the flag in `docs/FLAGS.md`.

**Acceptance:**

- With the flag off and on, identical tool responses, identical rows written to
  `core.agent_state`, `audit.outcome_events`, and `audit.events`, and identical
  monitor state for a fixture sequence covering warmup, pause, refusal, and a
  concurrent check-in.
- Import-boundary test: the new module imports nothing from the estimator.
- `seq` is strictly increasing per identity under concurrent check-ins in the
  fixture.
- No change to the files and joins the registered read uses.

**Out of scope for the issue:** the shadow assessor (Stage 2), any persistence,
and any response field.
