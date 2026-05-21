-- v7 F-hat Session 1 — reference corpus: outcome events
-- Spec: §2.2 C5 (audit.outcome_events.is_bad)
-- Spec §2.2 time discretization: join outcome nearest to state row t within ±60s on same agent.
--
-- This pull returns the raw outcomes; join-to-nearest-state is done in pandas/pyarrow
-- at fit time (avoids gnarly window-by-agent SQL).

SELECT
    o.ts,
    o.outcome_id,
    o.agent_id,
    o.outcome_type,
    o.outcome_score,
    o.is_bad,
    o.eisv_e,
    o.eisv_i,
    o.eisv_s,
    o.eisv_v,
    o.eisv_phi,
    o.eisv_verdict,
    o.eisv_coherence,
    o.eisv_regime,
    o.detail,
    o.epoch
FROM audit.outcome_events o
WHERE o.ts >= :'window_start'::timestamptz
  AND o.ts <  :'window_end'::timestamptz
ORDER BY o.agent_id, o.ts;
