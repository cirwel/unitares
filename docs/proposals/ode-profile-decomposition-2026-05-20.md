# ODE profile decomposition + persistence — 2026-05-20

Anchors the load-bearing unknown from `beam-footprint-roadmap-v0.md`
v0.3 RESOLUTION (2026-05-05): *"ODE profile
(`process_update_authenticated_async`) is the load-bearing unknown —
runs in parallel with Wave 1, lands in v0.3.1 amendment, doesn't gate
Wave 1."* Eleven days passed; the v0.3.1 amendment never landed because
the instrumentation it would cite didn't exist. This branch adds the
instrumentation.

## What was already in place

`src/mcp_handlers/updates/phases.py:1044` wraps the ODE call with one
timer:

```python
_perf_record_ms("phases.ode_call_ms", (time.perf_counter() - _ode_start) * 1000)
```

That records the **total** wall-clock of
`mcp_server.process_update_authenticated_async`. The Wave 3 RFC §0
disconfirmer A′.1 is computable from this alone (`ode_call_ms /
total_ms`). But it does not answer the v0.3 RESOLUTION question — "what
*within* the ODE call accounts for the 7s remainder?" — because the
single bucket conflates auth, loop-detection, monitor setup, the actual
numpy compute, and PG persistence.

## What this branch adds

Five sub-timers inside `process_update_authenticated_async`
(`src/agent_loop_detection.py:353-560`):

| Key | Surface measured | Persisted? | Expected character |
|---|---|---|---|
| `ode.auth_ms` | `verify_agent_ownership` in executor | volatile only | I/O-bound; sub-50ms typical |
| `ode.loop_detect_ms` | `detect_loop_pattern` in executor | volatile only | CPU-bound, in-process state inspection |
| `ode.monitor_setup_ms` | `get_or_create_monitor` + `hydrate_from_db_if_fresh` | volatile only | First-call I/O; cache-hit otherwise |
| **`ode.numpy_step_ms`** | `monitor.process_update` in executor — numpy step (wall-clock includes executor queue-wait) | **yes (p50/p99)** | CPU-bound numpy work + executor queue-wait |
| `ode.persist_ms` | `increment_update_count` (PG atomic write via ExecutorPool); covers `get_db()` acquisition cost | volatile only | I/O-bound |
| `ode.persist_failed_ms` | sibling key when `increment_update_count` raises | volatile only | Failure rate signal; distinguishes "errored" from "never fired" |

Sub-timers feed the existing in-process `perf_monitor` ring buffer
(`src/perf_monitor.py`) — 1000 samples per key, p50/p95/p99/max exposed
via the snapshot endpoint.

**Honest gap:** only `ode.numpy_step_ms` reaches `metrics.series`. The
other four sub-timers are volatile-only — visible via the snapshot
endpoint for ad-hoc inspection, but they don't accumulate longitudinally.
That means the "residual that doesn't sum is event-loop scheduling
overhead" computation requires manual snapshot collection across a
window, not a SQL query. A follow-up that promotes the other four to
catalog entries can land when the first 7d of `numpy_step_ms` data has
identified which decomposition slice is load-bearing.

## Persistence

