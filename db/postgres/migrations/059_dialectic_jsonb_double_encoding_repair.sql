-- 059_dialectic_jsonb_double_encoding_repair.sql
--
-- Repairs jsonb columns that the BEAM lease plane double-encoded between
-- 2026-06-28 and 2026-08-10.
--
-- `UnitaresLeasePlane.DialecticSaga` bound `Jason.encode!` output to a bare
-- `$N::jsonb` parameter. Postgrex infers a bare jsonb param as jsonb and runs
-- its own JSON encoder over the value, so an already-encoded binary landed as
-- a jsonb *string* rather than an object. The write succeeded, the column
-- SELECTs fine as text, and every key lookup through `->` / `->>` returns
-- NULL. Nothing ever errored.
--
-- The split is exact and dated — it is the day the saga became the writer:
--
--   core.dialectic_sessions.resolution_json
--     object  33 rows  2025-11-25 .. 2026-06-23
--     string  17 rows  2026-06-28 .. 2026-08-09
--
--   core.dialectic_sessions.paused_agent_state_json
--     object  29 rows  2025-12-07 .. 2026-06-23
--     string  56 rows  2026-06-28 .. 2026-08-10
--
--   coordination.session_resolution_sagas.resolution_payload_json
--     string  17 rows  (every row the table has)
--
-- `paused_agent_state_json` is the one that cost something: it holds the
-- paused agent's EISV snapshot, and 56 rows of real state
-- (`"{\"E\":0.735,\"I\":0.724,...}"`) have been unreadable to any JSON
-- consumer for six weeks.
--
-- The repair is information-preserving: a double-encoded value is exactly
-- `to_jsonb(<original text>)`, so `#>> '{}'` recovers the original JSON text
-- and re-parsing it restores the object. Applied only where `jsonb_typeof`
-- is 'string' AND the payload re-parses, so a legitimately-stored JSON string
-- (none exist in these columns today, but the guard costs nothing) is left
-- alone rather than corrupted in the other direction.
--
-- The writer fix ships in the same change; without it these rows would
-- re-accumulate from the next resolution onward.

BEGIN;

-- Recoverable only if the unwrapped text actually parses as JSON. Anything
-- else is left untouched and will show up in the verification query below.
CREATE OR REPLACE FUNCTION pg_temp.jsonb_unwrap_double_encoded(v jsonb)
RETURNS jsonb
LANGUAGE plpgsql
IMMUTABLE
AS $$
BEGIN
    IF v IS NULL OR jsonb_typeof(v) <> 'string' THEN
        RETURN v;
    END IF;
    RETURN (v #>> '{}')::jsonb;
EXCEPTION WHEN others THEN
    RETURN v;
END;
$$;

UPDATE core.dialectic_sessions
SET resolution_json = pg_temp.jsonb_unwrap_double_encoded(resolution_json)
WHERE jsonb_typeof(resolution_json) = 'string';

UPDATE core.dialectic_sessions
SET paused_agent_state_json = pg_temp.jsonb_unwrap_double_encoded(paused_agent_state_json)
WHERE jsonb_typeof(paused_agent_state_json) = 'string';

UPDATE coordination.session_resolution_sagas
SET resolution_payload_json = pg_temp.jsonb_unwrap_double_encoded(resolution_payload_json)
WHERE jsonb_typeof(resolution_payload_json) = 'string';

COMMIT;

-- Verification — every one of these must return 0 rows after the migration:
--
--   SELECT count(*) FROM core.dialectic_sessions
--    WHERE jsonb_typeof(resolution_json) = 'string';
--   SELECT count(*) FROM core.dialectic_sessions
--    WHERE jsonb_typeof(paused_agent_state_json) = 'string';
--   SELECT count(*) FROM coordination.session_resolution_sagas
--    WHERE jsonb_typeof(resolution_payload_json) = 'string';
--
-- And the EISV snapshots must be reachable again:
--
--   SELECT count(*) FROM core.dialectic_sessions
--    WHERE paused_agent_state_json ->> 'E' IS NOT NULL;
--
-- NOTE: this migration does NOT invent resolutions. The 17 repaired
-- `resolution_json` rows become an empty object `{}` because that is what was
-- actually written — the resolution was already empty before it was
-- double-encoded (a separate ordering bug in `handle_submit_synthesis`, fixed
-- in the same change). Those 17 sessions have no recorded resolution and
-- never will; the repair makes them honestly empty rather than malformed.
