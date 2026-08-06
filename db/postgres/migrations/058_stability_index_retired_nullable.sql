-- 058: let the retired stability_index column read as UNKNOWN, not as zero.
--
-- WHY
-- `stability_index` was a live signal (1.0 - S). It was retired in commit
-- 20684dd1 (2026-03-26, "Dashboard improvements: energy field, race fix,
-- pagination, filtering") -- a telemetry-semantics change that rode along inside
-- a UI commit. Since then the writers have hardcoded a constant and the column
-- has kept being written to every row.
--
-- Measured on core.agent_state (non-synthetic), stddev / mean by month:
--   2025-12  sd 0.333  avg 0.481     <- live, varying
--   2026-01  sd 0.040  avg 0.843
--   2026-02  sd 0.036  avg 0.850
--   2026-03  sd 0.060  avg 0.857
--   2026-04  sd 0.000  avg 0.000     <- retired here
--   2026-05..2026-08  sd 0.000  avg 0.000
--
-- The failure this fixes is not that the field is dead. It is that a dead field
-- written as 0.0 is indistinguishable, at the query layer, from a real signal
-- that happens to be flat. It reads as evidence. During this audit it was
-- reported as "sd = 0.0000 across 237 agents -- zero information", i.e. cited as
-- a measurement showing absent individuality, when it was never a measurement at
-- all. It also had teeth: the R1 trajectory reader consumed it as the "always-1.0
-- S channel" until PR #530 fixed the reader in 2026-05. The reader was fixed;
-- the tombstone was not.
--
-- NULL is the honest encoding: absent, not zero. Consumers that genuinely need a
-- value now fail loudly instead of silently averaging a constant.
--
-- WHAT
-- Drop NOT NULL and the 0.5 default. No data is rewritten.
--
-- FORWARD-ONLY, DELIBERATELY. Existing rows keep their values:
--   * 2025-12..2026-03 rows hold REAL measurements -- destroying them would
--     delete the only evidence the signal was ever alive, and the only record of
--     when it died.
--   * 2026-04..now rows hold the artifact constant. Backfilling those to NULL is
--     a ~66k-row UPDATE on a live governance table and is an OPERATOR decision,
--     not a migration side effect. Until then, `stability_index = 0 AND
--     recorded_at >= '2026-04-01'` identifies the artifact era.
-- This mirrors the raw_obs precedent (PR #1294): forward-only, no backfill.
--
-- SAFETY
-- DROP NOT NULL is backward-compatible: existing rows are untouched and any
-- writer still supplying a value continues to work. Reversible via the down
-- migration below (it re-fills NULLs with the historical default first, so the
-- constraint can be restored).
--
-- Blast radius verified before writing: the column appears in three SELECT lists
-- in src/db/mixins/state.py but is never read -- reconstruct_eisv_series builds
-- {E,I,S,V} from state_json.E, integrity, entropy and volatility, and the code
-- says so at state.py:384-386. No arithmetic consumer exists in src/, scripts/,
-- agents/ or dashboard/.

ALTER TABLE core.agent_state
    ALTER COLUMN stability_index DROP NOT NULL,
    ALTER COLUMN stability_index DROP DEFAULT;

COMMENT ON COLUMN core.agent_state.stability_index IS
    'RETIRED 2026-03-26 (commit 20684dd1). Was 1.0 - S. NULL = not computed. '
    'Rows before 2026-04 hold real measurements; rows from 2026-04 to the '
    'deploy of migration 058 hold an artifact constant (0.0) and are not data. '
    'Do not aggregate this column across that boundary.';

-- DOWN (manual, not auto-applied):
--   UPDATE core.agent_state SET stability_index = 0.5 WHERE stability_index IS NULL;
--   ALTER TABLE core.agent_state
--       ALTER COLUMN stability_index SET DEFAULT 0.5,
--       ALTER COLUMN stability_index SET NOT NULL;
