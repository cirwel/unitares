# Migration Drift Triage — 2026-04-29

**KG discovery:** `2026-04-26T19:57:58.834010+00:00`  
**Status:** open  
**Severity:** high  
**Triage agent:** triage-agent / branch `triage/migration-drift-renumbering`

> **No DDL has been run. No registry entries have been modified. All changes in this PR are source-file renames and version-number fixes. Every psql command below is Kenny's call.**

---

## 1. Problem statement

Surfaced 2026-04-26 during PR #200 (fleet-wide `hydrate_from_db_if_fresh`).
Production DB was missing `column s.synthetic` because
`018_bootstrap_synthetic_state.sql` never applied — slot 18 in
`core.schema_migrations` was already occupied by a **phantom** entry:

```
(18, 'progress flat telemetry tables')
```

This came from `018_progress_flat_telemetry.sql` (commit `f7f71723`), applied
out-of-band via `psql` but never merged to master.

A hot-fix was applied inline on 2026-04-26:

```sql
ALTER TABLE core.agent_state ADD COLUMN IF NOT EXISTS synthetic BOOLEAN NOT NULL DEFAULT false;
-- + partial index idx_agent_state_synthetic_partial
```

This unblocked hydration but left the registry and source tree out of sync.

---

## 2. Registry state at time of investigation (2026-04-26)

| Slot | Registry name | Source file in master |
|------|---------------|-----------------------|
| 1–13 | applied | applied |
| 14 | **missing** | `014_seed_epoch_2.sql` |
| 15 | **missing** | `015_agent_process_bindings.sql` |
| 16 | **missing** | `016_same_host_ppid_consistent.sql` |
| 17 | `substrate_claims` | `017_substrate_claims.sql` ✓ |
| 18 | `progress flat telemetry tables` (phantom) | `020_progress_flat_telemetry.sql`* |
| 19 | **missing** | `019_matview_measured_only.sql` → renamed **023** |
| 20 | **missing** | *(was file 020; INSERT said slot 18 — fixed)* |
| 21 | **missing** | `021_seed_epoch_3.sql` |

\* `020_progress_flat_telemetry.sql` existed in master with a wrong INSERT claiming slot 18 — fixed in this PR to (20, ...).

---

## 3. File-by-file analysis

### 014_seed_epoch_2.sql
- **DDL:** `INSERT INTO core.epochs (2, ...)` + registry INSERT
- **Idempotent:** Yes — both INSERTs use `ON CONFLICT DO NOTHING`
- **Dependencies:** `core.epochs` (migration 007)
- **Risk:** Low. A gap-filling data insert; no schema change.

### 015_agent_process_bindings.sql
- **DDL:** `CREATE TABLE IF NOT EXISTS core.agent_process_bindings` + 3 indexes + 2 `ALTER TABLE core.agents ADD COLUMN IF NOT EXISTS`
- **Idempotent:** Yes — all `IF NOT EXISTS`
- **Dependencies:** `core.agents`
- **Risk:** Low. If the table and columns already exist the file is a no-op.

### 016_same_host_ppid_consistent.sql
- **DDL:** `ALTER TABLE core.agent_process_bindings ADD COLUMN IF NOT EXISTS same_host_ppid_consistent`
- **Idempotent:** Yes
- **Dependencies:** 015 must run first (table created there)
- **Risk:** Low. Single nullable column add.

### 020_progress_flat_telemetry.sql (INSERT fix applied in this PR)
- **DDL:** `CREATE TABLE IF NOT EXISTS progress_flat_snapshots` + `resident_progress_pulse` + 5 indexes
- **Idempotent:** Yes — all `IF NOT EXISTS`
- **Prod state:** Tables/indexes already exist (created by the out-of-band phantom run). Applying this file re-runs the idempotent CREATEs (no-ops) and inserts registry slot 20 alongside the existing phantom slot 18.
- **Risk:** Low. The file was fixed to INSERT (20, ...) instead of the original wrong (18, ...) that would have silently collided.

### 021_seed_epoch_3.sql
- **DDL:** `INSERT INTO core.epochs (3, ...)` + registry INSERT (21, ...)
- **Idempotent:** Yes
- **Prod state:** The epoch row may already exist (bump_epoch.py was run on prod); the migration only ensures the test DB and fresh instances get it. Registry slot 21 may be absent.
- **Risk:** Low.

### 022_bootstrap_synthetic_state.sql (renumbered from 018, this PR)
- **DDL:**
  1. `ALTER TABLE core.agent_state ADD COLUMN IF NOT EXISTS synthetic BOOLEAN NOT NULL DEFAULT false` — **no-op on prod** (hot-fix applied this already)
  2. `CREATE INDEX IF NOT EXISTS idx_agent_state_synthetic_partial` — **no-op on prod** (hot-fix applied this already)
  3. `CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_state_one_bootstrap_per_identity` — likely missing; will create
  4. `DROP MATERIALIZED VIEW IF EXISTS core.mv_latest_agent_states` + `CREATE MATERIALIZED VIEW` + two `CREATE INDEX` — drops the matview (from migration 008, which lacks the `synthetic` column), then rebuilds it with `synthetic` projected and `WHERE synthetic = false` baked in. This matches the terminal measured-only definition so a failed/manual stop before 023 cannot expose bootstrap rows through the matview.
- **Idempotent:** The ALTER + partial index are. The matview DROP/CREATE is destructive but the fallback at `src/db/mixins/state.py:103` covers the brief drop window. The two post-CREATE indexes lack `IF NOT EXISTS` but that is safe because they target the freshly-created matview (the DROP cascades their predecessors).
- **Dependencies:** `core.agent_state.synthetic` column (exists via hot-fix)
- **Risk:** Medium. The matview rebuild is the main action. Brief window where the matview is absent — existing fallback handles this.

