-- Migration 032: R1 calibration_state singleton
--
-- Per docs/ontology/r1-verify-lineage-claim.md §v3.3-C: every score record
-- snapshots calibration_status at write time. This singleton holds the
-- *current* status + lifecycle timestamps, read on each scoring call and
-- written by explicit operator transitions.
--
-- Three states (CHECK constraint mirrors audit.r1_score_audit):
--   seeded — synthetic-fixture-calibrated thresholds (default at first ship)
--   earned — operator ran calibration analysis; cuts validated
--   calibration_failed — distributions did not separate; verdict suppressed
--
-- Singleton enforced via PRIMARY KEY (id) + CHECK (id = 1) so any INSERT
-- attempt with id != 1 fails, and the existing row is the only one consumers
-- ever read.

CREATE TABLE IF NOT EXISTS core.r1_calibration_state (
    id                  INTEGER     PRIMARY KEY DEFAULT 1
                        CHECK (id = 1),
    calibration_status  TEXT        NOT NULL DEFAULT 'seeded'
                        CHECK (calibration_status IN ('seeded', 'earned', 'calibration_failed')),
    seeded_since        TIMESTAMPTZ NOT NULL DEFAULT now(),
    earned_at           TIMESTAMPTZ NULL,
    failed_at           TIMESTAMPTZ NULL,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE  core.r1_calibration_state IS 'R1 v3.3-C: singleton holding the current calibration lifecycle status; consumers under calibration_failed degrade verdict to inconclusive';
COMMENT ON COLUMN core.r1_calibration_state.calibration_status IS 'R1 v3.3-C: current state — seeded (default at ship), earned (cuts validated), calibration_failed (distributions did not separate)';
COMMENT ON COLUMN core.r1_calibration_state.seeded_since      IS 'R1 v3.3-C: stamped at first ship of the primitive; surfaced by operator runbook for stale-seeded visibility';
COMMENT ON COLUMN core.r1_calibration_state.earned_at         IS 'R1 v3.3-C: stamped on seeded → earned transition';
COMMENT ON COLUMN core.r1_calibration_state.failed_at         IS 'R1 v3.3-C: stamped on seeded → calibration_failed or earned → calibration_failed transition';

-- Seed the singleton row. ON CONFLICT DO NOTHING so re-running the migration
-- on an existing DB doesn't reset timestamps.
INSERT INTO core.r1_calibration_state (id, calibration_status)
VALUES (1, 'seeded')
ON CONFLICT (id) DO NOTHING;

-- Register migration
INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (32, 'r1_calibration_state', NOW())
ON CONFLICT (version) DO NOTHING;
