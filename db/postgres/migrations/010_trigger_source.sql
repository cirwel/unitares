-- Add trigger_source to dialectic_sessions for provenance tracking
-- Values: "manual", "circuit_breaker", "loop_detection", "drift_detection"
ALTER TABLE core.dialectic_sessions ADD COLUMN IF NOT EXISTS trigger_source TEXT;

INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (10, 'trigger_source', NOW())
ON CONFLICT (version) DO NOTHING;
