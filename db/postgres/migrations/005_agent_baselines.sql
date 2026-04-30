-- Migration 005: Agent baselines table for ethical drift signal persistence
-- Allows baselines to survive service restarts instead of resetting to defaults.

CREATE TABLE IF NOT EXISTS core.agent_baselines (
    agent_id            TEXT PRIMARY KEY REFERENCES core.agents(id) ON DELETE CASCADE,
    baseline_coherence  REAL NOT NULL DEFAULT 0.5,
    baseline_confidence REAL NOT NULL DEFAULT 0.6,
    baseline_complexity REAL NOT NULL DEFAULT 0.4,
    prev_coherence      REAL,
    prev_confidence     REAL,
    prev_complexity     REAL,
    recent_decisions    TEXT[] DEFAULT '{}',
    decision_consistency REAL NOT NULL DEFAULT 0.8,
    update_count        INTEGER NOT NULL DEFAULT 0,
    alpha               REAL NOT NULL DEFAULT 0.1,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (5, 'agent_baselines', NOW())
ON CONFLICT (version) DO NOTHING;
