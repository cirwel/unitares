-- v7 F-hat Session 1 — reference corpus: state rows
-- Spec: §2.2 (core.agent_state) + §2.4 (class partition)
-- Frozen by Session 1a 2026-04-23 as the binding pull for Session 1 fit.
--
-- NOTE — spec §2.5 said "epoch-2" but the reference window 2026-02-20 through
-- 2026-03-20 is entirely epoch 1 in the production DB (epoch 2 started 2026-04-01).
-- The spec's epoch-2 phrase was a forward-looking error. This SQL drops the
-- epoch filter. See data/v7-fhat/session1a-findings.md §2 for the audit.
--
-- Parameters (bind at call time):
--   :window_start = '2026-02-20'
--   :window_end   = '2026-03-20'  (exclusive)

SELECT
    s.state_id,
    s.identity_id,
    i.agent_id,
    a.tags,
    s.recorded_at,
    -- Direct columns (spec §2.2 footnote):
    s.entropy    AS observed_S,
    s.integrity  AS observed_I,
    s.volatility AS observed_V,
    s.coherence,
    s.regime,
    s.epoch,
    -- From state_json (spec §2.2):
    (s.state_json->>'E')::double precision          AS observed_E,
    (s.state_json->>'phi')::double precision        AS phi,
    s.state_json->>'verdict'                        AS verdict,
    (s.state_json->>'risk_score')::double precision AS risk_score,
    -- Class partition (spec §2.4 v4):
    --   resident_persistent: 'persistent' IN tags
    --   session_or_unlabeled: everything else
    (a.tags IS NOT NULL AND 'persistent' = ANY(a.tags)) AS is_resident_persistent
FROM core.agent_state s
JOIN core.identities i ON s.identity_id = i.identity_id
LEFT JOIN core.agents a ON a.id = i.agent_id
WHERE s.recorded_at >= :'window_start'::timestamptz
  AND s.recorded_at <  :'window_end'::timestamptz
  AND i.status NOT IN ('archived', 'deleted')
ORDER BY i.agent_id, s.recorded_at;
