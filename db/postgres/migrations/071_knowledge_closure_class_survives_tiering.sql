-- 071_knowledge_closure_class_survives_tiering.sql
--
-- 064 added closure_class / closure_evidence and a CHECK that a class may only
-- sit on a closed row: status IN ('resolved', 'closed', 'wont_fix',
-- 'superseded'). Nothing wrote the class then, so the CHECK never met a row.
--
-- Once the class is stored, that list is too narrow. The KG lifecycle moves a
-- resolved row to 'archived' after 30 days and an archived row to 'cold' after
-- 90 (src/knowledge_graph_lifecycle.py). Both moves only change status; neither
-- touches the class, and neither should: archiving is retention, not a
-- re-opening, so the standard the row was closed by is still the standard it
-- was closed by. Under 064's CHECK every classified row would refuse the move:
--
--   * AGE backend: update_discovery syncs the status into this table inside
--     the Cypher transaction, the CHECK raises, the whole update rolls back and
--     returns False. The lifecycle ignores the result, so the row stays
--     'resolved' forever and every daily run logs the same error.
--   * Postgres backend: the UPDATE raises out of update_discovery and aborts
--     the rest of the lifecycle run (later archives and the cold sweep).
--
-- The class is a contradiction only on a row that is open again: 'open' and
-- 'disputed'. This replaces 064's CHECK, under the same name, with one that
-- admits a class on every other status. The writers clear the class when they
-- reopen a row, so the pair still cannot drift apart.
--
-- Same name on purpose: the constraint means what it meant ("a class needs a
-- closure"), and archived/cold are where closed rows go to be retained. A
-- second constraint beside 064's would have kept the narrow one in force.
--
-- Nothing is backfilled and no row changes. Every existing row satisfies the
-- wider CHECK because it satisfied the narrower one.
--
-- Everything is one transaction including registration, per 060.

BEGIN;

ALTER TABLE knowledge.discoveries
    DROP CONSTRAINT IF EXISTS discoveries_closure_class_requires_closed;

ALTER TABLE knowledge.discoveries
    ADD CONSTRAINT discoveries_closure_class_requires_closed
    CHECK (
        closure_class IS NULL
        OR status IN (
            'resolved',
            'closed',
            'wont_fix',
            'superseded',
            'archived',
            'cold'
        )
    );

-- Postcondition. The constraint's NAME already existed after 064, so its
-- presence proves nothing; its definition is what this migration changes.
-- 047 registered itself while its ADDs had failed, which is why enforcement
-- silently vanished for two months: a migration that reports success without
-- making its change is a failed migration.
DO $$
DECLARE
    definition text;
    admitted text;
    refused text;
BEGIN
    SELECT pg_get_constraintdef(c.oid)
    INTO definition
    FROM pg_constraint c
    WHERE c.conname = 'discoveries_closure_class_requires_closed'
      AND c.conrelid = 'knowledge.discoveries'::regclass
      AND c.convalidated;

    IF definition IS NULL THEN
        RAISE EXCEPTION
            'discoveries_closure_class_requires_closed missing or unvalidated';
    END IF;

    SELECT string_agg(status, ', ')
    INTO admitted
    FROM unnest(ARRAY[
        'resolved', 'closed', 'wont_fix', 'superseded', 'archived', 'cold'
    ]) AS status
    WHERE position(quote_literal(status) IN definition) = 0;

    IF admitted IS NOT NULL THEN
        RAISE EXCEPTION
            'discoveries_closure_class_requires_closed does not admit: % (%)',
            admitted, definition;
    END IF;

    SELECT string_agg(status, ', ')
    INTO refused
    FROM unnest(ARRAY['open', 'disputed']) AS status
    WHERE position(quote_literal(status) IN definition) > 0;

    IF refused IS NOT NULL THEN
        RAISE EXCEPTION
            'discoveries_closure_class_requires_closed admits a reopened status: % (%)',
            refused, definition;
    END IF;
END;
$$;

INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (71, 'knowledge_closure_class_survives_tiering', NOW())
ON CONFLICT (version) DO NOTHING;

COMMIT;