The catalog at `src/fleet_metrics/catalog.py` is intentionally a
high-bar surface ("answers a question the operator will actually ask
monthly"). Four entries earned their rent:

- `ode.numpy_step_ms.p50` / `.p99` — answers "is the numpy ODE step's
  wall-clock shrinking, stable, or growing?" The v0.3 RESOLUTION
  question. Renamed from the initial `ode.compute_ms` to make the
  executor-queue-wait inclusion explicit (council architect-lane finding).
- `lease_plane.client.v1.lease.acquire.p50` / `.p99` — answers "is the
  lease-boundary substrate-tax materializing?" The v0.3.2 amendment
  question. (Shared infra; persisted by this branch even though the
  recorder lives in `lease-plane-phase-a-latency`.)

`perf_monitor_persist_task` (`src/background_tasks.py:715+`, started at
server bootstrap alongside `coherence_monitoring_task`) samples
`perf_monitor.snapshot()` every 5 minutes and writes these four series
to `metrics.series` via `fleet_metrics.storage.record()`. The 5-minute
cadence matches Steward EISV-sync cadence; the 1000-sample ring buffer
absorbs ~85min of typical traffic before the p99 starts to drift to
recent-only.

Catalog-gated by design — perf_monitor may carry many keys; the
persistence task only writes those whose name maps to a registered
metric. New sub-timer? Add to both the catalog and
`_PERF_PERSIST_TARGETS`. Test (`tests/test_perf_monitor_persist.py`)
enforces both directions: every target's `metric_name` must be in the
catalog, and every target's `op_key` must be a recorded perf_monitor
key somewhere in `src/`.

## What this branch deliberately does not do

- Does not split the ODE numpy compute itself into sub-steps. The Wave 3
  re-attempt may want that (governance_core changes); this branch stops
  at the boundary between `agent_loop_detection.py` and the numpy
  module. Honest current-state: we will measure whether
  `ode.numpy_step_ms` is the load-bearing slice. If yes, a follow-up
  branch decomposes governance_core's ODE solver (and adds executor
  queue-depth sampling to disambiguate numpy from queue-wait).
- Does not redraft Wave 3 RFC. Per
  `feedback_redraft-cycle-bias-trap.md`, this is measure-first.
- Does not change ODE behavior. Pure observation.

## Falsifier

The v0.3 RESOLUTION's stop-sign was: *"ODE is 6+s of numpy compute"* —
meaning if the bulk of `process_update_authenticated_async` is
`monitor.process_update` itself, that's CPU-bound numpy work and the
substrate question becomes about NumPy/SciPy not Python event loops.

The four candidate readings, all directly checkable from the new
series in 7+ days of steady traffic (caveats below):

| Reading | Signature | Substrate-question consequence |
|---|---|---|
| ODE is numpy-or-executor-bound | `ode.numpy_step_ms.p99 > 6000` and other phases sub-100ms | Either numpy is slow OR default executor pool is saturated under load — same wall-clock surface; disambiguate by sampling executor queue depth alongside |
| ODE is asyncpg-bound | `ode.persist_ms.p99 > 5000` and rest fine | ExecutorPool isn't isolating; the bug class is still alive on this surface |
| ODE is event-loop-bound | All sub-timers sum to << `phases.ode_call_ms` (legacy outer-call timer in `phases.py:1062`) | Substrate-tax IS the asyncio/anyio scheduling residual; supports v0.3 destination commitment |
| ODE is monitor-setup-bound | `ode.monitor_setup_ms.p99 > 1000` | Cache pathology, not substrate |

**Readings are not mutually exclusive.** Numpy-bound and executor-bound
can both be true simultaneously; the first row of the table captures
their shared signature without resolving between them. Resolving
requires executor queue-depth instrumentation that this branch does
*not* add — that's a follow-up if/when the first row's signature fires.

This makes the v0.3 RESOLUTION question structurally falsifiable for
the first time. The legacy `phases.ode_call_ms` (whole-call timer in
`phases.py:1062`) is preserved; together with `ode.numpy_step_ms` and
the volatile sub-timers, the residual computation is well-defined.

## Cross-references

- `beam-footprint-roadmap-v0.md` v0.3 RESOLUTION (2026-05-05) — names
  this gate
- `beam-wave-3-handler-dispatch.md` §B1.2 — Wave 3 RFC A′.1 disconfirmer
  (the single-timer ratio this branch refines)
- `wave-1-window-evaluation-T0-2026-05-19.md` — sibling §129 measurement
  gate
- `lease-plane-phase-a-latency-2026-05-20.md` — sibling lease-boundary
  measurement gate; shares the persistence infra introduced here
- `CLAUDE.md §"Substrate Tax: anyio-asyncio Coupling"` — the
  amplification phenomenon `ode.numpy_step_ms` vs sum-of-other-phases is
  meant to detect or rule out
