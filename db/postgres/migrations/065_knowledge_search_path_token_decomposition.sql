-- 065_knowledge_search_path_token_decomposition.sql
--
-- Issue #1711, plus the schema drift found while fixing it.
--
-- == The reported bug ==
--
-- The symptom was filed as "hyphenated tokens never match the lexical arm".
-- Hyphens are not the cause. PostgreSQL's parser already decomposes a
-- standalone `unitares-paper-v7` into the compound lexeme plus its parts, and
-- websearch_to_tsquery builds the matching phrase query, so bare hyphenated
-- identifiers and every `slug-*` tag match without this migration. Verified
-- against the live corpus first.
--
-- The real cause is the slash. `cirwel/unitares-paper-v7` is classified as a
-- single `file` token and emitted as ONE undecomposed lexeme:
--
--     to_tsvector('english', 'repo cirwel/unitares-paper-v7 live')
--       -> 'cirwel/unitares-paper-v7':2 'live':3 'repo':1
--
-- so nothing can reach `unitares-paper-v7`. The same swallowing hits file paths
-- (`docs/ontology/identity.md`) and URL paths. In this corpus that is the common
-- case, not a corner case: 855 of 1521 discoveries carry a slash-joined token,
-- and repo/doc identifiers are exactly the high-precision queries callers issue.
--
-- Fix: index slash-joined spans a second time in decomposed form. Only those
-- spans are re-tokenized, not the whole field, so the vector grows a few percent
-- (327 -> 350 lexemes on the issue's repro row) rather than doubling. Per-field
-- weights are preserved, so ranking is unchanged for anything already matching.
--
-- == The drift found on the way ==
--
-- `search_vector` has two different definitions in this project:
--
--   * db/postgres/knowledge_schema.sql declares it GENERATED ALWAYS AS
--     (summary 'A' || details 'B') -- with NO tags term at all.
--   * The live governance database has a plain column maintained by a trigger,
--     knowledge.update_search_vector(), which DOES include tags at weight 'C'.
--
-- That trigger appears nowhere in the repository. It was applied to live by
-- hand and never committed, so the declared schema and the running schema have
-- disagreed silently. The consequence is not cosmetic: any database built from
-- the committed schema -- governance_test, a fresh deploy, a restore drill --
-- indexes no tags whatsoever, so tag-targeted lexical search returns nothing
-- there while working fine on live.
--
-- This migration converges both shapes on the trigger definition (the one live
-- already runs, and the only one of the two that can call a helper function).
-- DROP EXPRESSION is a no-op on live and demotes the generated column to a
-- plain one everywhere else, preserving stored data and the GIN index.
-- knowledge_schema.sql is updated in the same commit so new databases are born
-- correct rather than depending on this migration to repair them.
--
-- == Backfill ==
--
-- search_vector is a derived index column. The updated_at trigger is disabled
-- around the backfill because bumping updated_at across the whole corpus would
-- falsify the staleness signal that KG gardening and `knowledge(action=audit)`
-- read. Everything is one transaction, so a failure anywhere cannot leave that
-- trigger disabled.

BEGIN;

-- 1. Converge the column shape. No-op where search_vector is already plain.
ALTER TABLE knowledge.discoveries
    ALTER COLUMN search_vector DROP EXPRESSION IF EXISTS;

-- 2. The decomposition helper.
CREATE OR REPLACE FUNCTION knowledge.split_path_tokens(txt text)
RETURNS text
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $fn$
    -- Extract every span of two or more path segments joined by '/', and
    -- re-emit it with the separators replaced by spaces so the parser sees the
    -- segments as ordinary words. Returns '' when the input has no such span,
    -- which to_tsvector maps to an empty vector.
    SELECT coalesce(string_agg(replace(m[1], '/', ' '), ' '), '')
    FROM regexp_matches(
        coalesce(txt, ''),
        '[A-Za-z0-9_.@+-]+(?:/[A-Za-z0-9_.@+-]+)+',
        'g'
    ) AS m;
$fn$;

COMMENT ON FUNCTION knowledge.split_path_tokens(text) IS
    'Issue #1711: re-emit slash-joined tokens as separate words so their '
    'segments are individually searchable. PostgreSQL indexes owner/repo and '
    'a/b/c.md as single undecomposed `file` lexemes.';

-- 3. The vector builder. Tags at weight C are part of the contract here, which
--    is what the generated-column definition was missing.
CREATE OR REPLACE FUNCTION knowledge.update_search_vector()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
BEGIN
    NEW.search_vector :=
        setweight(to_tsvector('english', coalesce(NEW.summary, '')), 'A') ||
        setweight(to_tsvector('english', knowledge.split_path_tokens(NEW.summary)), 'A') ||
        setweight(to_tsvector('english', coalesce(NEW.details, '')), 'B') ||
        setweight(to_tsvector('english', knowledge.split_path_tokens(NEW.details)), 'B') ||
        setweight(to_tsvector('english', coalesce(array_to_string(NEW.tags, ' '), '')), 'C') ||
        setweight(to_tsvector('english', knowledge.split_path_tokens(array_to_string(NEW.tags, ' '))), 'C');
    RETURN NEW;
END;
$fn$;

-- 4. Ensure the trigger exists. CREATE OR REPLACE rather than DROP + CREATE so
--    the table is never momentarily unprotected.
CREATE OR REPLACE TRIGGER discoveries_search_update
    BEFORE INSERT OR UPDATE ON knowledge.discoveries
    FOR EACH ROW EXECUTE FUNCTION knowledge.update_search_vector();

-- 5. Backfill every existing row through the new definition.
DO $$
DECLARE
    has_timestamp_trigger boolean;
BEGIN
    SELECT EXISTS (
        SELECT 1 FROM pg_trigger
         WHERE tgrelid = 'knowledge.discoveries'::regclass
           AND tgname = 'trg_knowledge_discoveries_updated_at'
           AND NOT tgisinternal
    ) INTO has_timestamp_trigger;

    IF has_timestamp_trigger THEN
        EXECUTE 'ALTER TABLE knowledge.discoveries '
                'DISABLE TRIGGER trg_knowledge_discoveries_updated_at';
    END IF;

    -- Setting NULL lets the BEFORE trigger recompute; it never persists.
    UPDATE knowledge.discoveries SET search_vector = NULL;

    IF has_timestamp_trigger THEN
        EXECUTE 'ALTER TABLE knowledge.discoveries '
                'ENABLE TRIGGER trg_knowledge_discoveries_updated_at';
    END IF;
END;
$$;

DO $$
DECLARE
    unreachable bigint;
    sample text;
BEGIN
    -- Postcondition: every alphabetic segment of every slash-joined span must
    -- be reachable by a lexical query for that segment alone. Two exclusions,
    -- both query-side artifacts rather than index gaps:
    --   * segments reducing to an empty tsquery (pure stopwords);
    --   * segments not starting alphanumeric -- a leading '-' is websearch_to_
    --     tsquery's NEGATION operator, so probing `-wal` (from the literal
    --     `anima.db-shm/-wal`) asks for rows WITHOUT 'wal'. `wal` itself is
    --     indexed and reachable.
    SELECT count(*), min(seg.id)
      INTO unreachable, sample
      FROM (
        SELECT d.id, s.segment
          FROM knowledge.discoveries AS d
          CROSS JOIN LATERAL regexp_matches(
              coalesce(d.summary, '') || ' ' || coalesce(d.details, ''),
              '[A-Za-z0-9_.@+-]+(?:/[A-Za-z0-9_.@+-]+)+',
              'g'
          ) AS m
          CROSS JOIN LATERAL unnest(string_to_array(m[1], '/')) AS s(segment)
      ) AS seg
      JOIN knowledge.discoveries AS d2 ON d2.id = seg.id
     WHERE seg.segment ~ '^[A-Za-z0-9]'
       AND seg.segment ~ '[A-Za-z]'
       AND websearch_to_tsquery('english', seg.segment) <> ''::tsquery
       AND NOT (d2.search_vector @@ websearch_to_tsquery('english', seg.segment));

    IF unreachable > 0 THEN
        RAISE EXCEPTION
            'path-token decomposition incomplete: % segment(s) still unreachable (e.g. discovery %)',
            unreachable, sample;
    END IF;

    IF EXISTS (SELECT 1 FROM knowledge.discoveries WHERE search_vector IS NULL) THEN
        RAISE EXCEPTION 'backfill left null search_vector rows';
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_attribute
         WHERE attrelid = 'knowledge.discoveries'::regclass
           AND attname = 'search_vector'
           AND attgenerated <> ''
    ) THEN
        RAISE EXCEPTION 'search_vector is still a generated column; the trigger cannot maintain it';
    END IF;
END;
$$;

INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (65, 'knowledge_search_path_token_decomposition', NOW())
ON CONFLICT (version) DO NOTHING;

COMMIT;
