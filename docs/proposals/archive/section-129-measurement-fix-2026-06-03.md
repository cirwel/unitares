# §129 measurement-gate fix — 2026-06-03

Status: fix of the Wave 1 condition-1 / substrate-question measurement gate, after the T+14 checkpoint (2026-06-02) came due and the gate read inconclusive. Council-passed (architect + reviewer + live-verifier, parallel). Does **not** make a Wave 3 / substrate-migration recommendation — it restores the gate so that decision rests on a working measurement.

## Why

The 2026-05-20 measurement gates (#479/#480/#481) anchored a re-evaluation at T+14 = 2026-06-02. Running `scripts/dev/section_129_reeval.py` on 2026-06-03 returned **FAIL with `incident_id` 0/70** — but "FAIL" here meant "the gate cannot trust itself," not "incidents were found." Investigation found the gate was **doubly broken**, in a direction that would have manufactured pro-migration evidence if half-fixed.

## Two bugs (council-confirmed, live-verified)

### Bug 1 — nesting blindness

`emit_coordination_failure_sync` (`src/coordination_failure_emit.py`) stores the caller payload under `AuditEntry.details["payload"]`, and `audit_db.py` maps `details` → the `audit.events.payload` column. So the stored shape is `{"service": …, "payload": {…, "incident_id": …}}` and `incident_id` lives at `payload->'payload'->>'incident_id'`. §129 queried `payload->>'incident_id'` (top level) → **blind to a field present on every row** (verified: 69/69 in-window `anyio_cancellation.background_task` rows carry it nested; 0 at top level).

Fix: query the nested path in **both** the `DISTINCT` count **and** the `?` existence filter, in **both** `check_zero_incidents` and `check_incident_id_wired`. Storage was **not** normalized — `audit.events.payload` is a heterogeneous column shared by dozens of event types; the spec's flat-path language (`wave-0-step-2-call-site-scoping.md:83`) was the error, not the storage shape.

**Divergence to remember:** the dual-write to `audit.coordination_events` stores the caller payload **flat**, so a query *there* uses `payload->>'incident_id'`. The nested path is `audit.events`-only. (Verifier: `audit.coordination_events` currently carries no `incident_id` rows at all.)

### Bug 2 — graceful-shutdown cancellations counted as substrate incidents

`_on_background_task_done` emitted `coordination_failure.anyio_cancellation.background_task` on **every** supervised-task cancellation, including graceful shutdown, where all ~N tasks are cancelled at once. The 14-day window's 70 rows were **69 such cancellations** which the live-verifier collapsed to **8 sub-5ms fanout bursts** (= 8 server restarts; task names all housekeeping: `matview_refresh`, `audit_log_rotation`, `auto_ground_truth_collector`, …). Several were caused by this session's own MCP cutover restarts.

Had only Bug 1 been fixed, §129 would count **69 distinct "incidents"** → condition-3 FAIL → a spurious read of *"anyio substrate-coupling is firing"* → false **pro-BEAM** evidence.

Fix: a one-way `_background_tasks_shutting_down` latch set at the top of `stop_all_background_tasks()` (before the cancel loop, so the done-callbacks fired during the gather-await see it); the cancellation emitter suppresses when set. **Runtime-anomalous** cancellations (server up — e.g. `cancel_and_respawn_task`) still emit, preserving the genuine substrate signal. Tests cover both: shutdown suppresses, runtime cancel still emits.

## What the corrected gate reads

- Nesting fix verified live: `rows_with_incident_id` 0 → 69; `distinct_incidents` 0 → 69.
- The 69 are historical shutdown noise (already written; the emit-latch only prevents *future* ones), so the **historical** window still shows them. The verifier's fanout analysis establishes the **true substrate-tax incident count for the window is 0**.
- `coordination_failure.mcp_handler_timeout.tool_decorator` (the substrate-tax signature) fired **0 times in 14 days** (223 all-time, last burst 2026-05-04, gone after the locked-update perf fix). Combined with `ode.numpy_step_ms` p50 22ms / p99 44ms and sub-second p50 at 16 concurrent agents (v0.3.1b), **the latency/substrate-tax justification for Wave 3 remains dissolved.**

## The honest caveat (architect lane) — carry this into any Wave 3 call

A corrected `distinct_incidents = 0` is the **expected** reading for a healthy Python substrate, but the measurement has **low coverage**: 5 of 6 wired `coordination_failure` sub-types have **zero production fires ever**, and the one that historically fired stopped after a perf fix. A zero therefore means *"no instrumented failure mode fired,"* **not** strong *"stay Python"* evidence. The fix makes the gate **non-spurious**, not **complete**. Wave 3, if pursued, rests on the **coordination/ownership / lock-dissolution** architectural argument (A′, operator-committed), not on this latency-class signal.

## Residual follow-ups (out of scope here)

1. `coordination_failure.wave_3a.fallback` (a third emitter, `src/wave3a_beam_proxy.py`) carries **no** `incident_id`, so §129's `rows_with_id < raw_rows` caveat will fire whenever a fallback occurs even in a clean window. The dedup contract says every coordination_failure emit should carry it; extend the proxy emitter. Distinct emitter, not in this council's scope.
2. The real §129 read needs a **fresh forward window** under representative load (≥500 `core.agent_state` writes/day) now that the gate is trustworthy — the original spec's path-1.
