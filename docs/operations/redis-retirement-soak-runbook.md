# Redis Retirement — Shadow Soak Runbook

**Audience:** operator. **Purpose:** drive the Phase 1 shadow soak that gates the read flip, and decide whether `core.session_bindings` earns its place. Companion to `docs/proposals/redis-retirement-v0.md` (inventory) and `redis-retirement-phase-1-plan.md` (design, v1.1).

This is operational, not aspirational: every step below maps to code already merged or in draft. Nothing here changes live behavior until you set `UNITARES_SESSION_MIRROR_SHADOW=1`, and that step is itself behavior-neutral (best-effort writes to inert tables).

## The PR stack and merge order

| PR | What it lands | Status |
|---|---|---|
| #1122 | Prep fix: PG session-collision → `pg_session_collision` event | ✅ merged |
| #1123 | Migration 051 (`core.session_bindings`, `core.onboard_pins`) + DB methods | ✅ merged |
| #1132 | Hijack-check helper extracted from PATH 1 | ✅ merged |
| #1129 | Shadow dual-write (writes the mirror when the flag is on) | ✅ merged |
| #1130 | Birth-cohort parity checker (`scripts/ops/session_mirror_parity_check.py`) | ✅ merged |
| **#1135** | **TTL/NX parity fix + reaper** (Codex review #1, #4) | **OPEN — merge BEFORE the soak** |
| #1137 | Parity flip-gate (`--gate`) + api_key_hash note (Codex review #2, #3) | OPEN — needed before the flip decision |

**Merge order now:** the original stack is merged. **#1135 MUST merge before you enable the shadow flag** — it fixes the TTL/NX guard bug (an expired row wrongly blocking a fresh claim), and that bug would skew the soak's parity numbers (spurious `missing_in_pg`). #1137 is needed before the flip decision, not before the soak.

## Step 1 — apply migration 051 (manual)

Migrations are MANUAL in this repo (diff vs `core.schema_migrations` on every deploy). **Apply 051 only after #1135 is merged** — #1135 edits 051 to add the reaper (extends `core.cleanup_expired_sessions()`). If you already applied the pre-#1135 051, just re-run the file: it's idempotent (`CREATE TABLE IF NOT EXISTS`, `CREATE OR REPLACE FUNCTION`, `INSERT … ON CONFLICT DO NOTHING`).

```bash
psql "$GOVERNANCE_DATABASE_URL" -f db/postgres/migrations/051_session_mirror_tables.sql
# verify table + reaper:
psql "$GOVERNANCE_DATABASE_URL" -Atqc "SELECT version||'|'||name FROM core.schema_migrations WHERE version=51"
#   expect: 51|session_mirror_tables
psql "$GOVERNANCE_DATABASE_URL" -Atqc "SELECT pg_get_functiondef('core.cleanup_expired_sessions()'::regprocedure) LIKE '%session_bindings%'"
#   expect: t  (reaper present)
```

`unitares_doctor.py` reports `missing 51:session_mirror_tables` until this runs — that's the expected pending state, not drift.

## Step 2 — enable the shadow dual-write

Set the flag in the governance-mcp LaunchAgent env and restart (plist env changes need bootout+bootstrap, not just reload):

```bash
# add to com.unitares.governance-mcp.plist EnvironmentVariables:
#   UNITARES_SESSION_MIRROR_SHADOW = 1
launchctl bootout gui/$(id -u)/com.unitares.governance-mcp
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.unitares.governance-mcp.plist
```

**Precondition: #1135 merged and migration 051 (with the reaper) applied** — otherwise the expired-row guard bug skews the soak parity you're about to measure.

What this does: `_cache_session` and `set_onboard_pin` now ALSO write to `core.session_bindings` / `core.onboard_pins` alongside Redis. **Redis stays authoritative for all reads.** The PG writes are best-effort (failures swallowed, latency-bounded) — if the mirror write fails or is slow, the live identity path is unaffected. Nothing reads the mirror yet.

