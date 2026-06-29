-- Migration 033: Add raw_verdict + verdict columns to audit.r1_score_audit
--
-- PR 3 council (architect) found that audit.r1_score_audit (created in
-- migration 031) has neither `verdict` nor `raw_verdict` — yet R1 v3.3-A
-- audit-only persistence and §"Consumer behavior under calibration_failed"
-- jointly require both: the score record must "retain the original verdict
-- for forensic access" while the consumer-facing primitive returns the
-- degraded one (per v3.3-C).
--
-- Without these columns:
--   - reconstructing the verdict from `plausibility + parent_mature +
--     n_dims_used` is lossy when threshold cuts move (which they will, per
--     shadow-mode calibration);
--   - `calibration_failed` degradation is only legible from the `reasons`
--     text array, not as a structured anchor.
--
-- Both columns NULLable for backwards compatibility with the small number of
-- audit rows that landed before this migration (the verifier's PR 2 +
-- PR 3 test rows were all cleaned up; this is purely defensive). Production
-- writes from `score_trajectory_continuity` always populate both.

ALTER TABLE audit.r1_score_audit
    ADD COLUMN IF NOT EXISTS raw_verdict TEXT NULL
        CHECK (raw_verdict IS NULL OR raw_verdict IN ('plausible', 'inconclusive', 'unsupported')),
    ADD COLUMN IF NOT EXISTS verdict     TEXT NULL
        CHECK (verdict IS NULL OR verdict IN ('plausible', 'inconclusive', 'unsupported'));

COMMENT ON COLUMN audit.r1_score_audit.raw_verdict IS 'R1 v3.3-A/v3.3-C: pre-degradation verdict from threshold cuts; preserved for forensic access when calibration_failed degrades the consumer-facing verdict to inconclusive';
COMMENT ON COLUMN audit.r1_score_audit.verdict     IS 'R1 v3.3-A: consumer-facing verdict (potentially degraded under calibration_failed per v3.3-C)';

-- Register migration
INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (33, 'r1_score_audit_verdict_cols', NOW())
ON CONFLICT (version) DO NOTHING;
