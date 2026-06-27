# Redis Retirement — v0

**Status:** Draft / scoping
**Author:** scoped 2026-06-27
**Scope:** the `unitares` server only. Decoupled from the BEAM Wave 3 roadmap — this can ship independently and does not gate on, nor is gated by, `beam-footprint-roadmap-v0.md`.

## Summary

Redis is a **gracefully optional accelerator** in the unitares server. It is the system of record for **nothing**: every role is either a speed layer in front of a durable Postgres store, or a single-node convenience with a non-Redis fallback. This proposal retires Redis entirely, removing a process, a dependency, and the unwrapped-async-client footprint called out in the anyio-asyncio substrate-tax notes (`CLAUDE.md`).

The motivating reframe: **migrating to BEAM does not let us drop Redis** (Wave 3 retires at most 2 of 6 roles and is deferred), but Redis was never load-bearing to begin with. The actual lever for removing it is moving the one role that matters — session persistence — fully onto Postgres, which **is already done at the write side and the read side**. What remains is deleting the cache in front of it.

## Current state (verified 2026-06-27)

- Redis is running and enabled by default (`redis_client.py:53`, unset `REDIS_ENABLED` → `"1"`; live process PID-confirmed, ~5.3M commands processed).
- Deployment is **single-node**: one LaunchAgent (`com.unitares.governance-mcp.plist`, :8767), no Sentinel, no second instance. The distributed-lock and distributed-cache roles therefore have no consumer that needs them.
- BEAM services (orchestrator, lease plane, wave3a, dispatch_beam) use **zero** Redis.

## Surface inventory

| # | Surface | Durable store today | Retirement work | Effort |
|---|---|---|---|---|
| A | Session cache (`src/cache/session_cache.py`) | ✅ `core.sessions` — written on first work (`persistence.py:583`), read back via `db.get_session` (`mixins/session.py:46`) | Delete the Redis read tier; the resolver **already** falls through to Postgres | **Near-free** |
| B | Transport binding (`middleware/identity_step.py`) | ❌ Redis is the only durable layer; no PG table | Drop durable layer (degrade to cold re-resolution) **or** add a PG table | **S (drop) / M (table)** |
| C | KG-store rate limiter (`src/cache/rate_limiter.py`) | ❌ "PG fallback" is a no-op (`return True`, lines 61-63) | Implement PG sliding-window on the existing `audit.rate_limits` table **or** accept 20/hr KG-store limit unenforced | **S + a decision** |
| D | Metadata cache (`src/cache/metadata_cache.py`) | ✅ clean direct-PG fallback already exists | Delete module; callers already read `core.agents` directly | **Trivial** |
| E | Distributed lock (`src/cache/distributed_lock.py`) | ✅ `fcntl` file-lock fallback is already the single-node path | Delete Redis branch | **Trivial** |
| F | Circuit breaker / metrics (`redis_client.py`) | n/a (observability of Redis itself) | Vanishes with Redis | **Free** |

## Surface A detail — why it's near-free

The async resolver `resolve_session_identity()` (`src/mcp_handlers/identity/resolution.py:399`) already consults tiers in order:

1. **PATH 1 — Redis** (`resolution.py:548-826`)
2. **PATH 2 — Postgres** `session = await db.get_session(session_key)` (`resolution.py:846`) — unconditionally runs after a Redis miss/unavailable, returns the binding with `source: "postgres"`, and refreshes TTL via `db.update_session_activity` (`resolution.py:947`).
3. PATH 2.8 — token rebind; PATH 3 — mint new.

So **every Redis miss already exercises the Postgres path today** — it is load-bearing and tested in production by definition. Retiring Surface A is: delete PATH 1, keep PATH 2.

The one piece of logic that lives only in the Redis path and must be preserved is the **S21-a mint-guard** (`persistence.py:246`, `_redis_slot_blocks_overwrite`): a PATH-3 mint must not overwrite an existing session slot bound to a *different* UUID. The Postgres equivalent already half-exists — `create_session` uses `ON CONFLICT (session_id) DO NOTHING` (`mixins/session.py:38`), which refuses to clobber an existing row. The guard's *read-side* check (compare existing binding's agent_id before mint) ports to a `db.get_session` call. This is the only non-mechanical part of A.

**Performance note:** PATH 1 (Redis) is ~1ms; PATH 2 (Postgres) is ~10–50ms under the ExecutorPool. Retiring Redis makes every resolution pay the Postgres cost. The in-process `_session_identities` dict (sync path) absorbs the repeat-hit case within a process. Per the operator's standing position, negligible latency on a correctness-preserving path is the wrong axis to optimize — but if the resolution round-trip is measured at a real multi-hundred-ms tax under load, the fix is an in-process L1 cache, not Redis.

## Decisions required

1. **Transport binding (B):** drop-to-cold-resolution vs. add `core.transport_bindings`. Recommendation: **drop.** It is a 2h sticky-identity optimization that already degrades to cold re-resolution whenever Redis times out (`_load_binding_from_redis`, 500ms budget). Within a process the in-memory dict covers stickiness; across a restart, first-request-per-client re-resolves correctly. A PG table is only warranted if cross-restart sticky identity is a relied-upon property.
2. **KG-store rate limiter (C):** enforce-in-PG vs. accept-unenforced. Context: the *general* tool rate limit (60/min, 1000/hr) is **in-memory, not Redis** (`src/rate_limiter.py`) and is unaffected. Redis enforces exactly one thing — 20 KG-stores/hr anti-spam — and turning Redis off today already silently disables it. Decide whether that protection justifies wiring `audit.rate_limits`.

## Sequencing (each step independently shippable)

1. **D + E + F** — pure deletions, zero behavior change. Proves the path and shrinks the dependency surface immediately.
2. **A** — wrap the existing PG session methods behind `SessionCache`, port the S21-a guard to `db.get_session`, run in shadow against Redis to confirm parity, then delete PATH 1.
3. **B** — apply the decision (recommended: drop).
4. **C** — apply the decision.
5. Remove `redis_client.py`, drop the `redis` dependency from `pyproject`, remove the Redis `asyncio.wait_for` guards (`identity_step.py`, `persistence.py`, `session.py`), update `CLAUDE.md`/`AGENTS.md` stack notes, and `brew services stop redis`.

## Non-goals

- Not touching the in-memory token-bucket rate limiter (no Redis dependency).
- Not part of, and not blocked by, BEAM Wave 3. Surfaces B and G in the Wave 3 RFC overlap conceptually but this proposal stands alone; if Wave 3 later lands, the metadata cache (D here / Surface G there) is already retired.
