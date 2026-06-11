-- 046_outbox_unforwarded_attempts_index.sql
--
-- Companion to the lease-plane outbox head-of-line-blocking fix: the
-- forwarder's selection query now orders by (forward_attempts ASC, ts ASC)
-- so rows that keep failing sink behind fresh work instead of monopolizing
-- every batch (the 2026-06 partition-gap wedge: 2,199 permanently-failing
-- rows blocked all forwarding for 11 days; see migration 045 for the
-- partition repair itself).
--
-- The existing partial index lease_plane_events_unforwarded btree (ts)
-- WHERE forwarded_at IS NULL stays — monitoring queries (min(ts), counts)
-- use it. This one serves the new selection ordering.

CREATE INDEX IF NOT EXISTS lease_plane_events_unforwarded_attempts
    ON lease_plane.lease_plane_events (forward_attempts, ts)
    WHERE forwarded_at IS NULL;

-- Register migration
INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (46, 'outbox_unforwarded_attempts_index', NOW())
ON CONFLICT (version) DO NOTHING;
