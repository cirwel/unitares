# Check-in refactor: current execution and persistence boundaries

Inspected 2026-11-02 at `93525d42e`. This is a characterization of the current
implementation for the next component-ownership changes. It supplements the
[instrument-preservation inventory](eisv-instrument-preservation-harness.md)
and the [component boundary proposal](../proposals/active/federation-component-boundaries-v0.md).
It changes no runtime behavior or authorization and does not certify the full
checkpoint extraction gate.

## What the existing boundary actually guarantees

`run_process_update_workflow` holds an agent lock around the authenticated
estimator update. It releases that lock **before** grounding, the PostgreSQL
state insert, outcomes, and response enrichment. There is no transaction
enclosing those operations. A successful response currently does not prove
that a state row was committed. A stored monitor reference is also not an
immutable snapshot of that monitor.

Sources: [`update_workflow_service.py`](../../src/services/update_workflow_service.py),
[`phases.py`](../../src/mcp_handlers/updates/phases.py),
[`agent_loop_detection.py`](../../src/agent_loop_detection.py).

| Stage / owner symbol | Lock | Effects and refusal/failure behavior | Refactor implication |
|---|---|---|---|
| MCP middleware; `handle_process_agent_update` | Before agent lock | Transport identity and parameter validation precede the workflow; the handler also normalizes aliases/provenance and constructs `UpdateContext`. Handler exceptions become error responses. | Preserve transport-specific validation; a workflow-only test does not exercise middleware. |
| `resolve_identity_and_guards` | Before | Rejects missing or insufficient binding and an unexpired pause. It may expire a pause, lazily persist an identity, update its label, and reconcile configured tags before later stages run. | An early exit is not a blanket no-write receipt. Separate accountable identity/lifecycle operations from checkpoint acceptance. |
| `handle_onboarding_and_resume` | Before | Optional KG guidance for a new agent catches errors. Despite the historical function name, the current body refuses archived, paused, and deleted identities. | Preserve authoritative lifecycle gates; KG guidance can fail independently. |
| `transform_inputs` | Before | Normalizes report/confidence/complexity, applies dialectic complexity caps and weak-identity confidence damping, collects evidence input. | Retain submitted versus transformed values and provenance; a cap is not merely response decoration. |
| `prepare_unlocked_inputs` | Before | Builds `agent_state`; reads monitor histories, tool usage, fleet calibration, recent outcomes and baseline data; computes behavioral sensor inputs. Many optional reads catch exceptions. | Inputs can precede another accepted update while waiting for the lock. Moving them changes instrument timing/selection. |
| `execute_locked_update` | Inside | Checks creation policy; loads credentials/baselines, tracks thread identity, schedules metadata writes, computes anomaly input, and calls `process_update_authenticated_async`. Estimator errors propagate; no Phase-5 state write follows them. | Treat preparation, mutation, and durable capture as separate boundaries. Background metadata writes are not rolled back by later failure. |
| `process_update_authenticated_async` | Inside via caller | Checks ownership, actuation configuration, archive state and loop cooldown; hydrates/updates the monitor, increments update counts, optionally applies a durable or degraded local pause, and saves a monitor snapshot. These are separate writes. | A subscriber conversion must preserve existing holds and the explicit `enforce` posture, not silently move authority after acknowledgement. |
| `run_grounding_stage` | After | Stamps coherence provenance; configured shadow/apply modes can calculate, log or replace metrics before persistence. | Preserve the order relative to state capture and response construction. |
| `_post_update_health_and_baselines`, `_post_update_cirs_and_drift` | After | Update baseline/profile and CIRS/drift state, schedule persistence, and apply post-ODE dialectic conditions before recording the state row. | These are stateful operations; classifying all post-lock work as observers would change behavior. |
| `_post_update_record_state` | After | Builds state/provenance/report excerpt; calls `agent_storage.record_agent_state`, which looks up identity then calls `StateMixin.record_agent_state`. Generic errors are logged and return `True`. In strict mode, any `ValueError` is treated as missing identity and returns `False`, stopping later Phase-5 effects; the exception's origin is not inspected. | Its boolean means continuation, not a commit receipt. Do not use it as the source of a future `checkpoint.recorded` event. |
| `_post_update_save_baseline`, `_post_update_auto_outcome`, `_post_update_trajectory`, `_post_update_phase5_evidence`, `_r2_post_update_hook` | After | Ordered writes with distinct failure policies. Auto outcomes infer labels from report text; evidence can bind predictions and reports per-item exceptions. Strict missing-identity state failure skips these; a generic state-write failure does not. | A failure can leave outcomes without this check-in's state row. Capture this before defining new atomicity and retries. |
| `build_process_update_response_data`, `run_enrichment_pipeline`, formatter/serializer | After | Copy result fields, expose the monitor's latest prediction ID, run serial enrichments, and shape the response. Each registered enrichment catches exceptions. Formatter and serializer have fallbacks. | Preserve response modes and prediction attribution; serialization success is independent of persistence success. |
| `handle_process_agent_update` completion | After workflow returns | Schedules a presence heartbeat without changing the response. | Keep liveness distinct from accepted checkpoint recording. |

