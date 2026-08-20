-- 064_knowledge_closure_class.sql
--
-- `status` is two-valued over a three-valued world. A discovery is open, or it
-- is closed, and the state a reconciler keeps actually encountering is neither:
-- "not currently observed, cause unknown". Forced to pick, every reconciler
-- picks the one that shortens the queue.
--
-- The cost is not local to the entries that get it wrong. `status='resolved'`
-- records no standard, so a reader cannot distinguish a closure resting on a
-- deployed fix with positively observed effect from one resting on a
-- correlation with a date. The weak closure therefore does not stay attached to
-- its own row; it dilutes what "resolved" means across the whole graph,
-- retroactively, including entries other agents closed rigorously. Downstream
-- LLM readers treat status as ground truth, so the dilution propagates into
-- every inference built on it.
--
-- This adds the missing axis WITHOUT touching the status vocabulary, the
-- permission model, or any existing row's meaning. Two nullable columns:
--
--   closure_class     -- by what standard was this closed
--   closure_evidence  -- the specific evidence, shaped per class
--
-- Deliberately NOT required, and deliberately NOT backfilled. A required field
-- would break the KG gardener's mechanical auto-resolve on its next run. A
-- backfill would invent a standard for closures that declared none, which is
-- the exact failure this migration exists to stop. Existing closed rows keep
-- closure_class IS NULL, and NULL reads as "declared no standard" — which is
-- the honest label for them, including for the three I closed today.
--
-- Vocabulary, deliberately small. Each value names what the closer actually
-- had, not how confident they felt:
--
--   fix_verified   a named change is deployed AND its effect was positively
--                  observed. Positive means the new behaviour was seen, not
--                  that the old symptom is missing.
--   unobserved     the condition stopped occurring and the cause was not
--                  established. This is the honest label for closure-by-absence
--                  and it is deliberately unflattering.
--   not_reproducible  could not be reproduced from the report.
--   obsolete       the surface it describes no longer exists.
--   duplicate      another entry covers it.
--
-- Everything is one transaction including registration, per 060.

BEGIN;

ALTER TABLE knowledge.discoveries
    ADD COLUMN IF NOT EXISTS closure_class text;

ALTER TABLE knowledge.discoveries
    ADD COLUMN IF NOT EXISTS closure_evidence jsonb;

ALTER TABLE knowledge.discoveries
    DROP CONSTRAINT IF EXISTS discoveries_closure_class_check;

ALTER TABLE knowledge.discoveries
    ADD CONSTRAINT discoveries_closure_class_check
    CHECK (
        closure_class IS NULL
        OR closure_class IN (
            'fix_verified',
            'unobserved',
            'not_reproducible',
            'obsolete',
            'duplicate'
        )
    );

-- A closure class on an entry that is still open is a contradiction: it claims
-- a standard for a closure that has not happened. Rejecting it here means the
-- pair cannot drift apart no matter which writer sets them.
ALTER TABLE knowledge.discoveries
    DROP CONSTRAINT IF EXISTS discoveries_closure_class_requires_closed;

ALTER TABLE knowledge.discoveries
    ADD CONSTRAINT discoveries_closure_class_requires_closed
    CHECK (
        closure_class IS NULL
        OR status IN ('resolved', 'closed', 'wont_fix', 'superseded')
    );

CREATE INDEX IF NOT EXISTS idx_discoveries_closure_class
    ON knowledge.discoveries (closure_class)
    WHERE closure_class IS NOT NULL;

-- Postcondition. 047 registered itself while its ADDs had failed, which is why
-- enforcement silently vanished for two months. A migration that reports
-- success without making its change is a failed migration.
DO $$
DECLARE
    missing text;
BEGIN
    SELECT string_agg(expected.conname, ', ')
    INTO missing
    FROM (
        VALUES
            ('discoveries_closure_class_check'),
            ('discoveries_closure_class_requires_closed')
    ) AS expected(conname)
    LEFT JOIN pg_constraint actual
        ON actual.conname = expected.conname
       AND actual.conrelid = 'knowledge.discoveries'::regclass
       AND actual.convalidated
    WHERE actual.oid IS NULL;

    IF missing IS NOT NULL THEN
        RAISE EXCEPTION
            'closure-class constraints missing or unvalidated: %', missing;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'knowledge'
          AND table_name = 'discoveries'
          AND column_name = 'closure_class'
    ) THEN
        RAISE EXCEPTION 'knowledge.discoveries.closure_class was not created';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'knowledge'
          AND table_name = 'discoveries'
          AND column_name = 'closure_evidence'
    ) THEN
        RAISE EXCEPTION 'knowledge.discoveries.closure_evidence was not created';
    END IF;
END;
$$;

INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (64, 'knowledge_closure_class', NOW())
ON CONFLICT (version) DO NOTHING;

COMMIT;
