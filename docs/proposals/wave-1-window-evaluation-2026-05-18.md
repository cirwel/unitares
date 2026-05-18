# Wave 1 14-day window evaluation — 2026-05-18 (T+13)

Status: read-only operational evaluation of one of four Wave 1 exit conditions, with a process finding the data forces into view. Does NOT recommend Wave 1 close.

This draft was council-passed (architect + reviewer + live-verifier in parallel) on 2026-05-18; v0.1's framing of "literal reading: passes" was rejected by the architect lane as a status-quo-bias artifact and v0.1's Caveat 1 description of which emit sites lack `incident_id` was refuted by the reviewer lane. Both are corrected below.

## Scope

`docs/proposals/beam-footprint-roadmap-v0.md` lines 493–496 list **four** Wave 1 exit conditions:

1. Zero coordination-class incidents in the Wave 0 instrumentation feed.
2. Alarm rule parity with the Python Sentinel implementation.
3. Supervision tree absorbs at least one induced fault without manual intervention.
4. Anti-enthusiasm guard: *"the operator does not declare success on enthusiasm — the 14-day window and the Wave 0 incident-feed must both hold before Wave 1 closes."*

Conditions 2 and 3 are engineering work tracked elsewhere; this doc is silent on them.

Condition 4 is **not an independent criterion** — it is a stop-sign that warns specifically against the kind of "criterion technically passes, ship it" reading that this evaluation is intended to refuse. It applies to the operator's decision process about condition 1, not to a measurement.

This doc evaluates **condition 1 only**, under guard from condition 4.

## Window definition

