-- 048_substrate_observations.sql
--
-- A non-zero check-in FLOOR for un-onboarded sessions.
--
-- Today the plugin Stop hook bails when a session never called onboard
-- (no client_session_id), so those sessions write ZERO governance rows and
-- are invisible. Forcing a floor row through process_agent_update is wrong
-- on two counts confirmed by review: (1) under STRICT_IDENTITY_REQUIRED the
-- call is refused before any row is written; (2) epistemic_class is a
-- forward-only storage label with NO read-gate semantics, so a floor row in
-- core.agent_state would pollute every trust-tier / trajectory / calibration
-- / similarity / matview consumer (only the `synthetic` column excludes, and
-- that is bootstrap-only).
--
-- So the floor lives in its OWN identity-free sink. A row here is a
-- MEASUREMENT keyed to a session slot + transport fingerprint — not a claim
-- that an agent declared an identity. Nothing in the trajectory / trust /
-- calibration / EISV path reads this table; it exists purely to turn the
-- silent 0 into a measured "this session ran but never onboarded" count.
--
-- Deliberately NO foreign key to core.identities: the sink must not couple to
-- the identity table (that coupling is exactly what makes a "measurement"
-- decay into a "claimed identity"). `claimed_by_uuid` is a nullable forward
-- hook so a later onboard can retroactively claim a slot's dark rows; until
-- then the observation stands alone.
--
-- Base DDL (db/postgres/schema.sql) is backported in the same commit so a
-- database built from base stays honest (same convention as migration 047).

CREATE TABLE IF NOT EXISTS core.substrate_observations (
    observation_id   BIGSERIAL PRIMARY KEY,
    slot_key         TEXT        NOT NULL,
    fingerprint      TEXT,
    event            TEXT        NOT NULL DEFAULT 'turn_stop',
    tool_count       INTEGER     NOT NULL DEFAULT 0,
    summary_excerpt  TEXT,
    plugin_version   TEXT,
    claimed_by_uuid  UUID,  -- nullable; set if an onboarded identity later claims this slot. No FK by design: the sink stays identity-table-independent.
    observed_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE core.substrate_observations IS
    'Identity-free check-in floor for un-onboarded sessions. A measurement, not an identity claim. NOT read by any trajectory/trust/calibration/EISV/similarity query.';

-- Distinct-slot coverage and recency reads.
CREATE INDEX IF NOT EXISTS idx_substrate_obs_slot
    ON core.substrate_observations (slot_key);
CREATE INDEX IF NOT EXISTS idx_substrate_obs_observed_at
    ON core.substrate_observations (observed_at DESC);
-- Partial index for the "unclaimed dark sessions" read.
CREATE INDEX IF NOT EXISTS idx_substrate_obs_unclaimed
    ON core.substrate_observations (observed_at DESC)
    WHERE claimed_by_uuid IS NULL;

-- Register migration
INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (48, 'substrate_observations', NOW())
ON CONFLICT (version) DO NOTHING;
