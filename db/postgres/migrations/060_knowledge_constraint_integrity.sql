-- 060_knowledge_constraint_integrity.sql
--
-- Migration 047 widened the knowledge.discoveries enum checks, but it was not
-- transactional. On the live database its severity/status DROP statements
-- succeeded, the replacement ADD statements failed on rows outside the old
-- vocabulary, and psql continued far enough to register version 47. The table
-- has consequently accepted unconstrained values since 2026-06-12.
--
-- The out-of-contract severities came from store_discovery_internal(), whose
-- recovery writers used the public API's documented aliases while bypassing
-- its validator. Canonicalize those aliases before restoring the four-level
-- severity contract. status='cold' is not drift: the lifecycle report and
-- query paths deliberately use it as the deep-archive tier, so add it to the
-- authoritative status vocabulary.
--
-- Everything is one transaction, including registration. Any unknown value,
-- failed constraint re-add, or failed postcondition rolls the DROP statements
-- back instead of manufacturing another partially-applied migration.

BEGIN;

WITH normalized AS (
    SELECT id,
           CASE lower(btrim(severity))
               WHEN 'info'          THEN 'low'
               WHEN 'informational' THEN 'low'
               WHEN 'warn'          THEN 'medium'
               WHEN 'warning'       THEN 'medium'
               WHEN 'error'         THEN 'high'
               WHEN 'fatal'         THEN 'critical'
               WHEN 'urgent'        THEN 'critical'
               ELSE lower(btrim(severity))
           END AS severity
    FROM knowledge.discoveries
    WHERE severity IS NOT NULL
      AND lower(btrim(severity)) IN (
          'low', 'medium', 'high', 'critical',
          'info', 'informational', 'warn', 'warning',
          'error', 'fatal', 'urgent'
      )
)
UPDATE knowledge.discoveries AS d
SET severity = normalized.severity
FROM normalized
WHERE d.id = normalized.id
  AND d.severity IS DISTINCT FROM normalized.severity;

UPDATE knowledge.discoveries
SET status = lower(btrim(status))
WHERE lower(btrim(status)) IN (
    'open', 'resolved', 'archived', 'disputed', 'closed', 'wont_fix',
    'superseded', 'cold'
)
AND status IS DISTINCT FROM lower(btrim(status));

ALTER TABLE knowledge.discoveries
    DROP CONSTRAINT IF EXISTS discoveries_severity_check;
ALTER TABLE knowledge.discoveries
    ADD CONSTRAINT discoveries_severity_check
    CHECK (severity IN ('low', 'medium', 'high', 'critical'));

ALTER TABLE knowledge.discoveries
    DROP CONSTRAINT IF EXISTS discoveries_status_check;
ALTER TABLE knowledge.discoveries
    ADD CONSTRAINT discoveries_status_check
    CHECK (status IN ('open', 'resolved', 'archived', 'disputed', 'closed', 'wont_fix', 'superseded', 'cold'));

DO $$
DECLARE
    missing_constraints text[];
BEGIN
    SELECT array_agg(required.name ORDER BY required.name)
    INTO missing_constraints
    FROM (
        VALUES
            ('discoveries_severity_check'::text),
            ('discoveries_status_check'::text)
    ) AS required(name)
    LEFT JOIN pg_constraint AS actual
      ON actual.conrelid = 'knowledge.discoveries'::regclass
     AND actual.conname = required.name
     AND actual.contype = 'c'
     AND actual.convalidated
    WHERE actual.oid IS NULL;

    IF missing_constraints IS NOT NULL THEN
        RAISE EXCEPTION
            'knowledge constraint repair incomplete; missing/unvalidated: %',
            missing_constraints;
    END IF;

    IF EXISTS (
        SELECT 1
        FROM knowledge.discoveries
        WHERE severity IS NOT NULL
          AND severity NOT IN ('low', 'medium', 'high', 'critical')
    ) THEN
        RAISE EXCEPTION 'knowledge.discoveries still contains invalid severity values';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM knowledge.discoveries
        WHERE status NOT IN (
            'open', 'resolved', 'archived', 'disputed', 'closed', 'wont_fix',
            'superseded', 'cold'
        )
    ) THEN
        RAISE EXCEPTION 'knowledge.discoveries still contains invalid status values';
    END IF;
END;
$$;

INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (60, 'knowledge_constraint_integrity', NOW())
ON CONFLICT (version) DO NOTHING;

COMMIT;