Confirm it's populating (after a few minutes of traffic):

```bash
psql "$GOVERNANCE_DATABASE_URL" -Atqc "SELECT count(*) FROM core.session_bindings"   # should climb from 0
psql "$GOVERNANCE_DATABASE_URL" -Atqc "SELECT count(*) FROM core.onboard_pins"
```

## Step 3 — soak

Let it run long enough to capture a full session-TTL cycle of real traffic — **at least 24h** (the session TTL), ideally a few days to cover weekday/weekend traffic shapes and at least one service restart. The mirror only reflects bindings created *while the flag was on*, so parity climbs over the first 24h as the pre-existing Redis bindings age out and new ones get dual-written.

## Step 4 — measure parity (gates the read flip)

```bash
# informational (cron-friendly, always exit 0):
python3 scripts/ops/session_mirror_parity_check.py | tee /tmp/parity.json

# enforceable flip-gate (#1137 — exits non-zero unless decision-grade):
python3 scripts/ops/session_mirror_parity_check.py --gate --min-cohort 100 --min-ratio 0.99
#   exit 0 only if status=ran, cohort>=100, parity>=0.99, zero uuid_mismatch.
```

Reads a JSON summary. What the fields mean:

- `parity_ratio` — of the **birth cohort** (bindings 5–60 min old: committed, not yet expired), the fraction whose Redis `agent_id` matches the PG mirror. **This is the gate.** A faithful dual-writer should sit near **1.0**. Sustained < ~0.99 means the writer is dropping or corrupting writes — investigate before flipping.
- `missing_in_pg` — birth-cohort Redis bindings absent from PG (dropped writes).
- `uuid_mismatch` — present in both but bound to different UUIDs (corruption — should be ~0).
- `sample_missing` / `sample_mismatch` — up to 10 examples to investigate.
- `status: "inert"` — the mirror is empty; you haven't enabled the shadow flag (Step 2) or no traffic yet.

Why birth-cohort and not a full snapshot: comparing all keys conflates real dropped writes with expiry-race noise (independent Redis/PG TTL clocks) and never converges to 1.0. The cohort window excludes the mid-flight and freshly-reaped tails. Run it a few times across the soak; look for a stable high ratio, not one reading.

## ⚠️ What is NOT yet measured: the keep/drop question

The parity checker answers **"is the mirror faithful enough to read from?"** (the APPLY-flip gate). It does **not** answer the separate **Phase 1B question: is `core.session_bindings` worth keeping at all, vs. a simpler pins-only design?**

That question needs a different metric the council defined and which is **not yet built**: *how often would dropping the ephemeral binding mirror cause a cold-mint that neither `core.sessions` (PATH 2) nor a live onboard pin would have covered?* If that number is ~zero, the table should be dropped and the read flip is smaller (pins + persisted `core.sessions` only). Building that instrumentation is the next code task — flagged here so the soak isn't mistaken for answering it.

## Step 5 — the read flip (future PR, gated on the above)

Once parity is proven AND the keep/drop question is answered, a separate PR wires `UNITARES_SESSION_MIRROR_APPLY`: PATH 2 resolves from `core.session_bindings` (or, in the pins-only outcome, from `core.sessions` + `core.onboard_pins`), and calls the already-extracted `_fingerprint_hijack_check` (from merged #1132) so hijack detection survives the eventual Redis removal. That flip is LIVE-AFFECTING — it is not in any current PR by design.

## Rollback

At any point, to stop shadow writing: unset `UNITARES_SESSION_MIRROR_SHADOW` (or set to `0`) and bootout+bootstrap. No data migration to undo — the mirror tables are inert and self-expire. Redis was authoritative throughout, so there is nothing to recover.

## Invariant

Redis remains the system of record for sessions until the APPLY flip lands AND a subsequent Phase 2 removes the Redis read path. Until then this whole effort is observable, reversible, and off the live read path.
