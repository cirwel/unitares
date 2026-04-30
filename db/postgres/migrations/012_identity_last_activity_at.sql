-- 012_identity_last_activity_at.sql
--
-- Add the `last_activity_at` timestamp column on core.identities.
--
-- The column is read by src/db/mixins/identity.py (SELECT in get_identity /
-- get_identity_by_id), written by atomic-increment paths during check-ins,
-- and queried in src/mcp_server.py for recently-active filtering. Until
-- this migration the column was missing from schema.sql and silently
-- absent in any database that hadn't been hand-patched, but the integration
-- tests that would have caught it skipped whenever governance_test was not
-- provisioned.
--
-- Nullable because a freshly-onboarded identity has not yet recorded any
-- activity. Backfill leaves NULL — callers already tolerate it
-- (mcp_server.py:681 guards on truthiness).

ALTER TABLE core.identities
    ADD COLUMN IF NOT EXISTS last_activity_at TIMESTAMPTZ NULL;

CREATE INDEX IF NOT EXISTS idx_identities_last_activity_at
    ON core.identities (last_activity_at DESC NULLS LAST);

INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (12, 'identity_last_activity_at', NOW())
ON CONFLICT (version) DO NOTHING;