## Existing atomic islands and mutable inputs

`core.agent_state` is inserted by
[`StateMixin.record_agent_state`](../../src/db/mixins/state.py). Its identity
lookup, metadata update count, monitor file save, outcome inserts and audit
writes do not share that insert's transaction. The workflow also has no
check-in idempotency key. Its retries must not be described as deduplicated
checkpoints.

There is already a narrower durable delivery mechanism worth reusing as a
precedent: [`ToolUsageMixin.record_bound_outcome_event`](../../src/db/mixins/tool_usage.py)
atomically claims `(agent_id, prediction_id)`, inserts the canonical outcome,
and enqueues eligible calibration delivery. Identical submissions return the
canonical outcome; conflicting reuse is refused while the binding is retained.
[`calibration_delivery.py`](../../src/calibration_delivery.py) drains that
outbox with scoped receipts and retries. This covers bound outcomes, not every
automatic outcome or the check-in itself. A new checkpoint outbox must have
an explicit owner and must preserve this existing delivery protocol.

[`AuditLogger`](../../src/audit_log.py) writes a local JSONL record when enabled
and schedules its PostgreSQL copy separately; failures are caught. Audit
delivery is not transactional with the check-in's state insert.

Prediction IDs remain associated with monitor bookkeeping, and Phase-5 evidence
can mint or reuse them (`_prediction_id_for_phase5_evidence`). Optional EISV
therefore requires a neutral prediction owner before the estimator can be
absent. Persisted assessments require immutable input snapshots; replay from
checkpoints alone cannot reconstruct the discarded clock values and external
inputs listed in the instrument inventory.

## Callers and readers to migrate

This is a source inventory of named seams, not an exhaustive SQL dependency
graph. Search the owners and their callers again before each migration.

| Owned record / path | Current cross-component consumers or alternate writers |
|---|---|
| State: `agent_storage.record_agent_state` → `StateMixin` | `http_routes/{overview,residents,effects}.py`, `governance_trend.py`, `runtime_observations.py`, `resident_progress/sources.py`, `identity/{agent_fragmentation,model_risk_cohort,s22_h5_comparison}.py`, `agents/chronicler/scrapers.py`, analysis scripts. Timeline uses `fetch_timeline_checkins` in the owner mixin. |
| Outcomes: `ToolUsageMixin`; `_record_outcome_event_inline` | Direct private helper imports in `updates/phases.py`, `dialectic/resolution.py`, `http_routes/substrate.py`. Automatic check-in/trajectory outcomes use the DB recorder directly. `outcome_correlation.py`, `eisv_telemetry_health.py`, calibration backfill and analysis scripts read outcome SQL. |
| Identity: `IdentityMixin` and identity services | Session/state/outcome mixins join identities; HTTP overview/resident/effect queries and grounding/class-promotion queries cross ownership. Check-in identity guards and presence handling additionally use metadata and session state. |
| Review: `dialectic_db.py` | The optional BEAM saga (`elixir/lease_plane/lib/unitares_lease_plane/dialectic_saga.ex`) is a declared co-writer. Lifecycle recovery imports review internals; operations scripts query sessions directly. An API wrapper must account for both writers and existing resolution concurrency. |
| Audit: `audit_db.py`, `AuditMixin`, `AuditLogger` | Enrichments, monitor/policy paths and lifecycle operations emit records; resident/Elixir observers and operator scripts read them. Preserve the distinction between local logging and durable DB receipt. |

[`check_component_boundaries.py`](../../scripts/dev/check_component_boundaries.py)
already ratchets baselined imports/SQL. Its `TABLE_OWNERS` is a finite list:
prediction-binding and calibration-delivery tables are not currently included.
B4 scans server and Elixir library source; general analysis/operations scripts
are not all covered (B1/B2 separately follow resident-launched scripts). A green
ratchet does not establish complete ownership or portability.

No other checkout, deployed database, running process, or external client was
inventoried for this document. Before storage cutover, deployment owners must
identify clients outside this repository and supply compatibility evidence.
Do not infer their absence from this repository scan.

## Verification and remaining gates

The new [`test_checkin_characterization.py`](../../tests/test_checkin_characterization.py)
has nine cases using real workflow, phase, grounding, enrichment and serializer
bodies with controlled service/storage edges. They cover the successful
response and state/outcome arguments, three estimator exception types, strict
missing-row versus generic persistence failure, auto-outcome writer failure,
an unexpired pause before lazy persistence, and two interleaved calls. The
interleaving case uses an actual `asyncio.Lock` and events to show the second
response can finish while the first state write is pending (completion order
`[2, 1]`). It does not execute real PostgreSQL transactions or the actual
authenticated estimator service and cannot establish durable commit ordering.

