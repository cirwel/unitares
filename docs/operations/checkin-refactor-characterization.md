# Check-in refactor: current execution and persistence boundaries

Inspected 2026-11-02 at `93525d42e`. This is a characterization of the current
implementation for the next component-ownership changes. It supplements the
[instrument-preservation inventory](eisv-instrument-preservation-harness.md)
and the [component boundary proposal](../proposals/active/federation-component-boundaries-v0.md).
It changes no runtime behavior or authorization and does not certify the full
checkpoint extraction gate.

The execution trace below is the legacy baseline at that revision. The later
[neutral acceptance increment](#neutral-checkpoint-acceptance-2026-11-02-increment)
adds a separate commit boundary before assessment; its guarantees and remaining
limits are stated separately rather than attributed to the legacy state insert.

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
already ratchets baselined imports/SQL. Its `TABLE_OWNERS` is a finite list
(including `audit.outcome_prediction_bindings` and
`audit.outcome_calibration_deliveries` under `outcomes`).
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

## Neutral checkpoint acceptance (2026-11-02 increment)

The operator authorized this bounded next increment after the state-persistence
receipt. [Boundary proposal Section 13.1](../proposals/active/eisv-core-boundary-v0.md#131-bounded-neutral-acceptance-increment-2026-11-02)
records its scope and the historical governed-review disposition. The unit of
acceptance is now a neutral immutable checkpoint. The existing
`state_persistence` field continues to acknowledge only the later assessment
row, so the two receipts can truthfully report different outcomes.

### Admission and replay

Transport validation and identity binding, the existing lifecycle guards, and
the authenticated service's ownership, deployment-posture, archive and
loop-cooldown checks precede checkpoint acceptance. Refusal at one of these
gates does not mint an accepted checkpoint. An early refusal can still follow
identity, metadata or cooldown writes; it is not a promise that the entire
request had no effects. This increment does not provide a complete durable
ledger of refused, deferred, cancelled or unauthenticated attempts.
Legacy preparation also computes behavioral inputs and passes call-local
`_outcome_history` in `ctx.agent_state` before acceptance without mutating
`monitor._cached_outcome_history` on the shared monitor until assessment runs
(at which point `GovernanceMonitor.process_update` refreshes
`monitor._cached_outcome_history` for fallback use on later check-ins).
The new boundary precedes monitor creation/advancement in the authenticated
service and the later check-in effects; it does not yet move every
assessment-related read behind capture.

An explicit `idempotency_key` (a nonblank string of at most 200 characters) is
scoped to the resolved identity. Reusing it with the same canonical submitted
request returns the original acceptance; reusing it
with different semantic input is a conflict. The request digest is captured
before estimator transformations so a later complexity cap, confidence damping
or changed estimator state cannot redefine what the caller originally submitted.
The versioned input allowlist in
[`checkpoint_service.py`](../../src/services/checkpoint_service.py) includes
submitted report, assessment/evidence inputs and descriptive provenance. It
excludes response presentation and identity proofs; embedded credentials are
scrubbed before hashing. This fingerprint is for request equivalence, not a
complete assessor replay snapshot. Omitting the key allocates a new occurrence
on every call and reports `acceptance_retry_idempotent: false`.
Current admission requirements such as `require_strong_identity` remain gates,
rather than fingerprint fields. The response-only `include_memory_suggestions`
option is also excluded; submitted `trajectory_signature` and the versioned
allowlist of descriptive provenance are included.

Replay still passes the current write-admission gates. A pause, archive or
cooldown that arose after the original acceptance can therefore refuse a retry
until the identity is writable again. The stored receipt is evidence of a past
acceptance, not permission to operate under a current hold. There is no separate
read-only receipt-recovery tool in this increment. A replay that passes these
gates returns the accepted checkpoint without rerunning its assessment or later
workflow state/outcome/enrichment effects. Transport auditing and the handler's
presence heartbeat can still run on a retry; this is acceptance idempotency,
not a claim that the entire request is free of repeated effects.

### Atomic record and receipt

The checkpoint owner uses a PostgreSQL transaction to allocate the identity's
next sequence and predecessor, insert the immutable checkpoint, advance its
stream head, and insert the immutable `checkpoint.recorded.v1` event into
`core.checkpoint_outbox`. Commit makes these records visible together; rollback
leaves none of them accepted. Sequence establishes checkpoint order independently
of wall-clock order. It does not establish the order in which all legacy
post-update effects finish. New appends additionally lock and recheck the
durable identity's active lifecycle state through the identity owner in that
same transaction. The storage implementation is
[`CheckpointMixin`](../../src/db/mixins/checkpoint.py); its schema is
[`081_neutral_checkpoints.sql`](../../db/postgres/migrations/081_neutral_checkpoints.sql).
Database triggers reject updates to checkpoint and outbox rows. No application
deletion API or automatic pruning is introduced; deliberate administrative
deletion remains possible, so these records are not deletion-resistant storage.

`core.checkpoint_streams` holds a per-identity random digest salt. The stored
retry-key and canonical-request digests use domain-separated HMAC-SHA256; raw
retry keys, complete request text, API keys and session proofs are not stored
in the checkpoint. Its report field uses the existing bounded,
credential-scrubbed excerpt policy. Neutral provenance records the epistemic
class, identity tier, proof origin, transport and producer version. EISV metrics,
policy output, computed sensors and assessor-input snapshots are not part of
the neutral event.

The top-level `checkpoint` object carries this acceptance receipt:

| Acceptance receipt field | Meaning |
|---|---|
| `schema: checkpoint.acceptance.v1`, `status: accepted` | The neutral checkpoint transaction was acknowledged. |
| `checkpoint_id`, `event_id` | The immutable occurrence and its neutral outbox event; the event uses the checkpoint ID. |
| `identity_id`, `seq`, `prev_checkpoint_id`, `recorded_at` | The resolved identity, durable per-identity position, predecessor and UTC record time. |
| `accepted_attempt_id` | The attempt that first committed this checkpoint, retained on replay. |
| `idempotent_replay` | This call recovered an already accepted checkpoint. |
| `acceptance_retry_idempotent` | An explicit key can recover this acceptance without rerunning effects; it does not promise successful or exactly-once assessment. |

A lost acknowledgement can follow a successful commit. Such an exception is
not proof that no checkpoint exists: retrying with the same explicit key can
recover the original acceptance once current admission permits. The client must
retain that key. A fresh key represents a new occurrence and cannot deduplicate
the earlier request. An unconfirmed response has `status: unconfirmed`, the
current `attempt_id`, and `acceptance_retry_idempotent` indicating whether the
client supplied an explicit key; it does not invent a checkpoint ID or assert
rollback.

### Synchronous assessment and incomplete delivery

The first acknowledged new acceptance continues through EISV synchronously.
Before invoking it, a separate assessor-owned `assessment.checkpoint_deliveries` record claims
the delivery from `pending` to `started`. Assessment and the existing state,
outcome, prediction, calibration and audit effects do not share the checkpoint
transaction. The neutral event therefore survives estimator failure, a failed
legacy state insert, cancellation, or response-serialization failure after
acceptance.

`completed` means the workflow reached response construction after its effects
and enrichments, not that every legacy write committed atomically or response
serialization succeeded. `failed` records a delivery failure; a remaining
`started` record can mean work is still running, a process stopped, or an acknowledgement
was lost after effects. Neither state is automatically reclaimed or rerun by a
retry. Cancellation propagates and leaves an already started claim ambiguous.
A delivery whose claim or completion acknowledgement is unavailable is reported
as `unconfirmed`. The receipt's nested `assessment` object reports this execution
state separately from checkpoint acceptance; a retry reports `not_repeated`
without inventing a current verdict, metrics or prediction.

Even an accepted checkpoint that never started assessment is receipt-only on
retry. For example, losing the checkpoint commit acknowledgement can prevent
the original request from claiming delivery at all. Recovering its receipt does
not restart delivery; that needs a future explicit recovery contract. There is
no asynchronous consumer, automatic recovery loop, or exact EISV replay promise
in this increment.

The outbox is a durable neutral event record, not proof of successful delivery
to an assessor. Existing outcome-binding and calibration-delivery transactions
retain their separate ownership and retry contracts. Predictions remain owned
by the monitor; accepted checkpoints with failed assessment need not have a
prediction or assessment row. The rest of Stage 3, including neutral prediction
ownership, outcome rebinding, legacy-reader migration and export, remains work
to do.

### Evidence and remaining limits

The required verification for this increment is real PostgreSQL commit and
rollback behavior, identical and conflicting retries, simultaneous requests,
restart and lost-acknowledgement recovery, plus workflow checks that admission
refusal creates no new checkpoint and estimator failure leaves an accepted record.
Response-mode and serializer checks must preserve the acceptance receipt even
when no assessment receipt exists. This is narrower than the full preservation
and replay inventory above. It does not certify production latency, mixed-version
compatibility, external readers, complete estimator inputs, or exactly-once
assessment. The fixed registered-read cutoff and archived inputs remain
unchanged.
