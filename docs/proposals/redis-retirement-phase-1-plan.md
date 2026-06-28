# Redis Retirement — Phase 1 Implementation Plan (durable PG mirror)

**Status:** Draft / implementation plan — **v1.1, revised after council review (2026-06-27).**
**Parent:** `redis-retirement-v0.md` (v0.1). This details Phase 1 only.
**Goal:** make PostgreSQL durably carry the session/identity state that today lives only in Redis, so Redis can later be retired — while *measuring*, not assuming, how much of that state actually needs mirroring.

> **v1 → v1.1 council changes.** A three-member council (architect + code-reviewer + live-verifier) reviewed v1 against the running system. Outcome: the FK reasoning and new-table-vs-alternatives analysis hold, but v1 was **over-scoped, under-specified, and its schema was provably incomplete.** Key revisions: (1) the durable artifact that actually matters is the **onboard pin (C2)** — restructure Phase 1 so pins + persist-time rich fields + the hijack helper (all unconditionally needed) ship first, and the full `core.session_bindings` ephemeral mirror is **gated on a shadow-phase measurement** (architect: it may be solving a problem the pin already solves). (2) Add the two missing columns the live-verifier found (`public_agent_id`, `api_key_hash`). (3) Fix the two-table drift hazard with an explicit authority contract. (4) Replace the point-in-time parity gate. (5) Fix two extraction blockers (B1 `resume` mutation, B2 PATH 2 data shape).

## The central decision: FK forces a separate store, not `core.sessions`

`core.sessions` cannot hold the ephemeral 94% (live-verifier confirmed `\d core.sessions`):

```sql
session_id   TEXT PRIMARY KEY,
identity_id  BIGINT NOT NULL REFERENCES core.identities(identity_id) ON DELETE CASCADE
```

`identity_id` is NOT NULL FK → `core.identities` → `core.agents`. The Redis `session:` payload stores an `agent_id` *string* (the UUID) with no identity/agent row — that's the point of `persist=False` (`resolution.py:1274-1299`).

**Alternatives considered and rejected (architect steelmanned each):**
- **α eager identity persistence** — would mint `core.agents`/`core.identities` rows for ~900 transient probes, polluting the agent population and every agent-derived metric, inverting "reads don't need a bound caller." Cleanest schema, worst ontology. Reject.
- **β nullable `identity_id` on `core.sessions`** — genuinely the cleanest *end-state* (one table; ephemeral→worked is one `UPDATE`). Loses only on *migration safety*: dropping NOT NULL on a load-bearing FK with live joins/CASCADE is a hot-table mutation, vs. the new table being additive and flag-isolated (zero blast radius until APPLY). **Decision: new table for migration safety, but β goes on a Phase-3 docket** so the two-table split isn't permanent by inertia.
- **γ repurpose `core.agent_sessions`** — wrong cardinality (PK `agent_id`, the inverse mapping) and carries the very FK we're escaping. Reject. **Drop `core.agent_sessions` in Phase 2** (it's empty, 0 rows confirmed) rather than leave a misleading empty table.

## Restructured Phase 1: 1A unconditional, 1B measured

The council's central insight: the only population that *needs* cross-restart continuity is IP:UA-fallback clients (Claude Desktop, REST without `client_session_id`), and that is exactly what the **onboard pin** serves. Client-key-bearing clients re-anchor themselves by re-presenting their key on restart. So the full ephemeral `session_bindings` mirror may be redundant with the pin. We **build the things needed regardless first, then let the shadow phase decide** whether the ephemeral mirror earns its place.

### Phase 1A — needed regardless of the 1B decision

1. **Prep fix:** `persistence.py:583` must check `create_session`'s return value and reconcile the cache on conflict (small, independently shippable).
2. **Onboard pins → durable** (`core.onboard_pins`). This is the load-bearing continuity anchor. Dual-write `set_onboard_pin` (`session.py:977-1030`) to PG; add a PG fallback to the lookup at `_derive_session_key_impl` step 7 (`session.py:746-783`); preserve `if_absent`/NX subagent semantics via `ON CONFLICT DO NOTHING`; keep an in-process L1 to avoid a PG hit per request.
3. **Rich fields onto `core.sessions` at persist time.** When `ensure_agent_persisted` writes `core.sessions`, also persist `spawn_reason`, `bind_ip_ua`, `public_agent_id`, `trajectory_required` into `client_info`/`metadata` JSONB (no schema change — both are JSONB). This makes the *persisted* population complete on its own.
4. **Hijack-check helper (C3)** extracted from `resolution.py:651-753` — see blocker fixes below — and wired into **both** PATH 1 and PATH 2, reading `bind_ip_ua` from whichever store. This must land before PATH 1 is ever removed so detection survives.
5. **Shadow harness scaffolding** (flags + audit events), reusing the grounding/Wave-3 pattern.

