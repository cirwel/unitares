-- 072_coordination_events_service_open.sql
--
-- 035 locked audit.coordination_events.service to six emitter ids, four of
-- which are the names of one deployment's resident agents. Every install got
-- that list: an adopter's own agent could not record a coordination event
-- under its own service id, and the refusal (a CHECK violation inside an
-- emitter that swallows its own errors) looked like "nothing happened".
--
-- Which services exist is deployment configuration, the same as which
-- residents exist (UNITARES_RESIDENTS). This replaces 035's list, under the
-- same name, with a format rule: a lowercase identifier, a letter first,
-- then letters, digits or underscores, at most 63 characters. It keeps what
-- the list guarded against (free text, mixed case, whitespace, an empty id)
-- without naming anybody. src/coordination_events.py validates the same
-- pattern client-side (SERVICE_PATTERN) so a bad id fails before the INSERT.
--
-- Nothing is backfilled and no row changes: every id 035 admitted matches
-- the new rule. ALTER on the partitioned parent applies to every partition.
--
-- Everything is one transaction including registration, per 060.

BEGIN;

ALTER TABLE audit.coordination_events
    DROP CONSTRAINT IF EXISTS coordination_events_service_check;

ALTER TABLE audit.coordination_events
    ADD CONSTRAINT coordination_events_service_check
    CHECK (service ~ '^[a-z][a-z0-9_]{0,62}$');

-- Postcondition, as 071 does: the constraint's NAME existed after 035, so its
-- presence proves nothing; its definition is what this migration changes.
DO $$
DECLARE
    definition text;
BEGIN
    SELECT pg_get_constraintdef(c.oid)
    INTO definition
    FROM pg_constraint c
    WHERE c.conname = 'coordination_events_service_check'
      AND c.conrelid = 'audit.coordination_events'::regclass
      AND c.convalidated;

    IF definition IS NULL THEN
        RAISE EXCEPTION
            'coordination_events_service_check missing or unvalidated';
    END IF;

    IF position('ANY' IN upper(definition)) > 0 THEN
        RAISE EXCEPTION
            'coordination_events_service_check still enumerates services: %',
            definition;
    END IF;
END;
$$;

INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (72, 'coordination_events_service_open', NOW())
ON CONFLICT (version) DO NOTHING;

COMMIT;
