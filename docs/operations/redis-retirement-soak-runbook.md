# Redis Retirement — Shadow Soak Runbook

**Audience:** operator. **Purpose:** drive the Phase 1 shadow soak that gates the read flip, and decide whether `core.session_bindings` earns its place. Companion to `docs/proposals/archive/redis-retirement-v0.md` (inventory) and `redis-retirement-phase-1-plan.md` (design, v1.1).

**Status correction, 2026-10-12.** The binding mirror is no longer entirely inert.
`resolution._keyed_bind_fingerprint` reads `core.session_bindings` when Redis does
not supply a matching keyed-session binding with a fingerprint, then passes that
fingerprint to `_fingerprint_hijack_check`. This read is not gated by either mirror
flag. A substrate-attested resume also schedules a fingerprint refresh on existing
matching mirror bindings through `_refresh_bind_fingerprint_stores`, without
consulting the mirror flags. Under the global `log` mode observed in the loaded service configuration,
a mismatch produces suspected-hijack telemetry; under `strict` it can refuse a
keyed resume. Existing tests cover the strict case. `core.onboard_pins` still has
no production read caller found in this inspection. The general session-resolution
read flip remains unwired, but "nothing reads the mirror" is no longer true.

The June 28 topology decision in the parent proposal also changed the objective:
retain Redis for ephemeral coordination and move appropriate durable bindings to
PostgreSQL, **not remove Redis entirely**. Treat this historical runbook as migration
context with the correction above. No activation, deactivation, restart or migration
is authorized by this review. See the
[follow-up review](../analysis/shadow-overhead-and-mirror-review-2026-10-12.md).

## Historical PR stack and installation sequence

| PR | What it lands | Status |
|---|---|---|
| #1122 | Prep fix: PG session-collision → `pg_session_collision` event | ✅ merged |
| #1123 | Migration 051 (`core.session_bindings`, `core.onboard_pins`) + DB methods | ✅ merged |
| #1132 | Hijack-check helper extracted from PATH 1 | ✅ merged |
| #1129 | Shadow dual-write (writes the mirror when the flag is on) | ✅ merged |
| #1130 | Birth-cohort parity checker (`scripts/ops/session_mirror_parity_check.py`) | ✅ merged |
| #1135 | TTL/NX parity fix + reaper (Codex review #1, #4) | ✅ merged |
| #1137 | Parity flip-gate (`--gate`) + api_key_hash note (Codex review #2, #3) | ✅ merged |

**Merge order now:** the whole stack is merged. The TTL/NX guard fix is on `SessionMixin`'s NX-claim path (`src/db/mixins/session.py`, the `Codex review #1: TTL/NX parity` note), the reaper is in `core.cleanup_expired_sessions()` inside `db/postgres/migrations/051_session_mirror_tables.sql`, and the flip-gate flags (`--gate`, `--min-cohort`, `--min-ratio`) are in `scripts/ops/session_mirror_parity_check.py`. The installation steps below are historical instructions, not a finding that this deployment still needs migration 051. Verify deployment state before repeating them.

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

What this does: `_cache_session` and `set_onboard_pin` also write to
`core.session_bindings` / `core.onboard_pins` alongside Redis. The binding mirror
already supplies fallback fingerprint evidence as described above; it is not the
general authoritative session resolver. Write failures are swallowed and waits are
bounded, but the writes are awaited: `_cache_session` allows an additional 1.0 s
mirror-write timeout and pin-setting allows 0.5 s. Those are configured timeout
budgets, not measured latency or strict wall-clock ceilings (cancellation can take
longer). Best-effort does not mean no latency or no downstream consumer.

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

The old proposed metric was *how often would dropping the ephemeral binding mirror
cause a cold-mint that neither `core.sessions` (PATH 2) nor a live onboard pin would
have covered?* That counter is not built, and the current keyed-id resolver also
makes the old question incomplete: a valid keyed id can authenticate without a
stored binding, while the mirror can still supply its binding-time fingerprint.

Before proposing additional instrumentation, separate two questions: which legacy
or transient binding populations need durable continuity, and which callers need
the existing fallback fingerprint evidence? Evaluate a replacement design against
both. A low count cannot authorize deleting an identity capability, and parity
cannot settle either question. Retain the current behavior while an explicit
identity design review resolves the migration; do not extend soak time as if it
were progress toward a missing measurement.

## Step 5 — the read flip (future PR, gated on the above)

Any general read flip needs a separate identity design and implementation review,
including parity, continuity coverage, fingerprint evidence and expiry behavior.
`UNITARES_SESSION_MIRROR_APPLY` is not wired to a general resolver read path in the
inspected source. Preserve the existing keyed-session fingerprint fallback when
evaluating alternatives. Redis remains part of the intended multi-process system;
this is a durable-binding migration, not eventual removal of Redis.

## Rollback

Setting `UNITARES_SESSION_MIRROR_SHADOW=0` stops the flag-controlled binding and
pin dual-writes after the configuration is applied; it does **not** turn off the
fingerprint reader or attested fingerprint refresh, nor remove existing rows. As rows expire, the reader may lose fallback
evidence. This is therefore not a proven behavior-neutral rollback. Plan any flag
change as an identity change with its existing review requirements; do not execute
the historical disable procedure merely to reduce shadow count.

## Invariant

Redis and the existing persistent identity/session paths retain their current
roles. The binding mirror already contributes fallback fingerprint evidence;
the generic APPLY migration is separate. This review changes documentation only,
not identity resolution, either mirror flag, or data retention.