### Phase 1B — `core.session_bindings`, gated on measurement

Build `core.session_bindings` as an **instrumented shadow only**. During the soak, the parity script's primary output is not "does PG mirror Redis" but: **how often would dropping the ephemeral mirror entirely cause a cold-mint that neither PATH 2 (`core.sessions`) nor a live onboard pin would have covered?**

- If that number is **~zero** → drop `core.session_bindings` entirely. Ship the strictly simpler design: pins + persist-time rich fields. The drift hazard and the parity-gate problem vanish with it.
- If **material** (e.g. long-lived keyless Claude Desktop sessions outliving the 30-min pin TTL, hit by a routine plist restart) → keep it, with the consistency contract below.

This defers the heaviest, most-uncertain piece behind data instead of assuming it.

## Schema (corrected per live-verifier)

```sql
-- core.onboard_pins — Phase 1A. Durable mirror of recent_onboard:* keys.
CREATE TABLE IF NOT EXISTS core.onboard_pins (
    fingerprint        TEXT PRIMARY KEY,   -- full suffix after "recent_onboard:", e.g. "ua:<hash>|<transport>|<model>" (1-3 pipe segments)
    agent_uuid         TEXT NOT NULL CHECK (agent_uuid ~ '^[0-9a-fA-F-]{36}$'),
    client_session_id  TEXT NOT NULL,
    expires_at         TIMESTAMPTZ NOT NULL    -- 30-min TTL (PIN_TTL=1800)
);
CREATE INDEX IF NOT EXISTS idx_onboard_pins_expires ON core.onboard_pins(expires_at);

-- core.session_bindings — Phase 1B (instrumented shadow; keep only if measured material).
-- FK-less mirror of the Redis session: payload. Column set verified against 865 live payloads.
CREATE TABLE IF NOT EXISTS core.session_bindings (
    session_key         TEXT PRIMARY KEY,
    agent_uuid          TEXT NOT NULL CHECK (agent_uuid ~ '^[0-9a-fA-F-]{36}$'),  -- maps from Redis JSON "agent_id"; CHECK enforces "proof not label"
    public_agent_id     TEXT NULL,   -- ADDED (live in 59.8% of payloads)
    display_agent_id    TEXT NULL,
    api_key_hash        TEXT NULL,   -- ADDED (live in 40.2%, legacy sessions) — without it the parity check flags 40% as divergent
    spawn_reason        TEXT NULL,
    bind_ip_ua          TEXT NULL,   -- consumed by the C3 hijack check on PATH 2
    trajectory_required BOOLEAN NOT NULL DEFAULT FALSE,
    bind_count          INTEGER NOT NULL DEFAULT 1,
    bound_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at          TIMESTAMPTZ NULL   -- NULL = permanent (Redis TTL=-1); see TTL policy
);
CREATE INDEX IF NOT EXISTS idx_session_bindings_expires ON core.session_bindings(expires_at);
CREATE INDEX IF NOT EXISTS idx_session_bindings_uuid    ON core.session_bindings(agent_uuid);
```

Notes: `label` dropped (zero live backing). `agent_id`→`agent_uuid` rename is explicit and the extraction code must map it. The `CHECK` on `agent_uuid` makes "this column holds the UUID-proof, never a display label" a schema invariant (architect: otherwise the no-lookup-by-label identity invariant is enforced only by discipline). **Naming disambiguation:** migration 049 already added `coordination.session_resolution_sagas` — unrelated; `core.session_bindings` is the resolution mirror, not a saga.

**TTL policy (open Q2, data-backed):** all currently-expiring keys are ≤24h, so a 24h cap truncates nothing live — it only bounds the ~363 permanent (TTL=-1) keys, which read as accumulation cruft. Recommendation: store expiring keys with their `expires_at`, store permanent keys as 24h-from-now (not NULL-forever). Reaper extends `core.cleanup_expired_sessions()` (schema.sql:441-455) to both new tables.

## Blocker fixes (code-reviewer)

**B1 — `resume` mutation can't be extracted.** The PATH 1 block mutates the outer local `resume = False` (resolution.py:753); a plain helper can't do that. The helper must **return** `should_block: bool`; both call sites apply it:
```python
should_block = await _fingerprint_hijack_check(session_key, bound_bind_ip_ua, agent_uuid)
if should_block:
    resume = False   # PATH 1; PATH 2 falls through to mint analogously
```
Helper closes over (thread as params): `session_key`, `agent_uuid`, the bind-time `bind_ip_ua`, and re-derives the fp-mode config internally. It reads `get_session_signals().ip_ua_fingerprint` and emits `identity_hijack_suspected` via `_broadcaster()` — pure logic, no Redis.

**B2 — PATH 2 data shape.** `db.get_session` returns a `SessionRecord` with no `.get()` and no `bind_ip_ua`. So `get_session_binding` must (a) return a **plain dict**, and (b) the dict must carry `bind_ip_ua` (hence the column). Otherwise the helper raises `AttributeError`, or — if it silently sees `None` — the check is dead code on PATH 2.