- T+0 = 2026-05-05 (Wave 0 step 2C closed: PRs #366, #368, #369 on this date; #371 same day added the `executor_loop_died` namespace).
- T+14 = 2026-05-19 (tomorrow). Today is T+13.
- Criterion as written (wave-0-step-2-call-site-scoping.md:83): *"zero coordination-class incidents"*, evaluated against `COUNT(DISTINCT payload->>'incident_id')` on `audit.events` filtered by `event_type LIKE 'coordination_failure.%'`.

## Headline

**Condition 1 cannot be honestly evaluated from this window's data.** The metric `COUNT(DISTINCT payload->>'incident_id')` returns 0 not because no incidents occurred, but because the only emit site that actually fired during the window does not populate `incident_id`. The window's "0 incidents" is a measurement-shape artifact, not a measurement.

The supporting evidence below explains why.

## Raw counts (verified)

Queried `audit.events`, T+0 through today:

| Metric | Value |
|---|---|
| `count(*)` where `event_type LIKE 'coordination_failure.%'` | **203** |
| `count(DISTINCT payload->>'incident_id')` | **0** |
| Last coordination-failure event timestamp | 2026-05-05 07:47:37 (T+0, 07:47 local) |
| Days since last event | 12 days, 18+ hours |
| Distinct `event_type` values that fired in window | **1** (`coordination_failure.mcp_handler_timeout.tool_decorator`) |

Daily breakdown: all 203 events on T+0, then zero through T+13.

For context — `audit.events` overall traffic (any event_type) ran **824 to 16,786** rows/day during the same window, no gaps. The server was up and processing throughout. The 824 floor was on 2026-05-12, the first day the operator went AFK to Mercor; the 16,786 ceiling was on T+0.

`core.agent_state` writes/day (proxy for active agent check-in load):

| Date range | Avg writes/day |
|---|---|
| T+0 → T+6 (2026-05-05 → 2026-05-11) | 840 |
| T+7 → T+13 (2026-05-12 → 2026-05-18) | 51 |

A **~16× load drop** mid-window, tracking the operator going AFK. Held T+0→T+6 represents the "normal load" reference; T+7→T+13 represents an unrepresentatively quiet sub-window.

## Caveats — why the 0 means nothing

### Caveat 1 — the only firing emitter omits `incident_id`; the dedup contract is implemented elsewhere

The contract (wave-0-step-2-call-site-scoping.md §"Dedup contract") requires `payload.incident_id` (UUID) at every emit site, so dashboards can dedup cascades by it. Audit of the five wired emit sites:

| Emit site | event_type | `incident_id` in payload? |
|---|---|---|
| `src/mcp_handlers/decorators.py:163` | `coordination_failure.mcp_handler_timeout.tool_decorator` | **NO** |
| `src/db/postgres_backend.py:76` (via `_emit_bootstrap_coord_failure`, called at lines 312/319) | `coordination_failure.asyncpg_connect_error.bootstrap` | yes |
| `src/db/postgres_backend.py:352` (via `_emit_runtime_coord_failure`) | `coordination_failure.executor_pool_exhaustion.acquire_timeout` OR `coordination_failure.asyncpg_connect_error.runtime` (branches on pool saturation) | yes |
| `src/background_tasks.py:865` (via `_emit_background_task_cancellation`) | `coordination_failure.anyio_cancellation.background_task` | yes |
| `src/db/executor_pool.py:54` (via `_emit_executor_loop_died`) | `coordination_failure.executor_loop_died.{uncaught,premature_return}` | yes |

Four of five sites correctly include `incident_id`. The fifth — the `@mcp_tool` decorator timeout emitter — does not. It is the **only emit site that produced any events in the 14-day window**.

`COUNT(DISTINCT payload->>'incident_id')` therefore returns 0 for this window regardless of how many real incidents occurred, because every row that fired came from the one emitter without the field. The 0 is not a measurement of incidents; it is a measurement of `NULL`. The dedup contract is structurally fine but operationally vacuous for any window dominated by the decorator emitter.

Targeted fix: add `incident_id` to the decorator emit payload (`src/mcp_handlers/decorators.py:163`). Single site, ~2 lines.

### Caveat 2 — five of six in-scope event_types have zero production fires ever

Including the `_emit_runtime_coord_failure` branching, **six** sub-types are reachable from the wired emit sites:

| Event type | Production fires (all-time) |
|---|---|
| `coordination_failure.mcp_handler_timeout.tool_decorator` | 223 (203 in window; 20 on 2026-05-04 before window opened) |
| `coordination_failure.asyncpg_connect_error.bootstrap` | 0 |
| `coordination_failure.asyncpg_connect_error.runtime` | 0 |
| `coordination_failure.executor_pool_exhaustion.acquire_timeout` | 0 |
| `coordination_failure.anyio_cancellation.background_task` | 0 |
| `coordination_failure.executor_loop_died.{uncaught,premature_return}` | 0 |

A 1-of-6 production fire rate admits three readings:

1. Those failure modes genuinely don't manifest in this workload (best case).
2. The emit sites are wired but unreachable in practice — e.g., asyncpg pool is bootstrapped once and stays up, background tasks rarely cancel, executor loop is robust.
3. The bug class manifests on surfaces the six sub-types don't cover.

This window's data cannot distinguish (1) from (2) from (3). The 60× amplification finding (CLAUDE.md §"Substrate Tax: anyio-asyncio Coupling", measured 2026-05-04 on the governance-MCP request path) is exactly the kind of substrate-coupling pathology that should produce events somewhere in this table; that it produced events on only one sub-type is itself a hypothesis the data cannot resolve.

### Caveat 3 — the window held under load that may not exercise the bug class

The 16× drop in agent_state writes between T+6 and T+7 means the second half of the window ran under sharply reduced load. The 60× amplification is a per-call effect on the governance-MCP request path; whether it is also load-correlated (handler contention, pool acquisition under concurrency) or amplifies per-call regardless of fleet load is **not established in this repo's measurement record**. Treating the second half of the window as equivalent to the first overstates what was tested.

Order-of-magnitude check on the decorator emitter alone: 203 fires on T+0 under ~840-writes/day load. Normalized to T+7→T+13's ~51-writes/day load (and assuming linear scaling, which is the most generous assumption for a coupling bug), the expected count over 7 days is ~85 events. Observed: 0. The gap *could* be real abatement, but it could also be other things: load not linear, the locked_update perf fix (PR #372, merged the same afternoon) reduced timeout pressure below the 15s threshold, or the bug shifted to a non-instrumented surface (Caveat 2 path 3).

### Caveat 4 — `audit.coordination_events` exists but is empty

A dedicated partitioned table `audit.coordination_events` (with partitions through 2026-06) is registered in the schema with shape identical to `audit.events`. It has **zero rows** across all partitions; it has never received a write. Production emits route to `audit.events` filtered by `event_type LIKE 'coordination_failure.%'`. This is the schema-routing drift the roadmap notes at line 308 — consistent with the bug class itself blocking the dedicated-table path — but worth surfacing here because any future re-wiring of emit sites must commit explicitly to one sink or the other. The current ambiguity invites a future PR landing emits to the empty table and silently leaving §129's query against `audit.events` returning 0 for a different reason.

## Process finding — the dedup field shipped missing at the one site that fires most

The §129 contract was specified in wave-0-step-2-call-site-scoping.md as mandatory, with the explicit note that `incident_id` deduplication is the only protection against double-counting. Step 2C closed on 2026-05-05 (PRs #366/#368/#369), and PR #371 added a fifth namespace the same day. Four of those landed with the dedup field; the decorator emitter, which had been emitting since at least 2026-05-04 and was the highest-volume firing site, did not get the field added in any of the wave-0 step 2 PRs.

Reading this charitably: it is a one-site oversight that did not block step 2's structural contributions. Reading it less charitably: the council/review chain that gated step 2 enforced the wrapper pattern and the regex on namespace shape, but did not catch that the highest-volume emitter would silently produce un-dedupable rows in the only criterion the work was supposed to make evaluable. The next §129 evaluation will be on data the work as-shipped cannot produce.

This is operational data for the substrate question, not just a Python-side bug list. Wave 0's stated value was to make later waves' exit criteria honestly evaluable (roadmap line 304). It demonstrably did not, on its own central criterion, in its first 13 days. That is information.

## Falsifier — what would flip the recommendation

Per `feedback_substrate-migration-status-quo-bias`: the author has a known bias toward refusing substrate migrations across sessions. To prevent this evaluation from accumulating caveats indefinitely without a forcing decision, here is the concrete condition under which condition 1 should be considered substantively met (independent of conditions 2 and 3, which need their own work):

**Substantive pass condition for §129/condition 1:**

1. `incident_id` is wired in the decorator emit payload (`src/mcp_handlers/decorators.py:163`), AND
2. A subsequent 14-day window runs under load comparable to T+0→T+6 (agent_state writes ≥ 500/day averaged across the window), AND
3. `count(DISTINCT payload->>'incident_id')` over that window is zero.

If these three hold, condition 1 has been honestly tested at representative load with a metric the data can produce. If condition 3 fails, the substrate-question evidence becomes concrete: a counted incident is what AMENDMENT 2026-05-04 said the Wave 0 channel was set up to capture.

**Symmetric falsifier — what would update toward closing condition 1 without the additional window:** if step 2's design is amended to accept the as-shipped contract (e.g., evaluate against raw `count(*)` with a documented temporal-clustering dedup, applied retroactively to the current window's 203 events), and the resulting incident count is zero, condition 1 passes on the current data. This is a bias-symmetric alternative: do not insist on new evidence if a less brittle reading of the existing evidence resolves the question.

## What this evaluation supports

- **Condition 1 status: unresolved.** Cannot be honestly evaluated from this window. Two paths to resolution above (new window with the fix, or amend the contract).
- **Wave 1 close: not recommended.** Independent of conditions 2 and 3, condition 1 has not been tested.
- **Substrate question status: weakly updated, in the direction that Python-side coordination instrumentation work has high iteration cost.** The dedup field shipped missing at the one site that fires most, across a council-reviewed step. This is one data point, not a decision.

## Recommended follow-ups (priority order)

1. **One-site fix**: add `incident_id` to `src/mcp_handlers/decorators.py:163` emit payload. Smallest possible PR; restores criterion-evaluability for any future window.
2. **Commit to one audit sink**: either backfill the doc to declare `audit.events` canonical and deprecate `audit.coordination_events`, or wire all emits to the dedicated table. Either is fine; the current both-and is the avoidable mistake.
3. **Run a representative-load window**: once (1) lands and the operator returns from Mercor, the next 14-day window with ≥500 writes/day average is the real §129 evaluation.
4. **(Independent of this doc)** Conditions 2 and 3 still need their own evaluation before any Wave 1 close. They are not in scope here.

A note on the bias the author is operating under: `feedback_substrate-migration-status-quo-bias` flags that I reliably resist substrate migrations across sessions. The "Recommended follow-ups" above all keep the work on the Python side; the symmetric reading would be that 13 days into a 14-day window the dedup field was missing at the one site that fires most, and the substrate-tax surface produced the iteration cost that made that miss possible — which is itself a substrate-question data point. The falsifier section above is the structural answer; the recommended follow-ups are the operational answer. The operator should weigh both.

## Source queries

```sql
-- Window-filtered: criterion as written + raw row count
SELECT count(DISTINCT payload->>'incident_id') AS distinct_incidents,
       count(*) AS raw_rows,
       min(ts), max(ts)
FROM audit.events
WHERE event_type LIKE 'coordination_failure.%'
  AND ts >= '2026-05-05 00:00:00-06';

-- All-time event_type distribution (Caveat 2)
SELECT event_type, count(*)
FROM audit.events
WHERE event_type LIKE 'coordination_failure.%'
GROUP BY event_type;

-- Overall server liveness (proves absence is not artifactual)
SELECT date_trunc('day', ts) AS day, count(*) AS events
FROM audit.events WHERE ts >= '2026-05-05'
GROUP BY day ORDER BY day;

-- Load proxy (Caveat 3 normalization)
SELECT date_trunc('day', recorded_at) AS day, count(*) AS state_writes
FROM core.agent_state WHERE recorded_at >= '2026-05-05'
GROUP BY day ORDER BY day;

-- Confirm audit.coordination_events is empty (Caveat 4)
SELECT count(*) FROM audit.coordination_events;
```
