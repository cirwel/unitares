# Redis Retirement — Phase 1 Implementation Plan (durable PG mirror)

**Status:** Draft / implementation plan
**Parent:** `redis-retirement-v0.md` (v0.1). This details Phase 1 only.
**Goal:** make PostgreSQL a complete durable mirror of the session/identity state that today lives only in Redis, running dual-write with Redis authoritative until parity ≥99%. No Redis is removed in Phase 1 (that's Phase 2).

## The central decision: FK forces a new table, not `core.sessions`

`core.sessions` cannot hold the ephemeral 94%:

```sql
-- db/postgres/schema.sql:123-142
CREATE TABLE core.sessions (
    session_id   TEXT PRIMARY KEY,
    identity_id  BIGINT NOT NULL REFERENCES core.identities(identity_id) ON DELETE CASCADE,
    ...
);
```

`identity_id` is **NOT NULL FK → core.identities → core.agents**. The Redis `session:` payload stores an `agent_uuid` *string* with no identity/agent row (that's the whole point of `persist=False`). Writing ephemeral sessions into `core.sessions` would require eagerly minting `core.agents` + `core.identities` rows for every transient resolve — polluting the agent population with probes that never do real work, and contradicting both the lazy-creation design (`resolution.py:1274-1299`) and the strict-identity contract ("reads don't need a bound caller; writes do").

**Decision: a new FK-less mirror table `core.session_bindings`**, modeled on the existing shadow-table FK reasoning (migrations 043/044: *"we deliberately leave FKs OFF — it is a write-only replica, not a referential target"*). It mirrors the Redis `session:` payload 1:1, keyed by `session_key`, holding `agent_uuid` as a plain string. This gives durable cross-restart session→uuid continuity **without** forcing identity persistence.

`core.sessions` keeps its current role: the record that an identity actually onboarded/worked. `core.session_bindings` is the resolution-layer mirror. When an ephemeral session later does real work, the existing `ensure_agent_persisted` path (`persistence.py:583`) still creates the agent/identity/`core.sessions` rows — unchanged.

> Note: `core.agent_sessions` (schema.sql:111-118) looks like an abandoned precursor to this — but its PK is `agent_id` (one row per agent, answers "what session is this agent on"), the inverse of the `session_key → agent_uuid` lookup the resolver needs, and it is empty (0 rows live). Don't repurpose it; the keying is wrong. Leave it or drop it in Phase 2 cleanup.

## Schema (one new migration)

```sql
-- core.session_bindings — FK-less durable mirror of the Redis session: payload
CREATE TABLE IF NOT EXISTS core.session_bindings (
    session_key         TEXT PRIMARY KEY,
    agent_uuid          TEXT NOT NULL,                 -- plain string, NO FK (mirrors Redis)
    display_agent_id    TEXT NULL,
    label               TEXT NULL,
    spawn_reason        TEXT NULL,
    bind_ip_ua          TEXT NULL,                     -- for the PATH 1 hijack check
    trajectory_required BOOLEAN NOT NULL DEFAULT FALSE,
    bind_count          INTEGER NOT NULL DEFAULT 1,
    bound_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at          TIMESTAMPTZ NOT NULL           -- TTL parity with SESSION_TTL_SECONDS / -1≈far-future
);
CREATE INDEX IF NOT EXISTS idx_session_bindings_expires ON core.session_bindings(expires_at);
CREATE INDEX IF NOT EXISTS idx_session_bindings_uuid    ON core.session_bindings(agent_uuid);

-- core.onboard_pins — durable mirror of recent_onboard:* (IP:UA-fallback routing)
CREATE TABLE IF NOT EXISTS core.onboard_pins (
    fingerprint         TEXT PRIMARY KEY,              -- e.g. "ua:<md5prefix>" or scoped variant
    agent_uuid          TEXT NOT NULL,
    client_session_id   TEXT NOT NULL,
    expires_at          TIMESTAMPTZ NOT NULL           -- 30-min TTL (PIN_TTL=1800)
);
CREATE INDEX IF NOT EXISTS idx_onboard_pins_expires ON core.onboard_pins(expires_at);
```

Extend `core.cleanup_expired_sessions()` (schema.sql:441-455) — or add a sibling reaper — to `DELETE FROM core.session_bindings WHERE expires_at < now()` and the same for `core.onboard_pins`. The session-cleanup background task already runs (`background_tasks.py`).

Migration is **MANUAL** per repo convention (diff vs `core.schema_migrations` on deploy) — next free slot, CI-gated by `unitares_doctor.py`.

## DB mixin methods (new, in `src/db/mixins/session.py`)

```python
async def upsert_session_binding(session_key, agent_uuid, *, display_agent_id, label,
                                 spawn_reason, bind_ip_ua, trajectory_required,
                                 expires_at, mint_guard=False) -> bool
# INSERT ... ON CONFLICT (session_key) DO UPDATE ... ; when mint_guard, the
# UPDATE branch is WHERE session_bindings.agent_uuid = EXCLUDED.agent_uuid
# (refuse to clobber a different live uuid — the S21-a guard, now ATOMIC in one
# statement, strictly stronger than the old non-atomic Redis get+setex).

async def get_session_binding(session_key) -> Optional[SessionBindingRecord]
async def lookup_onboard_pin_pg(fingerprint, *, refresh_ttl) -> Optional[str]
async def set_onboard_pin_pg(fingerprint, agent_uuid, client_session_id, *, if_absent) -> bool
# if_absent → INSERT ... ON CONFLICT DO NOTHING (the NX-claim semantics for subagents)
```

This **fixes the S21-a race properly**: the council found the Redis guard was never atomic (`get` then `setex`). A single `INSERT ... ON CONFLICT (session_key) DO UPDATE ... WHERE agent_uuid = EXCLUDED.agent_uuid` is atomic at the row level — a net improvement, not a regression.

## The two blocker ports

### C3 — fingerprint hijack check into PATH 2

Extract the hijack logic from PATH 1 (`resolution.py:651-753`) into a shared helper:

```python
async def _fingerprint_hijack_check(session_key, agent_uuid, bound_bind_ip_ua) -> bool:
    # returns True if strict-mode says fall through to fresh session.
    # reads current_fp from get_session_signals().ip_ua_fingerprint,
    # honors session_fingerprint_check_mode() + prefix_bind_fingerprint_mode(),
    # emits identity_hijack_suspected on violation. Pure logic — no Redis.
```

Call it from **both** PATH 1 (passing `cached.get("bind_ip_ua")`) and PATH 2 (passing `binding.bind_ip_ua` from `core.session_bindings`). The `bind_ip_ua` is already captured at bind time (`persistence.py:153-172`) and will now be written to `core.session_bindings`. This closes the gap where retiring PATH 1 would silently disable hijack detection — the check survives in PATH 2 *before* PATH 1 is touched.

### C2 — onboard pin into Postgres

Dual-write `set_onboard_pin` (`session.py:977-1030`) to both Redis and `core.onboard_pins` (under the shadow flag). On the read side (`_derive_session_key_impl` step 7, `session.py:746-783`), add a PG fallback after the Redis lookup. Preserve the `if_absent`/NX subagent semantics via `ON CONFLICT DO NOTHING`. Keep an in-process L1 dict to avoid a PG hit on every IP:UA-fallback request.

## Dual-write harness (reuse the proven Wave 3 / grounding pattern)

Two independent flags, both default off (mirrors `grounding_shadow`/`grounding_apply`, `governance_config.py:1060-1084`):

- `UNITARES_SESSION_MIRROR_SHADOW=1` — on every `_cache_session` Redis write and every `set_onboard_pin`, **also** write to `core.session_bindings` / `core.onboard_pins`. Redis stays authoritative for reads. Behavior-neutral.
- `UNITARES_SESSION_MIRROR_APPLY=1` — resolver PATH 2 reads `core.session_bindings` (and pin lookup reads `core.onboard_pins`) as the source of truth. Only flip after parity is proven.

**Parity check script** `scripts/ops/session_mirror_divergence_check.py`, modeled on `scripts/ops/wave3_shadow_divergence_check.py` (1-148): for each live Redis `session:` key, compare against `core.session_bindings` (present? agent_uuid match? rich fields match?), emit `coordination_failure.*.shadow_divergence` to `audit.events`, and report the **mirror ratio** (target: ≥99%, inverse of today's ~6%). Gate the APPLY flip on that ratio.

## Build order (within Phase 1)

1. **Prep:** fix `persistence.py:583` to check `create_session`'s return value and reconcile cache on conflict (small, independently shippable).
2. **Migration:** `core.session_bindings` + `core.onboard_pins` + cleanup extension.
3. **DB methods:** the 4 mixin methods above + `SessionBindingRecord`.
4. **Hijack helper:** extract C3 into a shared function, wire into PATH 1 (no behavior change yet — proves parity of the extraction).
5. **Shadow dual-write:** wire `UNITARES_SESSION_MIRROR_SHADOW` into `_cache_session` and `set_onboard_pin`.
6. **Parity script** + run in production (Redis authoritative) until ratio ≥99%.
7. **Apply flip:** `UNITARES_SESSION_MIRROR_APPLY` — PATH 2 reads `core.session_bindings`, runs the hijack helper; pin lookup reads PG. Run with Redis still up as L1.

Phase 2 (separate plan/PR) deletes PATH 1, the Redis branches, `redis_client.py`, the dependency, and the ~229 Redis tests (single-writer-surface — its own draft PR).

## Effort

| Step | Effort |
|---|---|
| 1 prep (return-value fix) | XS |
| 2 migration | S |
| 3 DB methods | S |
| 4 hijack helper extraction | M (identity hot path — careful + tests) |
| 5 shadow dual-write | M |
| 6 parity script + soak | S code, then **wall-clock soak** (days, operator-gated) |
| 7 apply flip | M + verification |

Phase 1 is **M–L total**, dominated by step 4 (identity resolution is a single-writer surface per CLAUDE.md — coordinate, ship with tests) and the step-6 soak window. It is entirely behind two default-off flags until the apply flip, so it ships incrementally without risk to the live path.

## Open questions for the operator

1. **Pin durability:** PG-backed `core.onboard_pins` (survives restart, recommended) vs. in-process TTL dict (simpler, loses pins on restart — same as a Redis pin expiry). Plan above assumes PG-backed.
2. **Ephemeral binding TTL:** Redis has 347 permanent (TTL=-1) session keys. Mirror them as far-future `expires_at`, or impose the 24h `SESSION_TTL` uniformly on the PG mirror? (Recommend: impose 24h — permanent ephemeral bindings are almost certainly unintended accumulation.)
3. **Verify before build:** this plan should get a council pass (architect + code-reviewer + live-verifier) the same way v0 did — the FK/new-table decision and the hijack-helper extraction are the two highest-risk calls.