Existing complementary coverage:

- `test_update_workflow_service.py`: workflow branches, post-lock dispatch,
  actual phase input/provenance assembly with mocked service/storage, archived
  refusal and legacy state-create retry.
- `test_eisv_instrument_preservation.py`: estimator golden trajectories and
  clocks, mocked post-update dispatch order, and a synthetic database
  state-writer/registered-join check. The linked inventory lists its limits.
- `test_phases_phase5_evidence.py`, `test_phase5_prediction_not_advertised.py`:
  evidence mode, prediction handling and per-item isolation.
- `test_policy_actuation_posture.py`, `test_update_response_service.py`:
  actuation distinction and serializer fallback.
- `test_outcome_prediction_idempotency.py`,
  `tests/db/test_outcome_prediction_binding_postgres.py`,
  `test_calibration_delivery.py`, `tests/db/test_calibration_delivery_postgres.py`:
  bound-outcome replay/conflict and calibration delivery persistence.

Full middleware-to-database trace, restart, simultaneous check-ins, mixed
versions, external readers and production latency remain separate gates.
`test_concurrent_updates.py` explicitly executes synchronous monitor calls
sequentially; it is not evidence of concurrent handler persistence ordering.

The registered outcome-grounding read has already fixed its first instrument
change at `2026-10-15T13:45:00Z` in `REGISTERED_READ_MANIFEST`
([`eisv_ablation_matrix.py`](../../scripts/analysis/eisv_ablation_matrix.py)).
That is a research dataset cutoff, not a general authorization/read API. Do not
move it forward for this refactor or introduce a `RegisteredReadPort` on that
premise. Preserve the archived inputs, era labels and the study's rules;
ordinary data access continues through its own authorization contracts.

## Next bounded changes

1. Extract one existing component operation behind its owner's API, preserving
   current failure behavior and shrinking the boundary baseline. The private
   outcome helper's three importing sites form a concrete candidate; its
   binding/calibration transaction and evidence grades must remain intact.
2. Define the checkpoint acceptance and commit receipt explicitly. A neutral
   durable record and transactional outbox are new semantics here; specify
   retry/conflict handling, refused attempts, ordering, and failure reporting
   before adding them. Keep the proposal's diagnostic stages distinct from
   its later durable ownership transfer and authorization.
3. After a durable capture contract exists, adapt the estimator through a
   synchronous interface with call-local inputs and explicit response timing.
   Evaluate asynchronous delivery separately, including enabled enforcement,
   read-after-write expectations and replay gaps.

The target remains an accountability record that survives optional assessment
failure. The current successful-response-without-state behavior is recorded
here so a future correction is deliberate and tested, rather than disguised
as a behavior-preserving extraction.

## Legacy state-write acknowledgement (2026-11-02 increment)

The next increment exposes `state_persistence` on completed check-in responses,
including bounded `sync_state` replies and serialization fallbacks. This is a
call-local receipt for the existing `core.agent_state` assessment insert:

| Field | Meaning |
|---|---|
| `schema: agent_state.persistence.v1` | Identifies this assessment-write receipt, not a neutral checkpoint. |
| `status: recorded` with a positive integer `state_id` | The awaited writer returned that row ID. The acknowledgement covers this state insert only. |
| `status: unconfirmed` with `state_id: null` | No valid acknowledgement reached the workflow. The row may or may not exist; a connection failure can follow commit. |
| `checkin_retry_idempotent: false` | Repeating the full check-in can repeat estimator updates and other effects. No automatic retry or deduplication is added. |

The immutable receipt is separate from the existing Phase-5 continuation
boolean. Generic write errors still allow later effects; strict-mode
`ValueError` still stops them. Both yield unconfirmed persistence, because the
broad exception class does not prove a pre-write refusal. The legacy
create-and-retry branch confirms a row only if its second write returns a valid
ID. Missing or malformed writer acknowledgements are also unconfirmed.
Cancellation still propagates. Early gates that never reach the state writer
do not acquire a receipt.

Unconfirmed responses carry a fixed warning describing the ambiguous commit
and non-idempotent retry. Top-level `success` retains its existing workflow
meaning; it is not a persistence acknowledgement. A confirmed receipt remains
valid if a later outcome or response-serialization step fails. It does not
certify those other writes, a whole-check-in transaction, durable per-agent
ordering, or acceptance independently of EISV.

This is a prerequisite for a neutral checkpoint ledger. Its later contract
must define identity-scoped idempotency/conflicts, immutable neutral inputs,
atomic sequence/checkpoint/outbox allocation, ambiguous-commit replay, and the
relationship to gates that still run inside the estimator service. The partial
preservation and replay gates above remain incomplete; this receipt does not
fulfil them or authorize asynchronous EISV.
