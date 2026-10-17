# File-write custody: admission, revocation and recovery

The October 5–12 frontier brief proposed revoking authority between planning
and execution. This implements that experiment on the existing file-write
executor. See the [brief's repository mapping](../ontology/frontier-brief-2026-10-12.md).
It does not add a standing capability grant or enable live deployment flags.

## Enforcement contract

A successful database reservation is the admission point for one file-write
operation, including its compensation. Before admission, the exact lease ID,
surface and holder must still match, be unreleased and have an unexpired TTL.
The clock is checked **after** acquiring the lease-row lock, so waiting behind
another transaction cannot spend the remaining TTL unnoticed.

Admission commits `surface_leases.effect_fence = effect_id` and the payload row
(including a promotion's single-consumer claim) in one transaction. A custody
refusal leaves neither reservation nor consumed promotion, so fresh-custody
retries work with either the same or a new idempotency key. The commit occurs
before reading the pre-image or calling any mutating filesystem operation. This is a durable
reservation, not a connection-scoped database lock. Its commit serializes with
release on the same lease row:

- Release wins first: the executor is refused and never writes.
- Admission wins first: release and handoff return `effect_in_progress` (HTTP
  409). They have **not** revoked the admitted operation. TTL expiry cannot reap
  its lease or admit a replacement, including for the same holder.
- Commit or successful compensation completes: the executor retires only its
  own reservation, after its last filesystem call has returned. Normal lease
  release/reaping can then proceed.
- Writer crash, interrupted I/O, failed compensation, or an unacknowledged
  commit: the reservation remains. No timeout or reconnect retires it.

An admitted operation may finish after the original TTL. This deliberately
orders revocation before or after admission; it does not promise cancellation
of an OS operation already admitted. The semantics require operator review
before deploying this enforcement change.

Migration 076 supplies the reservation column and a database trigger that
rejects release, deletion, or ownership/target changes while it is populated.
Automatic expired acquisition and periodic reaping exclude reservations.
Recovery scans exclude reserved payloads: neither old pre-image bytes nor a
matching content hash prove that a paused writer cannot later resume.

The pre-image is committed separately before the write; it cannot disappear
through rollback of a transaction spanning filesystem I/O. A missing, already
pending, or already committed payload row cannot authorize another write.
The live executor requires actual acquired lease rows; surface names alone
are sufficient only for dry-run validation.

The post-veto freshness check remains an early refusal with no payload row.
The executor's reservation closes the later check-to-write interval. Refusal
receipts distinguish `after_veto` from `durable_admission`, include lease IDs,
and preserve idempotency conflict checks. A custody refusal can be retried
only through fresh acquisition and the governance veto.

## Verification

Use the existing `governance_test` database, never the live governance DB:

```sh
psql -X -v ON_ERROR_STOP=1 -h localhost -U postgres -d governance_test \
  -f db/postgres/migrations/076_effect_custody_reservation.sql
cd elixir/lease_plane
mix test test/governed_effect_test.exs --only custody_revocation
mix test test/effect_custody_fence_test.exs
mix test
```

The three original HTTP regressions revoke, expire, or replace custody during
the veto. The additional tests pause immediately before the OS write and
before compensation, attempt release/handoff/reaping/replacement, and resume
the writer. They also cover request death, a disconnected database session,
expiry during lock contention, HTTP busy responses, and missing/replayed
pre-images. Four promotion regressions use a real row lock to revoke or expire
custody after the veto check, then verify successful retry with the same or a
new key. They use real PostgreSQL rows and temporary test files with an
injected filesystem barrier. No live external target is involved.

The filesystem-barrier test fails with the previous executor at `6f6dd0d98`:
force-release returns `:ok` while its write is paused. With reservations it
returns `effect_in_progress`; a successor is admitted only after completion.

## Deployment and retained-reservation recovery

Migration 075 is occupied by PR #2836; this change uses 076 and has no dependency
on that migration. Apply 076 before starting the new binary: lease queries now
read its column even with execute flags disabled. Stop and drain **all** live
file-write executors before the upgrade. Do not mix old live-writing binaries
with the reservation protocol: the trigger protects reservations, but an old
executor does not create one. No live migration or flag change is part of this
PR's validation.

Inspect retained reservations through `/v1/lease/status` (`active_effect_id`)
or an operator database connection:

```sql
SELECT lease_id, surface_id, holder_agent_uuid, effect_fence, expires_at
FROM lease_plane.surface_leases
WHERE effect_fence IS NOT NULL;
```

A retained reservation can outlive a process restart. Recovery is intentionally
conservative because the database cannot prove that a disconnected filesystem
writer is dead. To recover it:

1. Stop every executor that can access this surface, including disconnected
   nodes, and establish that their outstanding filesystem I/O has terminated.
   A heartbeat timeout, expired TTL, or failed database connection is insufficient.
   Keep dispatch stopped throughout recovery.
2. Inspect the exact effect payload, pre-image, current bytes and reservation.
   Use `EffectReconcile.reconcile_payload/2` from an isolated maintenance console
   to persist the observed disposition; it never writes file bytes. Investigate
   fresh rows without pre-images and quarantine/partial-write outcomes explicitly.
3. Once the surface is reconciled and the old writer cannot resume, retire only
   the inspected reservation with an operator transaction conditioned on both
   its exact `lease_id` and `effect_fence`. Record the disposition and operator
   recovery evidence. Release that lease through the existing release path,
   then resume dispatch. Do not clear reservations in bulk or on a timer.

A failed fence-retirement acknowledgment also retains custody. A successful
write reports `custody_retained: true` in that case. If the commit mark itself
failed, it reports `mark_deferred: true`. Neither response proves release.

## Scope

This orders cooperating lease-plane file writers, compensation and successor
acquisition within one database authority. It is not an OS sandbox: arbitrary
shell writers, privileged database administrators, symlink changes by external
processes, and writers using a different authority database remain outside
this boundary. It makes no claim about network tools, agent spawning, safeguard
profiles, standing capability grants, or production deployment.
