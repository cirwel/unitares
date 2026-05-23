# Lease-plane Phase A latency — first measurement 2026-05-20

Anchors the substrate-tax measurement gate from
`beam-footprint-roadmap-v0.md` v0.3.2 amendment 2026-05-09: *"lease-plane
Phase A latency instrumentation producing ≥14d of data."*

## Scope

This doc evaluates the lease-plane's BEAM↔Python boundary for
substrate-tax signature using **existing 14-day audit data only**, plus
adds client-side RPC instrumentation for the next window. Same
methodology as `surface-lease-plane-v0.md` §7.5 v0.9 (Steward
audit-mining): use what is already in the audit log rather than waiting
for purpose-built telemetry.

## What "Phase A latency" measures here

The v0.3.2 amendment names this gate to capture substrate-tax cost at
the lease boundary — Python clients (residents, handlers) calling the
BEAM lease plane over HTTP. The interesting distributions are:

- **RPC latency** — Python-client wall-clock from request send to
  response. *Not yet recorded* — added in this branch
  (`src/lease_plane/client.py` → `perf_monitor`).
- **Hold time** — `released_at − acquired_at` from the BEAM event log.
  *Available retroactively*; not RPC latency but the closest proxy.
- **Conflict / TTL-reap rates** — operational signal that the lease
  plane is doing useful work.
- **forward_attempts distribution** — BEAM↔Postgres internal retry
  signal; non-zero indicates coordination friction inside the BEAM node.

## Headline (window: 2026-05-06 → 2026-05-20)

From `scripts/dev/lease_plane_latency_audit.py` on `lease_plane.lease_plane_events`:

| Surface | n acquire→release | p50 | p95 | p99 | p100 |
|---|---|---|---|---|---|
| resident | 44,535 | 28 ms | 338 ms | 5,146 ms | 60,933 ms |

Event-type distribution:

| event_type | count |
|---|---|
| acquire | 44,560 |
| release | 44,535 |
| renew | 27,645 |
| conflict_held_by_other | 69 |
| reaped_local_ttl | 21 |
| down_local | 3 |

Derived rates over 14 days:

- Conflict (`held_by_other / acquires`): **0.155%**
- TTL reap (`reaped_local_ttl / acquires`): **0.047%**
- BEAM↔Postgres forward attempts: **100% of 116,833 events** completed
  on the first forward attempt — no retry pile-up.

## Regime boundary inside the window

The 14-day window straddles two enforcement regimes — this is named here
because the v0.3.2 gate predates the boundary:

- 2026-05-06 → 2026-05-18 (13d): Phase A advisory mode (PR #305, merged
  2026-05-03)
- 2026-05-19 → 2026-05-20 (1d at window close): Phase B resident
  enforcement drill began; PR #476 merged 2026-05-20 UTC

The headline percentiles aggregate across both regimes. The conflict
and TTL-reap counts are dominated by the 13-day advisory tail; Phase B
enforcement has not produced enough events to characterize on its own.
The next 14d window will be Phase-B-only and is the cleaner attribution
surface — flag for the follow-up doc.

## Honest reading

The numbers are **compatible with no substrate-tax signature at the
lease boundary**, but they do not conclusively measure it. Specifically:

- Hold time at p99=5.1s and p100=60.9s is *not evidence of slow lease
  operations*. The default lease TTL is 300s (per the
  `resident:/sentinel_cycle` and `resident:/steward_eisv_sync` events
  inspected for shape), so a 60s hold is one-fifth of TTL — well within
  normal "lease held while resident did work" territory. The hold-time
  tail is dominated by *work duration under lease*, not by
  *acquire-RPC overhead*.
- `audit.tool_usage.latency_ms` is **NULL for 100% of 116,833 lease
  events** over 14 days. The Elixir-side `insert_tool_usage`
  (`elixir/lease_plane/lib/unitares_lease_plane/repo.ex:594-619`)
  hardcodes the column to NULL. The substrate-tax signal — Python
  client wall-clock per RPC — is not in this data at all.
- Forward-attempt distribution is uniformly 1 across the entire window.
  This is either "no BEAM↔Postgres friction" (best case) or "the
  forward-retry path simply doesn't get exercised because nothing fails"
  (Caveat 2 of `wave-1-window-evaluation-2026-05-18.md` re-applies).
  The data cannot distinguish these.

The 60× amplification finding from `CLAUDE.md §"Substrate Tax:
anyio-asyncio Coupling"` (KG calls running 21–71ms standalone,
~4,464ms in-handler) is what a substrate-tax floor at the lease
boundary would look like — and that is **not visible** in this 14-day
window. But it is also not measurable from this data either way until
the RPC instrumentation lands.

## What this branch adds for the next window

`src/lease_plane/client.py:_request_json` now records per-call RPC
latency to the in-process `perf_monitor` under keys:

- `lease_plane.client.v1.lease.acquire`
- `lease_plane.client.v1.lease.acquire.ok`
- `lease_plane.client.v1.lease.acquire.held_by_other`
- `lease_plane.client.v1.lease.release`
- `lease_plane.client.v1.lease.release.ok`
- `lease_plane.client.v1.lease.renew`
- ... and corresponding outcome shards (`schema_invalid`,
  `transport_exception`, `permission_denied`, `service_unavailable`)

`perf_monitor.snapshot()` exposes p50/p95/p99/max per key. Longitudinal
capture landed in PR #481: `perf_monitor_persist_task` samples the snapshot
every 5 minutes and writes catalog-gated series to `metrics.series`,
including `lease_plane.client.v1.lease.acquire.p50` and `.p99`.

## Falsifier — what this window cannot resolve

The substrate-tax-at-lease-boundary question is **deferred** until:

1. A subsequent 14-day window runs with the recorder and persistence live,
   under load comparable to the §129 representative-load floor (≥ 500
   agent_state writes/day).
2. The persisted client-side RPC series has enough samples to characterize
   p50/p99 rather than only hold-time proxy data.
3. RPC latency p95 for `lease.acquire.ok` is examined against the
   baseline that asyncpg-coupled handlers showed pre-`PR #218`
   ExecutorPool wrap. A 60× amplification signature at the lease
   boundary would falsify "no substrate tax here"; sub-100ms p95 would
   support "lease boundary is clean."

This is **measure-first**, not redraft. Per `feedback_redraft-cycle-bias-trap.md`, this doc does not propose any change to
the Wave 3 RFC; it adds the missing instrumentation that the v0.3.2
amendment's gate needed.

## Cross-references

- `beam-footprint-roadmap-v0.md` v0.3.2 amendment (2026-05-09) — names
  this gate
- `wave-1-window-evaluation-T0-2026-05-19.md` — sibling measurement
  track (Wave 1 §129 re-eval, separate gate)
- `surface-lease-plane-v0.md` §7.5 v0.9 — audit-mining precedent
- `CLAUDE.md §"Substrate Tax: anyio-asyncio Coupling"` — the
  amplification phenomenon this gate is meant to detect at the lease
  boundary
