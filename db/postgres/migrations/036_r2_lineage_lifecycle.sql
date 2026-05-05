-- Migration 036: R2 lineage lifecycle columns on core.identities
--
-- Extends R1 PR #306 (migration 031) which already shipped
-- provisional_lineage, provisional_score_id, provisional_recorded_at,
-- confirmed_at. R2 adds the columns required for the demote/archive
-- transitions, sweeper cadence guard, and forward-only chain counter.
--
-- See: docs/ontology/r2-honest-memory-integration.md §Storage
--      docs/handoffs/2026-05-04-r2-implementation-plan.md PR 1

ALTER TABLE core.identities
    ADD COLUMN IF NOT EXISTS lineage_declared_at  TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS lineage_demoted_at   TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS lineage_archived_at  TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS lineage_last_eval_at TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS chain_obs_count      INTEGER     NOT NULL DEFAULT 0;

COMMENT ON COLUMN core.identities.lineage_declared_at  IS 'R2: stamped when parent_agent_id first set at onboard';
COMMENT ON COLUMN core.identities.lineage_demoted_at   IS 'R2: stamped on * → demoted; parent_agent_id is also cleared';
COMMENT ON COLUMN core.identities.lineage_archived_at  IS 'R2: stamped on grace-window expiration; parent_agent_id retained but inert';
COMMENT ON COLUMN core.identities.lineage_last_eval_at IS 'R2: updated by sweeper/check-in trigger to enforce cadence guards';
COMMENT ON COLUMN core.identities.chain_obs_count      IS 'R2: forward-only chain counter; incremented post-promotion, reset to 0 on confirmed→demoted clawback';

-- Sweeper-friendly partial index: only rows the FSM cares about.
-- Provisional rows (still in flight) and confirmed rows (subject to
-- post-promotion divergence clawback) are the candidates the sweeper
-- re-evaluates. Archived/demoted rows are terminal and excluded from
-- the index — the sweeper has no work to do on them.
--
-- Drop-then-create so this migration is idempotent against dev DBs that
-- ran an earlier version of slot 036 with a looser predicate (the
-- predicate change is the load-bearing edit; CREATE INDEX IF NOT EXISTS
-- alone would not rebuild the index in that case).
DROP INDEX IF EXISTS core.idx_identities_provisional_eval;
CREATE INDEX IF NOT EXISTS idx_identities_provisional_eval
    ON core.identities (lineage_last_eval_at)
    WHERE (provisional_lineage = TRUE OR confirmed_at IS NOT NULL)
      AND lineage_archived_at IS NULL
      AND lineage_demoted_at IS NULL;

INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (36, 'r2_lineage_lifecycle', NOW())
ON CONFLICT (version) DO NOTHING;
