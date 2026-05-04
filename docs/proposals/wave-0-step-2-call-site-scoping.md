# Wave 0 step 2 — coordination-failure call-site scoping

**Purpose:** scope the four `coordination_failure.*` event-type emit sites for `audit.coordination_events` (PR #342). One pass; no code yet — surface the design choices first so the implementation PR ships with operator-anchored decisions instead of inferences.

**Read this with:** `docs/proposals/beam-footprint-roadmap-v0.md` §86–110 (the envelope spec) and `src/coordination_events.py` (the emitter from #342).

---

## 1. `coordination_failure.asyncpg_connect_error` — clean chokepoint

**Where:** `src/db/postgres_backend.py:208-219` `_create_pool`.

```python
except asyncio.TimeoutError:
    raise ConnectionError(...)
except Exception as e:
    raise ConnectionError(f"Failed to connect to PostgreSQL ... {e}") from e
```

**Plan:** add `await emit_event(pool=None_or_self, service="governance_mcp", event_type=ASYNCPG_CONNECT_ERROR, payload={...})` before each `raise`. Payload: `{error_class, db_url_hash, timeout_s, attempt}`.

**Choice point — emit before the pool exists:** the very first connect error is the one we most want to log, but there's no pool to write to yet. Options:
- **(a)** Lazy retry — buffer the event in-memory and emit after the next successful connect lands. Lossless but couples observability to recovery.
- **(b)** Skip the bootstrap-failure case — only emit on subsequent connect errors after the pool has been up. Simple but misses the most diagnostic event.
- **(c) [recommended]** Use a separate short-lived `asyncpg.connect` direct-INSERT (no pool, no transaction) just for the event. Slow path for the failure case, which is fine — failures are rare; the cost is bounded.

## 2. `coordination_failure.anyio_cancellation` — scope question

**Where:** ~10 `except (asyncio.)CancelledError` sites across `src/background_tasks.py`, `src/knowledge_graph_lifecycle.py`, etc. Most are normal lifecycle (shutdown, cycle timeout, deliberate cancel).

**The problem:** emitting on EVERY `CancelledError` would be noisy and useless. The roadmap's intent is the *spurious* cancellations from the anyio-asyncio conflict (CLAUDE.md "Known Issue") — when the MCP SDK's task group cancels an asyncpg/Redis call mid-flight without a real shutdown signal.

**Choice point — what counts as "spurious":**
- **(a) [recommended]** Only emit when CancelledError fires INSIDE a known-conflict path: `_load_binding_from_redis` (identity_step.py), `deep_health_probe_task` (background_tasks.py:380), and any `run_in_executor` call that wraps a sync DB client. Pin the emit site list explicitly in the PR description; reviewer audits each.
- **(b)** Emit on every CancelledError, tag `payload.expected: True|False` based on whether the task name is in a known-shutdown allowlist. Higher signal volume, ambiguous classification.
- **(c)** Defer — wait for Wave 1's Sentinel-on-BEAM canary to surface real conflict events first, then instrument the specific paths it identifies. Punts the measurement we said we needed.

## 3. `coordination_failure.executor_pool_exhaustion` — needs probe target

**Where:** `src/db/executor_pool.py` is the dedicated background-thread pool that isolates asyncpg from anyio. "Exhaustion" here means tasks queueing on the pool's submit-queue beyond a threshold, or the executor loop falling behind.

**The problem:** the existing ExecutorPool doesn't have an exhaustion signal. It just submits coroutines to the executor loop — backpressure is invisible until the queue grows unbounded.

**Choice point — where to measure:**
- **(a) [recommended]** Add a counter at `ExecutorPool.submit` for `pending_count`; emit when `pending_count > threshold` (e.g., 50) within a 1-min window. Coarse but actionable. Threshold is operator-tunable.
- **(b)** Wrap `asyncio.wait_for` around each submit with timeout=2s; emit on TimeoutError. Catches latency spikes but fires on *any* slow query, not just exhaustion.
- **(c)** Defer — instrument when we have a real pool exhaustion incident to characterize. Punts.

## 4. `coordination_failure.mcp_handler_timeout` — clean chokepoint

**Where:** `src/mcp_handlers/decorators.py:108` `@mcp_tool` wrapper's `except asyncio.TimeoutError`.

```python
except asyncio.TimeoutError:
    logger.warning(f"Tool '{tool_name}' timed out after {timeout}s")
    return [error_response(...)]
```

**Plan:** add `await emit_event(pool, service="governance_mcp", event_type=MCP_HANDLER_TIMEOUT, payload={tool_name, timeout_s, elapsed_s, agent_id})` before the `return`. The pool is reachable via the existing handler-context (every MCP handler has a DB connection available).

**Choice point — secondary timeout sites:**
- `resident_progress.py:96` — wraps an INSERT in `asyncio.wait_for(_insert(), timeout=4.5)`. Emits a separate `coordination_failure.mcp_handler_timeout` with payload distinguishing tool-level vs sub-operation timeout.
- `identity_step.py:173` — Redis fallback path with 500ms timeout. Same event_type, payload.subtype="identity_step".

**Recommended:** ship the decorator chokepoint in PR A; secondary sites in a follow-up PR after the dashboard surfaces what the volume looks like.

---

## Recommended PR shape

**Wave 0 step 2A — clean chokepoints (small, low-risk):**
- `coordination_failure.asyncpg_connect_error` (path **1c** above)
- `coordination_failure.mcp_handler_timeout` at the decorator chokepoint only
- ~50 LOC + tests

**Wave 0 step 2B — anyio + executor (scoped, needs operator decisions):**
- `coordination_failure.anyio_cancellation` per **2a**
- `coordination_failure.executor_pool_exhaustion` per **3a** with tunable threshold
- ~150 LOC + tests + a tunable threshold env var

**Wave 0 step 2C — secondary timeout sites:**
- After step 3 (Chronicler projection) lands and dashboard shows timeout volume

Splitting 2A from 2B/C keeps the high-confidence emits unblocked by the scope-decision emits.

## Decisions you need to make

| # | Choice | My recommendation |
|---|---|---|
| 1 | Bootstrap-failure asyncpg event path | **1c** — short-lived direct-INSERT |
| 2 | Spurious CancelledError scoping | **2a** — pinned site list, audited |
| 3 | Executor exhaustion measurement | **3a** — pending_count threshold |
| 4 | Secondary timeout sites | Defer to step 2C |
| 5 | PR splitting | 2A + 2B + 2C as separate PRs |

If you accept the recommendations as a block, say "ship 2A" or "ship all" and I take it from there. If you want a different shape on any row, name it and I redirect.

---

**Cost estimate:** 2A is ~1hr including tests. 2B is ~3hr because of the threshold-tuning and the per-site audit on the CancelledError list. 2C waits on Chronicler projection (step 3). The roadmap's "days, not weeks" framing fits if 2A+2B land cleanly without re-scope churn.
