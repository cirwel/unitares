# Redis Retirement — v0.1

**Status:** Draft / scoping — **central claim of v0 REFUTED by live verification (2026-06-27); corrected here.**
**Author:** scoped 2026-06-27
**Scope:** the `unitares` server only. Decoupled from the BEAM Wave 3 roadmap — this can ship independently and does not gate on, nor is gated by, `beam-footprint-roadmap-v0.md`.

> **v0 → v0.1 correction.** v0 claimed Redis "is the system of record for nothing" and that retiring the session cache was "near-free" because Postgres already mirrors sessions. **Direct measurement against the running system refuted this.** Live state: **920 Redis session keys vs. 57 `core.sessions` rows — ~94% of active sessions exist only in Redis** (347 of them permanent, TTL=-1). The Postgres mirror is only written on the `persist=True` onboard/first-work path; the *default* resolution path (`persist=False`, used by `identity()` and middleware auto-mint) writes Redis + in-process dict only. So **today Redis is the de-facto primary store for most session/identity state.** Retirement is still feasible and worthwhile, but the order inverts: **build the durable Postgres write-through FIRST, run it until PG genuinely mirrors Redis, then retire.** This document is corrected accordingly.

> **v0.1 → topology decision (operator, 2026-06-28).** The keep/drop scope below was implicitly a bet on process topology. **Decision: the server's permanent target is multi-process — single-process is never the long-term shape.** That changes the endpoint, not the immediate work:
> - The observation "Redis is server-internal, not a cross-process bus" (§Current state) is true **only for today's single process**. Under the multi-process target Redis *becomes* the cross-process coordination + shared-TTL layer, which is exactly what it is good at.
> - **The goal was never "remove Redis." It is "evict the durable data Redis should not be holding."** Endpoint = **two stores, each its right job**: durable identity/session/transport bindings → Postgres (the Phase 1 mirror work — **unchanged and still correct**); ephemeral + cross-process coordination (metadata cache, distributed lock, rate limiter, onboard-pin TTL, dedup) → Redis, **permanently**.
> - **This strikes Phase 2's "remove `redis_client.py` / drop the `redis` dependency / `brew services stop redis`"** and reclassifies **Surface D (metadata cache)** and **Surface E (distributed lock)** as **NOT safe deletions** (see row notes). Under multi-process, per-worker in-process copies *break*: cache = N× the ~17s hydrate tax + cross-worker stale skew; `src/cache/rate_limiter.py` = each worker independently allows the full limit → real ceiling N× configured (a correctness bug); `distributed_lock` = no actual mutual exclusion. Phase 0's "safe deletions now" therefore shrinks to genuinely dead code only (the `identity_notifications` reader, the false `rate_limiter` PG-backend comment).
> - **New prerequisite the decision creates (separate from retirement):** going multi-process requires auditing every in-process global that is currently load-bearing and confirming its cross-process backstop — notably the **`_session_identities` slot guard** (`persistence.py:116-121`, today's load-bearing mint guard) and the in-memory `src/rate_limiter.py`. The slot guard's cross-process backstop is `create_session`'s `ON CONFLICT DO NOTHING` (argued *stronger* in §S21-a) — verify that before worker #2 ships, do not assume it.

## Summary

Redis in the unitares server is **not the intended system of record** — BEAM (the coordination destination) uses none, and every role *should* be a cache over Postgres. But it has silently become the **de-facto primary store** for the majority of session, transport-binding, and onboard-pin state, because the dual-write to Postgres was only ever wired on the onboard/write path, not the default resolve path. Retiring Redis is a real data-path migration, not a deletion.

The motivating reframe still holds: **migrating to BEAM does not let us drop Redis** (Wave 3 retires at most 2 of 6 roles and is deferred). But the converse — "Redis isn't load-bearing today" — is false. It is load-bearing right now for ~94% of session bindings.

## Current state (verified live 2026-06-27)

Redis is running, enabled by default (`redis_client.py:53`), single-node (one LaunchAgent on :8767, no Sentinel). Live key census (`redis-cli --scan`, 992 keys):

| Prefix | Count | Postgres mirror? |
|---|---|---|
| `session:` | 920 | **NO for ~94%** — only 57 rows in `core.sessions`; 50/50 sampled Redis keys had no PG row. `core.agent_sessions` = 0 rows. 347 keys are TTL=-1 (permanent). |
| `agent_meta:` | 54 | yes — pure cache over `core.agents` |
| `transport_binding:` | 10 | **NO** — `core.transport_bindings` table does not exist |
| `recent_onboard:` | 4 | **NO** — no PG equivalent (missed in v0) |
| `unitares:metrics:` | 2 | n/a (Redis self-observability) |
| `rate_limit:` | 1 | **NO** — fail-open, no PG backend |
| `kg_surfaced:` | 1 (set) | **NO** — no PG equivalent (missed in v0) |
| `lock:` | 0 | yes — `fcntl` file fallback |

Resident agents (Sentinel/Vigil/Watcher/Steward) hold **zero** Redis connections (verified: no `import redis` / `get_redis` in `agents/`). Redis is server-internal, not a cross-process bus. BEAM services use zero Redis.

## Surface inventory (corrected)

| # | Surface | Real durable store today | Retirement work | Effort |
|---|---|---|---|---|
| A | Session cache (`src/cache/session_cache.py`) | ⚠️ **Redis is primary for ~94%**; `core.sessions` holds only the onboarded subset | Build write-through so EVERY resolve (incl. `persist=False`) lands in PG; carry the rich fields; preserve S21-a guard; shadow; then cut PATH 1 | **M–L (was mislabeled near-free)** |
| B | Transport binding (`middleware/identity_step.py`) | ❌ Redis is sole store; no PG table | Drop (cold re-resolution) **or** add PG table | **S (drop) / M (table)** |
| C | KG-store rate limiter (`src/cache/rate_limiter.py`) | ❌ "PG fallback" is a no-op (`return True`, 61-63) — and the comment claiming a PG backend is false | Wire `audit.rate_limits` **or** accept 20/hr unenforced; delete the misleading comment regardless | **S + a decision** |
| C2 | **Onboard pin** (`recent_onboard:*`, `identity/session.py:879-1003`) — **NEW, missed in v0** | ❌ Redis sole store | Sole session-routing for IP:UA-fallback clients (Claude Desktop, REST w/o `client_session_id`). Drop → fresh session key every call → S21 ghost-fork for that population. **Must port to PG or in-process TTL store.** | **M — blocker** |
| C3 | **PATH 1 fingerprint hijack check** (`resolution.py:651-753`) — **NEW, missed in v0** | n/a | `identity_hijack_suspected` enforcement (`UNITARES_PREFIX_BIND_FINGERPRINT`) lives ONLY in the Redis fast path. Retiring PATH 1 silently disables it; PATH 2 has no equivalent. **Must port to PATH 2 first.** | **M — blocker** |
| C4 | **KG surfaced dedup** (`kg_surfaced:*`, `enrichments.py:1583-1616`) — **NEW, missed in v0** | ❌ Redis sole store (fail-open) | Drop → duplicate KG notifications every check-in. Behavioral regression, not correctness. | **S** |
| D | Metadata cache (`src/cache/metadata_cache.py`) | ✅ clean direct-PG fallback (verified) | ~~Delete module~~ **KEEP — cross-worker cache under multi-process (see topology decision); in-process copy = N× hydrate tax + stale skew** | ~~Trivial — safe now~~ **not a deletion** |
| E | Distributed lock (`src/cache/distributed_lock.py`) | ✅ `fcntl` file fallback is the single-node path | ~~Delete Redis branch~~ **KEEP — `fcntl` is single-node only; this is the correct cross-process primitive once workers contend** | ~~Trivial — safe now~~ **not a deletion** |
| F | Circuit breaker / metrics (`redis_client.py`) | n/a | Vanishes with Redis | **Free** |

`identity_notifications:*` (read at `enrichments.py:1795`) is **dead code** — no writer exists. Remove on cleanup, no migration needed.

## Why Surface A is NOT near-free (the v0 error)

`resolve_session_identity()` (`resolution.py:399`) does fall through PATH 1 (Redis) → PATH 2 (`db.get_session`, `resolution.py:846`) → PATH 3 (mint). v0 concluded "delete PATH 1, keep PATH 2." The flaw: **PATH 2 only finds a row for sessions that were persisted**, and the default mint path is `persist=False` (`identity()` → `handlers.py:616`; middleware auto-mint → `identity_step.py:844`), which returns `ephemeral`/`memory_only` and writes **no** `core.sessions` row (`resolution.py:1274-1299`). Live data confirms the consequence: 94% of session keys have no PG row. Cutting PATH 1 today routes those to PATH 3 → cold re-mint → the S21 ghost-fork pattern at fleet scale.

Additionally, the Redis session payload carries `trajectory_required`, `spawn_reason`, `bind_ip_ua`, and `bound_at` that `core.sessions` does not store (verified: `metadata={}` on a live row), and `agent_id` is a **UUID in Redis but a display label in PG `client_info`** — a read-path correctness hazard for any naive fallback.

**S21-a note:** the Redis slot guard (`_redis_slot_blocks_overwrite`, `persistence.py:304`) was never atomic (`get` then `setex`, last-writer-wins). The load-bearing guard is the in-memory `_session_identities` check (`persistence.py:116-121`), and `create_session`'s `ON CONFLICT (session_id) DO NOTHING` (`mixins/session.py:38`) is *stronger* than Redis here. So moving the guard to PG does **not** reintroduce the ghost-fork race — but the call site at `persistence.py:583` currently **ignores `create_session`'s return value**; the port must check it and reconcile the cache on conflict.

## Corrected sequencing

**Phase 0 — safe deletions now (no data path):** D + E + F. Zero behavior change, shrinks dependency surface, proves the harness. Also delete the false "PostgreSQL backend will enforce" comment in `rate_limiter.py` and the dead `identity_notifications` reader.

**Phase 1 — build the durable mirror (the real work, Redis stays up):**
1. Make every resolve path (including `persist=False`) write-through to `core.sessions` (or a purpose-built `core.session_bindings`) with the rich fields (`spawn_reason`, `bind_ip_ua`, `trajectory_required`) and UUID-typed `agent_id`.
2. Port the PATH 1 fingerprint hijack check (C3) into PATH 2.
3. Port the onboard pin (C2) to PG or an in-process TTL store.
4. Decide + implement B (transport binding) and C (rate limiter).
5. Run **dual-write with Redis still authoritative** in production until a parity check shows PG mirrors ≥99% of live Redis bindings (the inverse of today's 6%).

**Phase 2 — cut over the durable surfaces (NOT full Redis removal — see topology decision):** flip the *session/identity/transport* reads from PATH 1 (Redis) to PG, delete only those Redis write/read branches, and accept-or-port C4 (dedup). **Do NOT remove `redis_client.py`, drop the `redis` dependency, or `brew services stop redis`** — under the multi-process target Redis stays as the coordination/TTL layer (metadata cache, distributed lock, rate limiter, onboard-pin TTL, dedup). Keep the `asyncio.wait_for` Redis guards. Endpoint is two stores, not one.

**Test impact:** ~229 Redis test functions across 7 files plus ~33 partial mocks — over the CLAUDE.md single-writer-deletion tripwire. Phase 2 deletions must be surfaced as their own draft PR, not folded in.

## Decisions required

1. **Transport binding (B):** drop-to-cold-resolution (recommended; already the timeout behavior) vs. add `core.transport_bindings`.
2. **KG-store rate limiter (C):** accept 20/hr unenforced (recommended; the general 60/min·1000/hr limit is in-memory and unaffected) vs. wire `audit.rate_limits`.
3. **Onboard pin (C2):** PG-backed vs. in-process TTL store. Not optional — it's a blocker for IP:UA-fallback clients.

## Non-goals

- Not touching the in-memory token-bucket rate limiter (`src/rate_limiter.py`, no Redis dependency).
- Not part of, and not blocked by, BEAM Wave 3.

## Provenance

v0 inventory came from file exploration. v0.1 corrections came from direct verification against the running instance on 2026-06-27: an S21-a race analysis, a sweep for hidden consumers / missed surfaces / test blast radius, and a live key census (Redis `--scan` vs. `core.sessions` row count). The 94%-Redis-only finding is what refuted v0's "near-free" claim, and is the reason this is v0.1 and not an implementation PR.
