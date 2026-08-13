-- 062_schema_migration_checksums.sql
--
-- Gives core.schema_migrations a CONTENT anchor. Until now the registry stored
-- only (version, name, applied_at), so a registered version was a claim about
-- *which* migration ran and never about *what* ran. Nothing in the system could
-- tell the difference between "version 34 applied" and "version 34 applied from
-- a working tree that did not yet contain the file's final content".
--
-- That is not hypothetical. Migration 034 registered at 2026-05-03T21:28:22Z,
-- but 37bdf999 — the only commit that has ever touched 034 — landed 74 minutes
-- LATER. The fourth of its four CHECK constraints was written into the file
-- after the apply and before the commit, so prod silently ran a 3-of-4 version
-- of that migration for three months. apply_migrations.py could not notice: it
-- plans by registered version, never by file content, so version 34 has been
-- "done" ever since. See 061_lease_plane_sensor_status_check_repair.sql.
--
-- The repair in 061 closes that one instance. This closes the CLASS: with a
-- checksum recorded at apply time, a file that changes after it was applied
-- stops being invisible and becomes a hard failure in both apply_migrations.py
-- and the doctor.
--
-- DELIBERATELY NOT BACK-FILLED
-- ---------------------------
-- The obvious move is to hash the 60 existing files and write those in. That
-- would be a lie, and precisely the lie this column exists to prevent: for a
-- row applied before this migration, the content that actually ran is
-- unknowable. Hashing today's file asserts "the applied content matched this
-- file" — the exact false-green that let 034 hide.
--
-- So pre-existing rows keep checksum IS NULL, which reads as "applied before
-- content anchoring; unverifiable". Had this column existed in May, 034 would
-- have sat as unverifiable rather than as a clean row. Unverifiable is the
-- honest state, and it is the one that prompts a look.
--
-- The NULL population is therefore expected to shrink only as migrations are
-- legitimately re-applied against fresh databases, never by a bulk update. Any
-- future change that back-fills these from source files re-opens the hole.

BEGIN;

ALTER TABLE core.schema_migrations
    ADD COLUMN IF NOT EXISTS checksum text;

COMMENT ON COLUMN core.schema_migrations.checksum IS
    'sha256 (hex) of the migration file as applied. NULL = applied before '
    'content anchoring (migration 062); unverifiable, never back-fill from '
    'source — see the header of 062_schema_migration_checksums.sql.';

-- Shape guard. A checksum is a 64-char lowercase hex sha256 or nothing at all;
-- this stops a truncated / uppercase / "unknown" sentinel from being written in
-- and later compared as if it were a real digest.
DO $$ BEGIN
    ALTER TABLE core.schema_migrations
        ADD CONSTRAINT schema_migrations_checksum_is_sha256
        CHECK (checksum IS NULL OR checksum ~ '^[0-9a-f]{64}$');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- Post-condition, same discipline as 061: this file refuses to register itself
-- unless the change it claims to make is actually present. A migration that
-- reports success without making its change is the failure being repaired here,
-- so it must fail loudly rather than exit clean.
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'core'
          AND table_name = 'schema_migrations'
          AND column_name = 'checksum'
    ) THEN
        RAISE EXCEPTION
            'core.schema_migrations.checksum absent after ADD COLUMN — '
            'migration 062 refuses to register a change it did not make';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'core.schema_migrations'::regclass
          AND contype = 'c'
          AND conname = 'schema_migrations_checksum_is_sha256'
    ) THEN
        RAISE EXCEPTION
            'schema_migrations_checksum_is_sha256 absent after ADD CONSTRAINT — '
            'migration 062 refuses to register a change it did not make';
    END IF;
END $$;

-- Register migration. Its own checksum is written by apply_migrations.py after
-- this file runs — a file cannot contain its own hash.
INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (62, 'schema_migration_checksums', NOW())
ON CONFLICT (version) DO NOTHING;

COMMIT;
