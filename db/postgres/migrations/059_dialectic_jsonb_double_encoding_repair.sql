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
-- The split is exact and dated — it is the day the saga became the writer.
-- Counts below were taken 2026-08-10 and DRIFT UPWARD until the lease plane is
-- restarted, because the bug is still live; treat them as a shape, not a
-- checksum. Zero boundary violations in either direction:
--
--   core.dialectic_sessions.resolution_json
--     object  33 rows  2025-11-25 .. 2026-06-23
--     string  17 rows  2026-06-28 .. 2026-08-09
--
--   core.dialectic_sessions.paused_agent_state_json
--     object  29 rows  2025-12-07 .. 2026-06-23
--     string  57 rows  2026-06-28 .. 2026-08-10
--
--   coordination.session_resolution_sagas.resolution_payload_json
--     string  17 rows  (every row the table has)
--
--   effects.payloads.required_leases
--     string  40 rows  2026-06-28 .. 2026-08-10  (every row the table has)
--
-- What the repair actually recovers, stated honestly: of the 57
-- `paused_agent_state_json` rows, 54 are the empty `"{}"` and only **3** hold a
-- real EISV snapshot (`"{\"E\":0.719,\"I\":0.795,...}"`). So this restores 3
-- unreadable snapshots, not 57 — 28 further rows were already readable and are
-- untouched. The 40 `required_leases` rows all hold real lease arrays and are
-- the larger practical recovery.
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

-- Snapshot before rewriting anything. 90+ rows change in place under a manual
-- migration regime with no down path; four lines buys the undo.
CREATE TABLE IF NOT EXISTS core.dialectic_jsonb_repair_059 AS
SELECT session_id, resolution_json, paused_agent_state_json, now() AS captured_at
FROM core.dialectic_sessions
WHERE jsonb_typeof(resolution_json) = 'string'
   OR jsonb_typeof(paused_agent_state_json) = 'string';

-- resolution_json: NULL, not '{}'.
--
-- Unwrapping these yields an empty object, and `{}` reads as "this session
-- resolved, with no conditions" — a plausible-looking value that was never
-- measured. Migration 058 was written to kill exactly that pattern: NULL
-- carries a warning, a well-formed empty value does not. The truth is "no
-- resolution was ever recorded" (the ordering bug fixed alongside this), and
-- the column is nullable, so say that.
--
-- Every one of these is verified empty before the rewrite; a double-encoded
-- resolution with real content would be unwrapped, not nulled.
UPDATE core.dialectic_sessions
SET resolution_json = CASE
        WHEN pg_temp.jsonb_unwrap_double_encoded(resolution_json) = '{}'::jsonb THEN NULL
        ELSE pg_temp.jsonb_unwrap_double_encoded(resolution_json)
    END
WHERE jsonb_typeof(resolution_json) = 'string';

UPDATE core.dialectic_sessions
SET paused_agent_state_json = pg_temp.jsonb_unwrap_double_encoded(paused_agent_state_json)
WHERE jsonb_typeof(paused_agent_state_json) = 'string';

UPDATE coordination.session_resolution_sagas
SET resolution_payload_json = pg_temp.jsonb_unwrap_double_encoded(resolution_payload_json)
WHERE jsonb_typeof(resolution_payload_json) = 'string';

-- effects.payloads.required_leases — the same defect in EffectRepo, found by
-- the review that checked whether the dialectic sites were the complete set.
-- They were not: all 40 rows since 2026-06-28 store a jsonb string where an
-- array belongs. `EffectReconcile.decode_leases/1` has been silently absorbing
-- it with an `is_binary` branch, which is why nothing ever surfaced.
UPDATE effects.payloads
SET required_leases = pg_temp.jsonb_unwrap_double_encoded(required_leases)
WHERE jsonb_typeof(required_leases) = 'string';

-- Register migration
INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (59, 'dialectic_jsonb_double_encoding_repair', NOW())
ON CONFLICT (version) DO NOTHING;

COMMIT;

-- Verification — every one of these must return 0 rows after the migration:
--
--   SELECT count(*) FROM core.dialectic_sessions
--    WHERE jsonb_typeof(resolution_json) = 'string';
--   SELECT count(*) FROM core.dialectic_sessions
--    WHERE jsonb_typeof(paused_agent_state_json) = 'string';
--   SELECT count(*) FROM coordination.session_resolution_sagas
--    WHERE jsonb_typeof(resolution_payload_json) = 'string';
--   SELECT count(*) FROM effects.payloads
--    WHERE jsonb_typeof(required_leases) = 'string';
--
-- And the payloads must be reachable by key again:
--
--   SELECT count(*) FROM core.dialectic_sessions
--    WHERE paused_agent_state_json ->> 'E' IS NOT NULL;      -- 28 -> 31
--   SELECT count(*) FROM effects.payloads
--    WHERE required_leases -> 0 ->> 'surface' IS NOT NULL;   -- 0 -> 40
--
-- NOTE: this migration does NOT invent resolutions. The 17 `resolution_json`
-- rows are set to NULL, not `{}` — the resolution was already empty before it
-- was double-encoded (a separate ordering bug in `handle_submit_synthesis`,
-- fixed in the same change). Those sessions have no recorded resolution and
-- never will. NULL says that; `{}` would read as "resolved, no conditions".
--
-- Undo: core.dialectic_jsonb_repair_059 holds the pre-repair values for the
-- dialectic columns. Drop it once the repair has been confirmed in place.