**M1 — atomic guard must surface the block.** `INSERT ... ON CONFLICT (session_key) DO UPDATE SET ... WHERE session_bindings.agent_uuid = EXCLUDED.agent_uuid` is atomic, but the blocked case returns `INSERT 0 0` with **no error**. Use `RETURNING agent_uuid` to distinguish inserted/updated/blocked, and emit `S21A_OVERWRITE_BLOCKED` on the blocked case (preserving the log/audit sentinel the Redis guard had). This is still strictly stronger than the old non-atomic Redis `get`+`setex`.

**M2 — uncovered write site.** The sliding-TTL refresh `raw_redis.expire("session:"+key, SESSION_TTL)` at **resolution.py:788** is outside `_cache_session`. The dual-write must also call `update_session_activity` (already in `SessionMixin`) at that PATH 1 hit site, or PG bindings stale while Redis stays fresh.

**M3 — read must filter expiry.** Existing `get_session` does NOT filter `expires_at` (pre-existing bug — don't repeat). `get_session_binding` must filter `expires_at IS NULL OR expires_at > now()`.

**Write sites that must dual-write (enumerate before coding):** `_cache_session` rich path (`persistence.py:240-301`), `_cache_session` bare path, the resolution.py:788 TTL refresh, and `set_onboard_pin`. Wiring only `_cache_session` misses the TTL refresh and the raw-redis rich path.

## Two-table consistency contract (architect blocker — only if 1B keeps the table)

If `core.session_bindings` survives 1B, the same `session_key` will exist in both it and `core.sessions` after a session works → two TTLs, two reapers that can disagree → a *manufactured* ghost-fork (PATH 2 reads `session_bindings`, misses a reaped row, cold-mints). Mandatory:
1. **`core.session_bindings` is the resolution authority** for any key present in both.
2. `ensure_agent_persisted` updates **both** rows in one transaction.
3. The two `expires_at` clocks are kept in lockstep (derive the binding TTL from the session TTL for persisted keys), so the reapers can't disagree.

## Parity gate (architect — replaces the point-in-time ratio)

A point-in-time full-scan ratio conflates writer-correctness with expiry-race noise under churn (920 keys turning over, independent Redis/PG expiry clocks) — it may never read 99% even with a perfect writer. Instead:
- **Write-path parity:** instrument the dual-writer — for each Redis write, did the paired PG write succeed in the same operation? Target ~100%.
- **Birth-cohort parity:** sample keys bound 5–60 min ago (committed, not yet expired); these should match exactly.
- **The 1B decision metric:** cold-mints-not-covered-by-pin (above).
- Snapshot ratio = smoke test only. Reconcile the expiry clocks *before* measuring.
- Watch `audit.events` volume (920 keys × scan freq) — don't flood the partition.

## Build order

1A: prep fix → `core.onboard_pins` + dual-write/lookup → rich fields on `core.sessions` → hijack helper (B1/B2) wired into PATH 1+2 → shadow scaffolding.
1B: `core.session_bindings` instrumented shadow → soak → **decide keep/drop on the cold-mint-not-covered metric** → if keep, apply the consistency contract + APPLY flip.

Phase 2 (separate PR): delete PATH 1, Redis branches, `redis_client.py`, the dependency, drop `core.agent_sessions`, ~229 Redis tests (single-writer surface — its own draft PR).

## Effort

1A is **M** (pins + helper extraction + persist-path rich fields, all with tests; identity = single-writer surface). 1B is **S code + a soak window**, and may *reduce* to near-zero if the measurement says drop the table. Net Phase 1: **M**, less than v1 estimated, because the heaviest piece is now conditional.

## Open questions for the operator

1. **Pin store:** PG-backed `core.onboard_pins` (survives restart, recommended — pins are the real continuity anchor) vs. in-process TTL dict.
2. **Permanent-key TTL:** impose 24h on the ~363 TTL=-1 keys (recommended — data shows no live key exceeds 24h) vs. mirror as permanent.
3. **1B disposition:** accept the measure-then-decide gate on `core.session_bindings`, or commit up-front to either keeping it (belt-and-suspenders) or dropping it (pins-only). Recommendation: **measure** — the shadow phase is the cheapest possible place to settle it.

## Provenance

v1 from the Phase 1 facts dossier. v1.1 from a council on 2026-06-27: architect (FK alternatives, drift hazard, the pins-vs-mirror challenge, parity-metric critique), code-reviewer (B1/B2/M1/M2/M3, write-site enumeration), live-verifier (the two missing columns + key-format + TTL ground-truth against 865 live payloads). Live-verifier refutations are why the schema gained `public_agent_id`/`api_key_hash` and lost `label`.
