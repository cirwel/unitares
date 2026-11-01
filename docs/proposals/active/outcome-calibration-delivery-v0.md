# Durable delivery of prediction-bound outcome calibration

Status: draft implementation contract for [#2247](https://github.com/cirwel/unitares/issues/2247).
This document implements no runtime change and does not close the issue.

## Required invariant and boundary

For each newly committed canonical prediction-bound outcome, the calibration
that is eligible under its original recording rules must eventually reach each
configured sink once. A lost commit acknowledgement, a retry, a restarted
consumer or a transient sink failure must neither lose the sample nor train it
twice. Progress assumes the configured durable sinks and a consumer eventually
become available again; a retired or destroyed sink requires an explicit
recovery decision.

The identity is the canonical outcome UUID, not a submitting process, retry
attempt or current prediction-registry entry. The existing
`(agent_id, prediction_id)` binding and request digest continue to decide
canonical creation, replay and conflict. Calibration delivery cannot reopen or
replace that decision.

The guarantee begins with outcomes created through the new transactional
enqueue path. Existing outcomes have no durable calibration receipt. Their
absence is not proof that calibration was missed, so neither migration nor
retry may silently enqueue them. Historical reconciliation is a separate,
evidence-dependent operation.

## Verified current failure boundaries

1. `ToolUsageMixin.record_bound_outcome_event` commits the binding and outcome
   together. Its real-PostgreSQL tests lose the acknowledgement after commit
   and recover the canonical outcome through a different connection.
2. `_record_outcome_event_inline` trains calibration only for `created`, after
   the transaction returns. A lost acknowledgement prevents that effect;
   replay deliberately skips it.
3. `CalibrationChecker.record_prediction` and `record_tactical_decision`
   mutate a local object. `save_state` coalesces asynchronous whole-object
   replacements of `core.calibration.data`. A stale legacy writer could erase
   an outbox consumer's committed sample even if that consumer recorded a
   correct delivery receipt.
4. `SequentialCalibrationTracker` has a separate authority: its configured
   JSON file. Its update already holds a file lock and atomically replaces the
   file after flushing and `fsync`, but the state has no outcome receipt. A
   successful file replacement followed by a lost PostgreSQL acknowledgement
   would otherwise train again on retry.

An outbox and a marker written after an ordinary callback do not fix boundaries
3 and 4. Making all callbacks idempotent is a prerequisite, not an optional
second phase hidden behind a successful queue acknowledgement.

## Selected mechanism

Use a transactional outbox with a durable receipt at each sink. Preserve the
existing calibration estimators, eligibility, bin definitions and channels.
Do not introduce a second calibration instrument or blend an independent
projection into the old e-process.

### Canonical enqueue

Add an unpartitioned outbox keyed uniquely by canonical outcome UUID. Enqueue
eligible work in the same PostgreSQL transaction as the binding and canonical
outcome. No binding or outcome may commit without its required delivery work.

Freeze the original confidence and prediction provenance, canonical persisted
`REAL` score, badness, outcome type, agent, observation timestamp, tactical
channel, sequential eligibility, resolved class, epoch and destination identity.
The eligibility exclusions for fixtures, shadow traffic, scraped confidence
and insufficient evidence weight remain exactly those used by the recorder.
The consumer must not look up a newer confidence, agent class or forecast on
retry. Ineligible outcomes need no delivery row.

The outbox records progress separately for the ordinary calibration projection
and the sequential projection. A terminal acknowledgement requires every
required sink's durable receipt; a skipped or ineligible sink is represented
explicitly rather than as a successful callback.

### PostgreSQL calibration projection

Serialize each bound sample's strategic and tactical updates with the
`core.calibration` row lock. Compute both using the existing reducer semantics,
write the resulting state, and advance that sample's outbox receipt in one
transaction. A duplicate consumer observes the receipt and performs no second
update. A lost acknowledgement is recovered by reading the receipt.

Normal asynchronous calibration writers must stop replacing a state they read
before that transaction. Capture their existing mutation methods as operations,
with arguments and observation times frozen when called. Persist a stable
operation identity with the projection update under the same row lock; reuse
that identity after an ambiguous acknowledgement. Rebuild the local read cache
from the acknowledged projection plus still-pending local operations.

This applies to prediction, tactical, complexity, ground-truth and peer-update
methods. Nested mutations form one logical operation rather than independently
replaying an inner call twice. Direct reset, replacement and backfill callers
must use explicit replacement semantics with a version precondition; a stale
whole-object replacement must fail visibly instead of erasing committed work.
This writer-coherence change must land before durable outcome delivery is
enabled.

Unbound outcomes keep their existing non-idempotent submission semantics. Their
local mutation queue remains best-effort until persistence acknowledges it;
they do not acquire the prediction-bound exactly-once claim. Shared projection
writes gain serialization and stable retry identities without claiming that an
unbound submission itself became durable or deduplicated.

### Sequential file projection

For the existing canonical sequential state file, persist the canonical outcome
receipt in the same locked JSON replacement as its counters. Reload the latest
file while holding the lock, check the receipt, apply the sample using the
existing sequential update, add the receipt, flush, `fsync`, replace and `fsync`
the containing directory. Only
then acknowledge that sink in PostgreSQL. A crash between file replacement and
the PostgreSQL acknowledgement leaves a recoverable receipt, not a second bet.

All legacy file updates, pruning and rebucketing must preserve the receipt
section. A failed replacement must restore or reload the in-memory object
before it is reused; an unpersisted in-memory receipt cannot suppress a retry.

Competing consumers of this sink must share its canonical file and lock. Two
independent local files are different instruments, even if their path strings
match. Pin the destination in the work record and refuse a mismatched consumer;
do not silently redirect work after a host, backend, path or epoch change.
Distributed replication of the sequential instrument is outside this repair.
JSON-only ordinary calibration needs the same locked, atomic state-plus-receipt
adapter and writer-coherence rule; it cannot fall back to an unreceipted callback.

### Scheduling, failures and retention

Use the existing tracked background-task lifecycle to drain durable pending
work. A post-commit wake may reduce latency; the background scan, not the wake,
provides recovery after process restart or an ambiguous outcome commit. No MCP
tool call, governance update or model inference is needed for delivery.

Consumers use bounded batches and serialized claims such as `FOR UPDATE SKIP
LOCKED`. Transient failures leave durable work pending. Progress and last error
must distinguish pending, already delivered, blocked destination and storage
failure. No success flag may stand in for a receipt. Claims need no expiring
in-memory ownership proof: database rollback releases a crashed consumer's lock.

Pending work retains its frozen payload even if the partitioned outcome ages
out. Binding cleanup must not retire a claim with outstanding delivery. Completed
work and receipts may be compacted only when the binding's replay window is
closed and no valid consumer can replay that operation. File receipts must not
expire merely by age while their outbox or binding can still replay. Epoch or
backend retirement must first drain or explicitly preserve its pending work;
old work cannot be trained into a replacement instrument by accident.

## Alternatives rejected

- Retrying the current callbacks, then marking the outbox delivered: each
  callback can commit its effect before its acknowledgement is lost.
- Marking delivery before the callback: a crash loses the sample.
- A receipt separate from a whole-snapshot state write: either ordering has a
  crash window, and a later stale snapshot can still erase the sample.
- A second, additive bound-outcome projection merged into legacy reads: the
  sequential e-process is order-sensitive, so summing independent projections
  changes the estimator. It also expands every read and reset contract.
- Updating all sinks inside the original outcome transaction: a file effect
  cannot roll back with PostgreSQL, and sink failure would become outcome
  persistence failure. The outbox preserves the existing capture boundary.

## Scientific and authority constraints

The registered read already has `instrument_change_at =
2026-10-15T13:45:00Z` in `REGISTERED_READ_MANIFEST`. The operator's data-boundary
amendment superseded the old calendar hold on live changes. This repair must
leave that boundary, the archived extract, thresholds and registered command
unchanged; it does not reopen supply by changing eligibility.

The rollout records the delivery cutover and storage/projection version in the
implementation evidence. Historical cohorts and receipts are not backfilled
without independent evidence. No claim of improved calibration quality follows
from a durability fix.

There is a separate constraint in the registered
[coordination ablation](../registered/accountable-coordination-ablation-v0.md).
Arm D includes outcome binding, and the [outcome-binding assurance case, L1](../../evaluations/assurance-cases/outcome-binding-v0.md#6-known-limits)
identifies #2247 as a defect specific to that arm. The assurance case recommends
disclosing it as arm-specific; that recommendation is not an operator
classification. Classification remains the operator's decision because success
condition 5 depends on whether a common-mode repair occurred. Under the
protocol, a common-mode repair must reach every arm and invalidates the affected
block before an arm comparison is read. This contract cannot redefine that rule,
reclassify L1, patch only arm D inside a block, or treat the outcome-grounding
cutoff as permission to modify a different experiment.

Before live adoption, check the immutable enrollment artifact and included
blocks, not just the protocol's draft status. If adopting this repair would
affect an enrolled block, hold that adoption for the operator's classification
and block-disposition decision. Isolated implementation and fixture testing do
not enroll a cohort or authorize changing frozen experiment arms.

The assurance case also recommends measuring L1's production frequency. The
current store has no durable calibration-delivery receipt, so a canonical row
or a successful replay cannot establish whether calibration was delivered.
The forced ambiguous-commit regression proves the failure is reachable, not
its production rate. Record that incidence is unmeasured; do not turn a zero
from this blind surface into a reason to retain or retire a capability. Any
operational incidence audit must name its observable numerator, denominator,
coverage and unresolved cases, and remain separate from confirmatory arm data.

## Implementation and verification sequence

1. Review this storage and writer-coherence contract. The advisory council
   request timed out at the MCP transport boundary; it supplies no completed
   critique or implementation-review coverage. Verify both registered-study
   constraints and current enrollment evidence. Adoption into an enrolled
   coordination-ablation block requires the operator's classification and
   disposition; no implementation test or readout can substitute for it. State
   L1 incidence as unmeasured until an adequate operational audit exists.
2. Implement the private operation reducer and coherent persistence adapters.
   Verify numerical parity and serialization with all current writer families,
   including a stale writer that overlaps a durable sample and reset/backfill
   version conflicts. Preserve the existing public tool surface.
3. Add atomic enqueue, sink receipts, background recovery and retention. Wire
   prediction-bound recording away from the ordinary inline callbacks so a
   sample cannot travel both routes.
4. Exercise the failure matrix below on real PostgreSQL connections and real
   temporary state files, then run the aggregate gate and independent diff
   review before readiness. A design review is not that implementation review.

| Case | Required observation |
| --- | --- |
| Outcome failure before commit | No canonical row, binding, delivery work or sink mutation. |
| Outcome commit acknowledgement lost | One canonical row and work item; later scan delivers each eligible sink once. |
| Identical replay after restart | Same canonical response and no second delivery effect. |
| Conflicting retry | Existing conflict semantics and no additional work or training. |
| Concurrent consumers | Distinct PostgreSQL connections; one effect per sink despite competing claims. |
| Calibration transaction failure | Projection and its receipt both roll back. |
| Calibration acknowledgement lost | Receipt replay confirms the persisted update without incrementing again. |
| Sequential replacement fails | No durable receipt; retry trains once from the last durable state. |
| Sequential replacement succeeds, acknowledgement lost | File receipt suppresses a second update. |
| Legacy writer overlaps delivery | Previously committed bound sample survives later legacy persistence. |
| Destination or epoch changes | Work remains pending or is explicitly migrated; it never silently trains a different instrument. |
| Outcome retention before delivery | Frozen work survives; claim cannot be retired while work is pending. |
| Completed-work compaction | Neither a still-valid replay nor a stale writer can retrain or erase the sample. |
| Fixture, shadow, scraped and low-weight rows | Existing exclusion rules remain unchanged. |
| Unbound submission | Existing non-idempotent recording semantics remain explicit. |

Until these implementation gates pass, #2247 remains open and the current
zero-delivery regression remains the truthful description of the live boundary.
