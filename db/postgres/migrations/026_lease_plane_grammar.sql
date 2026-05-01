-- 026_lease_plane_grammar.sql
--
-- Phase A storage-layer enforcement for surface-lease-plane.
--
-- Implements RFC v0.8:
--   §7.2.2 — Postgres CHECK constraint on surface_id scheme grammar
--            (closes three-voice convergence: dialectic BLOCK-2 + code-reviewer
--             BLOCK-1 + live-verifier DRIFT-A).
--   §7.2.3 — surface_kind becomes a generated column derived from surface_id
--            (closes code-reviewer CONCERN-2 + live-verifier DRIFT-B).
--
-- Pre-flight rule (operator decision 2026-04-30, see Phase A plan):
-- aborts with a clear remediation error if lease_plane.surface_leases
-- contains rows when 026 runs against a fresh schema. Avoids silent
-- destructive DROP COLUMN against populated data.
--
-- Idempotent: safe to re-run; the DO block detects post-migration state
-- (surface_kind already generated) and skips both the pre-flight abort and
-- the redundant ALTER TABLE.

DO $$
DECLARE
    row_count int;
    grammar_constraint_exists bool;
    surface_kind_is_generated bool;
BEGIN
    -- Detect prior application of this migration: surface_kind = generated column.
    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'lease_plane'
          AND table_name = 'surface_leases'
          AND column_name = 'surface_kind'
          AND is_generated = 'ALWAYS'
    ) INTO surface_kind_is_generated;

    -- Pre-flight: bail if rows exist AND this is a fresh application
    -- (re-run against a populated, already-migrated table is safe).
    IF NOT surface_kind_is_generated THEN
        SELECT count(*) INTO row_count FROM lease_plane.surface_leases;
        IF row_count > 0 THEN
            RAISE EXCEPTION
              'Migration 026 aborted: lease_plane.surface_leases contains % row(s). '
              'This migration drops and re-adds surface_kind as a generated column, '
              'which is not safe against existing rows. Remediate by either: '
              '(a) draining existing leases (release + verify released_at IS NOT NULL on every row), '
              'or (b) running a separate data-migration step that re-INSERTs each row into the '
              'post-026 schema. See docs/proposals/surface-lease-plane-v0.md §7.2.3.',
              row_count;
        END IF;
    END IF;

    -- Grammar CHECK on surface_id (idempotent via existence check).
    SELECT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'surface_id_grammar'
          AND conrelid = 'lease_plane.surface_leases'::regclass
    ) INTO grammar_constraint_exists;

    IF NOT grammar_constraint_exists THEN
        ALTER TABLE lease_plane.surface_leases
            ADD CONSTRAINT surface_id_grammar
            CHECK (surface_id ~ '^(file://|dialectic:/|resident:/|capture:/|td:/)');
    END IF;

    -- Convert surface_kind to generated column (idempotent).
    IF NOT surface_kind_is_generated THEN
        ALTER TABLE lease_plane.surface_leases DROP COLUMN surface_kind;
        ALTER TABLE lease_plane.surface_leases
            ADD COLUMN surface_kind text
            GENERATED ALWAYS AS (split_part(surface_id, ':', 1)) STORED;
    END IF;
END $$;

INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (26, 'lease_plane_grammar', NOW())
ON CONFLICT (version) DO NOTHING;
