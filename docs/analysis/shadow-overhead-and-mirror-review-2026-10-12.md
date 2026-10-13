# Shadow overhead and session-mirror review — 2026-10-12

This follow-up distinguishes measurement cost from a mistaken assumption that
all paths named "shadow" are inert. No live flags, identity behavior, audit
retention or study rules change. The benchmark uses synthetic records and
scratch files; this review performs no live database or study-outcome reads.

## Session mirror: the missing purpose was partly a documentation error

The durable mirror already supplies fallback fingerprint evidence on the keyed
session resume path. The previous inventory correctly found the enabled writer,
but relied on a stale runbook claim that nothing consumed the tables. That claim
is corrected here and in the [soak runbook](../operations/redis-retirement-soak-runbook.md).

The source path is concrete:

1. `resolve_session_identity` verifies a keyed session id.
2. `_keyed_bind_fingerprint` tries the Redis binding. If it does not supply a
   matching agent and fingerprint, it calls `get_db().get_session_binding`.
3. The PostgreSQL reader filters expired bindings; the caller accepts a returned
   fingerprint only when its `agent_uuid` matches the keyed identity.
4. `_fingerprint_hijack_check` consumes that evidence. Global `log` mode records
   suspected mismatches; `strict` can refuse a keyed resume. Neither mirror flag
   gates this read.

An additional maintenance path, `schedule_attested_bind_fingerprint_refresh`,
updates the fingerprint on existing same-UUID bindings after a substrate-attested
resume. Its PostgreSQL refresh also ignores the mirror flags. Thus disabling
SHADOW neither stops all access nor immediately makes existing rows irrelevant.

The loaded service configuration observed during this review has
`UNITARES_SESSION_MIRROR_SHADOW=1` and
`UNITARES_SESSION_FINGERPRINT_CHECK=log`. This establishes configuration and a
wired consumer, not how often it was used or how often a mismatch occurred.
Existing synthetic keyed-session tests confirm both strict mismatch rejection
and acceptance without a recorded fingerprint (3 tests passed).

`core.onboard_pins` has no production read caller found in the inspected source.
The general `UNITARES_SESSION_MIRROR_APPLY` resolver migration is still unwired.
Do not conflate either fact with absence of the binding-table fingerprint reader.
The inspected source baseline is
`d607235e1072a1332f96f0f060cb529ae70f53d2`; the deployment checkout had also reached
that commit when checked. The earlier October 12 inventory used `6d7422734`.
The fingerprint reader is present in both revisions.

The June 28 operator topology decision in
[the parent proposal](../proposals/archive/redis-retirement-v0.md) already defined
the target: PostgreSQL for appropriate durable bindings, Redis retained for
shared ephemeral coordination. It did not authorize eliminating Redis. Thus the
next design review should distinguish durable continuity coverage from fallback
fingerprint evidence and preserve both required behaviors. More parity samples
cannot settle that architecture, and a low usage count cannot retire identity
capability. Do not disable the mirror just to reduce the number of shadows.

The writes also are not latency-free. The binding write is awaited with a
1.0-second timeout budget and the pin mirror with 0.5 seconds, in addition to
their Redis work. These are code budgets, not observed delays or hard wall-clock
ceilings; cancellation may take longer. This review does not benchmark the
identity writer, Redis, PostgreSQL, connection-pool contention or cancellations.

## Local audit-writer benchmark

On CPython **3.14.8**, macOS 27.0.1 arm64, the actual `AuditLogger._write_entry`
ran in an isolated namespace with synthetic entries and temporary files. The
PostgreSQL scheduler was replaced by a counted, validating stub. Twelve cells
(three recording modes, two padding sizes, one/four events per logical update)
ran in seeded shuffled order for three rounds of 200 measured updates per cell.
Each block had 20 separate warmup updates, excluded from measurements. Total:
**7,200 measured logical updates**, 600 per cell. Rows, event order, redaction,
byte presence, fsync calls and scheduling-stub counts all matched expectations.

Normal JSONL plus `os.fsync`, milliseconds per synthetic logical update:

| Padding per event | Events per update | Mean | Median | p95 |
|---|---:|---:|---:|---:|
| 256 bytes | 1 | 0.04967 | 0.04577 | 0.06115 |
| 256 bytes | 4 | 0.21221 | 0.20035 | 0.32025 |
| 2,048 bytes | 1 | 0.06382 | 0.05854 | 0.09062 |
| 2,048 bytes | 4 | 0.22620 | 0.21677 | 0.33767 |

The four-event condition represents one base record plus three synthetic shadow
records. The difference of sample means is about **0.162 ms** for either padding
size. These are separate randomized blocks, not paired live check-ins, and no
confidence interval or production performance budget is asserted. Encoded rows
are larger than their padding: the normal four-event cases wrote 1,310,400 and
5,611,200 bytes respectively over 600 measured updates each.

The four-event means with JSONL disabled were 0.00881 and 0.00894 ms. With JSONL
kept but `fsync` replaced by a counted no-op, they were 0.15372 and 0.17690 ms.
That diagnostic contrast shows the local file-writing path includes more than
fsync alone; it does **not** recommend bypassing durability. On macOS this calls
Python's `os.fsync`, not `F_FULLFSYNC`; filesystem/cache and host-load differences
limit transfer to another machine or persistence regime.

The benchmark excludes import/constructor and payload-construction cost,
shadow-calculation cost, PostgreSQL scheduling/persistence, full handler work,
and concurrent contention. It exercises redaction and the real JSONL lock,
append, serialization, flush and fsync path. Counting and integrity checks prevent
swallowed file-write errors from looking like a fast successful benchmark.
It does not measure the session-mirror writer described above.

**Disposition:** this small sequential local recording cost is not evidence of a
production bottleneck and does not justify disabling shadows, discarding
non-firing denominators, weakening durability or adding a new batching service.
Keep the evidence. Revisit optimization only with a concrete performance question
and a benchmark covering the relevant workload. The lifecycle/ownership review
remains useful independently of whether the measured cost is small.

## Reproduce and validation

From the repository root in a fresh Python process:

```sh
python3 scripts/analysis/shadow_audit_cost.py --output shadow-audit-cost.json
python3 -m pytest tests/test_shadow_audit_cost.py -q --no-cov
```

The script records runtime and source-file hashes, configuration, balanced block
order, all raw per-update durations, summaries and integrity counts. A 3.12.12
preliminary run showed the same sub-millisecond local scale; the table above uses
the 3.14.8 run matching the inspected service interpreter. Neither run selects a
production threshold. Four benchmark tests cover count integrity, redaction,
failure visibility and scratch containment. Three existing keyed-session tests
and 14 attested-fingerprint refresh tests passed without live stores.


Recorded raw-result SHA-256 (timings vary on reproduction):
`9dd33d7a967e71679f30ee08545310f0f2d5de1d15dd225db6b015ff933c0403`.