### 023_matview_measured_only.sql (renumbered from 019, this PR)
- **DDL:** Drop + recreate `core.mv_latest_agent_states` with the same measured-only `WHERE synthetic = false` definition, plus the same two indexes.
- **Idempotent:** Same notes as 022: DROP IF EXISTS is safe; CREATE indexes are safe on a fresh matview.
- **Dependencies:** `synthetic` column must exist (it does, from hot-fix); must run **after** 022 to preserve registry/order semantics.
- **Risk:** Medium. Same brief matview-absent window; same fallback covers it. 022 already creates the desired terminal rowset, so 023 is a confirming rebuild plus registry marker.

---

## 4. Renumbering rationale

| Old file | New file | Reason |
|----------|----------|--------|
| `018_bootstrap_synthetic_state.sql` | `022_bootstrap_synthetic_state.sql` | Slot 18 permanently occupied by phantom in prod registry. Renumber to next free slot to avoid PK collision. |
| `019_matview_measured_only.sql` | `023_matview_measured_only.sql` | Depends on 022; renumbered to sit immediately after it and enforce apply order. Leaving at 019 would create a conceptual inversion (019 < 022 but logically depends on 022). |
| `020_progress_flat_telemetry.sql` | unchanged name, INSERT fixed 18→20 | Was a latent bug: the file body INSERT claimed slot 18, which would have silently collided on prod. Fixed to claim slot 20. |

No collision with open PRs — there are none at time of triage (2026-04-29).

---

## 5. psql commands Kenny must run (in order)

Connect as the governance DB owner, then:

```sql
-- Step 1: fill registry gaps 14, 15, 16 (all idempotent)
\i db/postgres/migrations/014_seed_epoch_2.sql
\i db/postgres/migrations/015_agent_process_bindings.sql
\i db/postgres/migrations/016_same_host_ppid_consistent.sql

-- Step 2: register telemetry slot 20 (tables already exist in prod)
\i db/postgres/migrations/020_progress_flat_telemetry.sql

-- Step 3: seed epoch 3 if not already in registry
\i db/postgres/migrations/021_seed_epoch_3.sql

-- Step 4: bootstrap_synthetic_state (renumbered 022)
--   - ALTER TABLE and partial index are no-ops (hot-fix applied them)
--   - creates uq_agent_state_one_bootstrap_per_identity unique index
--   - rebuilds matview with WHERE synthetic = false baked in
\i db/postgres/migrations/022_bootstrap_synthetic_state.sql

-- Step 5: matview_measured_only (renumbered 023)
--   - confirming rebuild of the same measured-only terminal schema state
\i db/postgres/migrations/023_matview_measured_only.sql
```

**Verify after each step:**

```sql
SELECT version, name, applied_at
FROM core.schema_migrations
ORDER BY version;
-- Expected terminal sequence: 1–17, 18 (phantom), 20, 21, 22, 23
-- Gaps at 14/15/16 will close after steps 1–3.
-- Gap at 19 is intentional (slot never used).
```

---

## 6. Registry back-fill SQL (if Kenny prefers manual inserts over running the files)

All of these are safe to run even if a step was already applied (ON CONFLICT DO NOTHING):

```sql
-- Back-fill 14, 15, 16
INSERT INTO core.schema_migrations (version, name) VALUES (14, 'seed_epoch_2') ON CONFLICT (version) DO NOTHING;
INSERT INTO core.schema_migrations (version, name) VALUES (15, 'agent_process_bindings') ON CONFLICT (version) DO NOTHING;
INSERT INTO core.schema_migrations (version, name) VALUES (16, 'same_host_ppid_consistent') ON CONFLICT (version) DO NOTHING;

-- Register telemetry at slot 20 (phantom is already at 18; this is additive)
INSERT INTO core.schema_migrations (version, name) VALUES (20, 'progress flat telemetry tables') ON CONFLICT (version) DO NOTHING;

-- Register epoch 3
INSERT INTO core.schema_migrations (version, name) VALUES (21, 'seed_epoch_3') ON CONFLICT (version) DO NOTHING;
```

> **Warning:** The back-fill approach for 014–016 only makes sense if the corresponding schema objects *already exist* in prod. Verify `core.agent_process_bindings` exists before inserting slot 15/16 without running their DDL.

---

## 7. What is NOT in this PR

- No DDL executed
- No `core.schema_migrations` rows inserted or deleted
- No deletion of the phantom slot 18 entry (it is correct history; leave it)
- No merge — this is a draft for Kenny's review

---

## 8. Open questions for Kenny

1. **014/015/016 schema objects** — do they already exist in prod (applied out-of-band like the phantom)? If yes, only the registry back-fill SQL in §6 is needed, not the full file runs. If no, the full `\i` is needed.
2. **uq_agent_state_one_bootstrap_per_identity** — does this index exist in prod? The hot-fix description only mentions the column and the partial index, not the unique constraint. If the column has duplicate `synthetic=true` rows for any identity, migration 022 will fail on the unique index CREATE — check first with: `SELECT identity_id, COUNT(*) FROM core.agent_state WHERE synthetic = true GROUP BY identity_id HAVING COUNT(*) > 1;`
3. **Matview timing** — the DROP/CREATE in 022 + 023 has a brief window where the matview is absent. The fallback at `src/db/mixins/state.py:103` covers read-path callers. If the server is under heavy load, consider running these during a low-traffic window.
4. **Slot 19 gap** — after this triage, slot 19 is permanently unused. That is intentional. The migration doctor's gap-check may flag it; that flag can be suppressed or the doctor can be taught about explicit gaps.
