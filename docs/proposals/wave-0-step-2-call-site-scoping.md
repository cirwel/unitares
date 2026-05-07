# Wave 0 step 2 — coordination-failure call-site scoping (v0.3 post-2A-pivot)

**Purpose:** scope the four `coordination_failure.*` emit families for `audit.coordination_events` (PR #342). v0.1 of this doc went out for council review (3-agent: dialectic-knowledge-architect, feature-dev:code-reviewer, live-verifier in parallel, adversarial framing). Council returned **1 BLOCK + 2 CONCERN-ONLY with near-BLOCK items** plus 4 factual REFUTED claims. v0.2 folds every finding.

**v0.3 pivot inheritance (read this first if you're picking up 2C-2 or later):**

The v0.2 prescriptions for §1.bootstrap (stderr + Chronicler sweeper), §1.runtime (direct asyncpg → `audit.coordination_events`), §3 (polling pending-counter on `ExecutorPool`), and §2.executor_loop (wrap a "main coroutine") are all **superseded by the 2A pivot** (PR #345) and downstream 3-agent council folds.

What actually shipped:

| Section | Original v0.2 design | What shipped (and where) |
|---|---|---|
| §1.bootstrap | stderr structured line + Chronicler sweeper (Wave 0 step 3) | Sync emit via `emit_coordination_failure_sync` → `audit_logger._write_entry` JSONL fallback (PR #366, Wave 0 step 2B) |
| §1.runtime | direct asyncpg → `audit.coordination_events` | Sync emit via same path; **2C-2 carved saturation out of `runtime`** into `executor_pool_exhaustion.acquire_timeout` (post-council reshape) |
| §2.background_task | wrap individual CancelledError catches | **Single emit at the OUTER supervisor done-callback** `_on_background_task_done` (PR #368, Wave 0 step 2C-1) |
| §2.executor_loop | wrap "main coroutine" + shutdown-flag discriminator | **Deferred** — `ExecutorPool._run_loop` is `loop.run_forever()` with no main coroutine; anyio teardown cannot propagate cancel across `run_coroutine_threadsafe` (CPython #105836). If ever wired, the family should be **`executor_loop_died.*`**, not `anyio_cancellation.executor_loop` (different failure class — premature `run_forever()` return / uncaught exception in `_run_loop`, NOT cancellation) |
| §3 | polling pending-counter + 30s check task | **Replaced** by event-driven saturation discriminator at the existing 2B wire site: when `pool_size == pool_max AND pool_idle == 0` at TimeoutError, emit `executor_pool_exhaustion.acquire_timeout`; otherwise stay on `asyncpg_connect_error.runtime` (PR for 2C-2). Drops the polling counter entirely — council BLOCKED it on counter-leak (production uses `__await__` path, `__aexit__` never fires; CancelledError in `__aenter__` strands the counter), threshold-unreachable (default `DB_POSTGRES_MAX_CONN=25` makes pending>50 structurally unreachable), and redundancy with 2B's already-shipped acquire-timeout emit |
| §4.tool_decorator | direct asyncpg in decorator | Sync emit (PR #345, Wave 0 step 2A — original pivot) |

The dedicated `audit.coordination_events` table from PR #342 stays unused for now; everything writes to `audit.events` with namespaced `event_type` strings. Wave 0 step 3 (Chronicler projection into the dedicated table) remains an option if/when a separate replay surface is genuinely needed.

The body below documents the v0.2 prescriptions as historical context. **Do not implement them as written** without checking against the v0.3 table above.

---

**Read this with:** `docs/proposals/beam-footprint-roadmap-v0.md` §86–110 (envelope spec), `src/coordination_events.py` (emitter from #342), the v0.1 council reports (in PR #342 thread).

## v0.1 → v0.2 council changes

| Finding | Origin | v0.2 fix |
|---|---|---|
| **BLOCK**: 1c "lossless" framing wrong — auth/refused/DNS fail same way on retry | code-reviewer | **Dropped 1c.** Bootstrap-failure path now writes a structured stderr line; Chronicler sweeps stderr into the table on next healthy connect. §1 below. |
| **BLOCK**: emitter raises inside `except` clauses → masks original exception | code-reviewer | **Mandatory** `try/except Exception: logger.warning(...)` wrapper at every wired site. Pattern locked in §"Mandatory wrapper pattern" below. |
| **REFUTED**: `_load_binding_from_redis` has NO `CancelledError` site — only `TimeoutError` | live-verifier | **Phantom site removed.** Real CancelledError sites in `background_tasks.py` substituted (verified: lines 415 + 423). §2. |
| **REFUTED**: `ExecutorPool` has no `submit()` method, no pending-count metric | live-verifier | **3a re-scoped** as "build the metric, then wire it." LOC estimate revised ~50→~120. §3. |
| **REFUTED**: `@mcp_tool` wrapper has no `agent_id` or pool reference | live-verifier | **Pool/agent_id source explicit** — wrapper extracts agent_id from `arguments`; pool fetched via module-level `get_pool()`. §4. |
| **REFUTED**: line anchors drifted (203-213 not 208-219; 193 not 173; 4 indexes not 3) | live-verifier | All anchors corrected throughout. |
| **C5 (near-BLOCK)**: `payload.subtype` violates §110 spirit — same anti-pattern one field deeper | architect | **Migration 035 regex bumped to `^(coordination_failure)(\.[a-z_]+)+$`** (sub-namespace allowed). All sub-discriminators land in event_type: `coordination_failure.mcp_handler_timeout.identity_step`, etc. |
| **C3 (CONCERN)**: mcp_handler_timeout + anyio_cancellation co-occur — no dedup story | architect | **`incident_id` UUID** added to payload contract. All events fired from one root cause share an incident_id. Dashboard-side aggregation does the dedup; no special server logic. §"Dedup contract" below. |
| **C1 (near-BLOCK)**: "spurious CancelledError" lacks structural discriminator | architect | Replaced pinned-list scoping with **explicit named sub-types per call site** (`coordination_failure.anyio_cancellation.background_task`, `.executor_loop`, etc.). Each site documents its discriminator at landing time; no implicit "spurious vs expected" classification. §2. |
| **C2 (near-BLOCK)**: Wave 1 exit criterion evaluable on incomplete data during 2A→2B→2C window | architect | **Wave 1 clock starts only when 2C lands**, per the new §"Wave 1 readiness gate" below. PR descriptions enforce this. |
| **N1 (architect)**: ~7 silent commitments not on decisions list | architect | All promoted. Decisions table now has 12 rows. |

## The four event_type families (v0.2)

Migration 035's regex now allows sub-namespaces (post-council change above). Wave 0 step 2 wires these specific event_types — all start with `coordination_failure.`:

| Family | Sub-types in Wave 0 step 2 | Source |
|---|---|---|
| `asyncpg_connect_error` | bootstrap, runtime | §1 |
| `anyio_cancellation` | background_task, executor_loop | §2 |
| `executor_pool_exhaustion` | acquire_pending_high_water | §3 |
| `mcp_handler_timeout` | tool_decorator, resident_progress, identity_step | §4 |

## Mandatory wrapper pattern (every wired site)

Every `await emit_event(...)` call MUST be wrapped:

```python
try:
    await emit_event(
        pool,
        service=...,
        event_type=...,
        payload={"incident_id": str(incident_id), ...},
    )
except Exception as emit_exc:  # noqa: BLE001 — observability MUST NOT mask the real bug
    logger.warning(
        "coordination_events emit failed (event_type=%s): %r — original exception preserved",
        event_type,
        emit_exc,
    )
```

Per code-reviewer BLOCK-2: every wired site is inside an `except` clause; an emitter that raises would replace the original `ConnectionError`/`TimeoutError`/`CancelledError` with the emit-failure traceback. The wrapper makes that impossible by structural discipline. Reviewers MUST reject any PR that emits without this wrapper.

## Dedup contract (incident_id)

When multiple event_type rows fire from one root cause (e.g., MCP handler timeout caused by anyio task-group cancellation that cancelled an asyncpg query), each emit's `payload.incident_id` MUST be the same UUID. Generated at the outermost emit site of the cluster; passed down via the exception chain (or via a contextvar if the chain is broken).

Dashboard / Sentinel rules aggregate by `incident_id` for true incident counts. Raw event_type counts remain useful for "which class fires most" but are explicitly NOT incident counts.

This is the only protection against double-counting that the doc commits to. Roadmap §129's "zero coordination-class incidents" is evaluated against `COUNT(DISTINCT payload->>'incident_id')`, not raw row count.

## Wave 1 readiness gate

Wave 1's exit criterion (roadmap §129: "the 14-day window AND the Wave 0 incident-feed must both hold") is now formally **gated on Wave 0 step 2C landing**. The 14-day window cannot start counting before all four event_type families have a wired emitter. Otherwise Wave 1 would pass on under-counted data — exactly what architect C2 surfaced.

PR descriptions for 2A and 2B MUST include a row in the test plan table:

| Wave 1 readiness | This PR contributes _______ event_type families. _______ remain before clock can start. |

---

## §1. `coordination_failure.asyncpg_connect_error`

**Two sub-types per call-site:**

### 1.bootstrap

**Where:** `src/db/postgres_backend.py:203-213` `_create_pool` (line range corrected post-live-verifier).

**Status of pool at raise:** None. `_create_pool` returns the pool to `_ensure_pool` (line 175); the assignment to `self._pool` happens after return. So at raise time, `self._pool is None`.

**Path:** stderr structured-log line, NOT a separate `asyncpg.connect`. Reasoning per architect C4 + code-reviewer BLOCK-1: a fresh connect with the same credentials would fail the same way for the most-diagnostic cases (auth, refused, DNS). The architecturally honest answer is "we cannot reach the table when the table's substrate is down."

**Stderr line format** (single-line JSON, parseable by Chronicler sweeper):

```json
{"_coord_event":true,"service":"governance_mcp","event_type":"coordination_failure.asyncpg_connect_error.bootstrap","payload":{"error_class":"OSError","db_url_hash":"abc123","timeout_s":5.0,"attempt":1,"incident_id":"<uuid>"},"context":{...}}
```

**Chronicler sweeper** (separate Wave 0 step 3 work — runs on its existing daily cadence): `tail -F /Users/cirwel/Library/Logs/governance-mcp-stderr.log | grep '_coord_event' | jq | INSERT into audit.coordination_events`. Buffered between sweeps; loss bounded to "events from windows where governance-mcp was down AND Chronicler hadn't run yet" — a small tail.

### 1.runtime

**Where:** every connection-acquire failure within an established pool (e.g., `asyncpg.InterfaceError` on `pool.acquire()`). Wired at the `ExecutorPool.acquire` failure path in `src/db/executor_pool.py`.

**Path:** normal `await emit_event(pool=self_or_global, ...)` — pool is established, table is reachable. Use the mandatory wrapper.

---

## §2. `coordination_failure.anyio_cancellation`

**Sub-types in Wave 0 step 2:**

### 2.background_task

**Where:** `src/background_tasks.py` — verified CancelledError catch sites at:
- line 78 (in `_supervised_create_task` outer guard)
- line 147 (in `wait_for` timeout-and-cancel path)
- line 172, line 178, line 213 (per-task except)
- line 415 + 423 in `deep_health_probe_task`

**Approach:** instrument the OUTER supervisor (`_supervised_create_task` line 78 area). Single emit point; payload carries `task_name` so per-task attribution lands without per-site instrumentation. Per architect C1 — explicit-named sub-type means future maintainers see the contract in the event_type, not in implicit "is this spurious" judgment.

### 2.executor_loop

**Where:** `src/db/executor_pool.py` — when the executor loop's main task receives a CancelledError that wasn't operator-initiated (e.g., GC cancelled the pool's lifecycle task during anyio teardown).

**Approach:** wrap the executor loop's main coroutine in a try/except that distinguishes shutdown-initiated cancel (a flag the operator sets) from external cancel.

**NOT a Wave 0 step 2 wired site:** `_load_binding_from_redis` (live-verifier REFUTED — function has no CancelledError catch, only TimeoutError; would require ADDING a behavior change to instrument). Re-evaluate after the dashboard surfaces actual anyio incident volume.

---

## §3. `coordination_failure.executor_pool_exhaustion`

### 3.acquire_pending_high_water

**Where:** `src/db/executor_pool.py`. Live-verified state: NO `submit()` method. Operations go through `acquire() -> _AcquireContext -> _await_on_loop`.

**Implementation shape (re-scoped post-council):**
- Add `self._pending = 0` + `threading.Lock` to `ExecutorPool.__init__`
- Increment in `_AcquireContext.__aenter__`, decrement in `__aexit__` (need to thread the counter ref into the context)
- Add `pending_count` property
- Periodic check task on the main loop: every 30s, if `pending_count > THRESHOLD` (env-tunable, default 50), emit ONCE per high-water episode (debounced — re-emit only after pending drops below threshold and rises again)

**LOC estimate (revised):** ~120 LOC + tests, NOT ~50. Includes `_AcquireContext` refactor to carry a counter ref.

**Re-evaluation gate:** if the implementation lift is genuinely > 200 LOC (e.g., `_AcquireContext` lifecycle is more entangled than the live-verifier surfaced), defer to "deferred-pending-real-incident" rather than ship a half-baked metric.

---

## §4. `coordination_failure.mcp_handler_timeout`

**Sub-types in Wave 0 step 2:**

### 4.tool_decorator

**Where:** `src/mcp_handlers/decorators.py:108` `@mcp_tool` wrapper's `except asyncio.TimeoutError`.

**Pool source (corrected post-live-verifier):** wrapper has NO context-injected pool. Use module-level `from src.db import get_pool` and call `get_pool()` inside the except path. CLAUDE.md anyio caveat applies — `get_pool()` MUST be already-initialized at this point (it is — handlers fire after pool init). Document this as a load-order assumption.

**agent_id source:** extract from `arguments.get("agent_id")` or `arguments.get("client_session_id")` if the tool injected it. May be None — that's fine, the column is nullable.

### 4.resident_progress

**Where:** `src/mcp_handlers/resident_progress.py:95` `await asyncio.wait_for(_insert(), timeout=4.5)` (line 95 not 96 — except clause is 96).

### 4.identity_step

**Where:** `src/mcp_handlers/middleware/identity_step.py:193` `_load_binding_from_redis` 500ms `wait_for` (line 193 not 173 per live-verifier REFUTED).

---

## Recommended PR shape (v0.2)

**Wave 0 step 2A — clean chokepoints:**
- 1.bootstrap (stderr structured log, no DB write)
- 1.runtime (ExecutorPool.acquire failure path)
- 4.tool_decorator
- ~80 LOC + tests + module-level `get_pool` documentation
- Wave 1 readiness: contributes 2/4 families (asyncpg_connect_error, mcp_handler_timeout)

**Wave 0 step 2B — anyio + executor:**
- 2.background_task (supervisor-level)
- 2.executor_loop
- 3.acquire_pending_high_water (with the counter refactor)
- ~150-200 LOC + tests
- Wave 1 readiness: contributes 2/4 families (anyio_cancellation, executor_pool_exhaustion)
- After 2B lands, Wave 1's 14-day clock can start

**Wave 0 step 2C — secondary timeout sub-types + Chronicler stderr sweeper:**
- 4.resident_progress, 4.identity_step
- Chronicler stderr-line → audit.coordination_events sweeper (carries 1.bootstrap events into the table)
- ~80 LOC + tests
- Wave 1 readiness: complete; dashboard panel can land in step 4

## Decisions you need to make (v0.2 — 12 rows)

| # | Choice | My recommendation |
|---|---|---|
| 1 | Bootstrap-failure asyncpg event path | Stderr structured-log + Chronicler sweep (post-council pivot) |
| 2 | Spurious CancelledError scoping | Per-site explicit sub-types; no implicit "spurious vs expected" classification |
| 3 | Executor exhaustion measurement | `pending_count` counter via `_AcquireContext` refactor (~120 LOC, not ~50) |
| 4 | Sub-namespace `event_type` instead of `payload.subtype` | Migration 035 regex amended (this PR); all sub-discriminators in event_type |
| 5 | Dedup of co-occurring events | `payload.incident_id` UUID; dashboard aggregates by it |
| 6 | Wave 1 readiness gate | 14-day clock starts only when 2C lands; PR descriptions enforce |
| 7 | Mandatory wrapper at emit sites | Yes — locked pattern, reviewer BLOCK if missing |
| 8 | Emit timing relative to original `raise` | After the `raise` would lose the event on process death; before the `raise` adds latency. **Recommendation: before** — observability beats latency in the failure path |
| 9 | Pool source at decorator chokepoint | Module-level `from src.db import get_pool`; load-order assumption documented |
| 10 | Sentinel rule design timing | Wait until 2C lands so rules can reference all 4 families |
| 11 | PR splitting | 2A (clean) + 2B (counter refactor) + 2C (secondary + sweeper) |
| 12 | Counter-refactor risk threshold | If 2B implementation crosses 200 LOC, defer 3 to "wait for real incident" rather than over-build |

If you accept the v0.2 recommendations as a block: "ship 2A". If you want to redirect any row, name it. If you want a third council pass on this revision, say so — the architect lane explicitly noted this is a "fold then re-pass" situation.

---

**Cost estimate (revised):** 2A is ~2hr including tests + Chronicler-sweeper-stub. 2B is ~5hr (the counter refactor is the variable). 2C is ~3hr. Roadmap's "days, not weeks" framing still fits if 2A+2B+2C land in sequence without re-scope churn.
