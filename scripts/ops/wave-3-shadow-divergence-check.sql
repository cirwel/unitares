-- wave-3-shadow-divergence-check.sql — Wave 3 §8.2 comparator.
--
-- Full outer join of canonical vs shadow for the two PATH-3 tables, all live
-- columns, three divergence kinds (canonical_missing / shadow_missing /
-- column mismatch). Run hourly by scripts/ops/wave3_shadow_divergence_check.py
-- (launchd: com.unitares.wave3-shadow-divergence-check), which emits one
-- coordination_failure.beam_python_boundary.shadow_divergence event per row.
--
-- Column-set rationale (live-schema verified 2026-06-10 against the
-- governance DB; §15 live-verifier lane should re-verify on review):
--   * identities: compares every live column EXCEPT
--       - identity_id   (serial PK; canonical and shadow draw the same
--                        sequence but the shadow writer copies values —
--                        compared implicitly via the agent_id join)
--       - created_at / updated_at (writer-local timestamps; legitimate skew)
--       - metadata_tsv  (generated from metadata; equal iff metadata equal)
--   * agents: compares every live column EXCEPT created_at / updated_at
--     (same writer-local-timestamp rationale).
-- The join keys are unique on the canonical side (identities_agent_id_key
-- UNIQUE; agents_pkey) and copied to the shadows by LIKE ... INCLUDING ALL.
--
-- Statements are separated by top-level semicolons only (no procedural
-- bodies, no semicolons inside string literals), so the runner strips
-- comment lines and splits on semicolons.

-- core.identities divergence
WITH ident_compare AS (
    SELECT
        COALESCE(c.agent_id, s.agent_id)                              AS agent_id,
        c.agent_id IS NULL                                             AS canonical_missing,
        s.agent_id IS NULL                                             AS shadow_missing,
        (c.api_key_hash             IS DISTINCT FROM s.api_key_hash)             AS api_key_hash_diff,
        (c.status                   IS DISTINCT FROM s.status)                   AS status_diff,
        (c.parent_agent_id          IS DISTINCT FROM s.parent_agent_id)          AS parent_agent_id_diff,
        (c.spawn_reason             IS DISTINCT FROM s.spawn_reason)             AS spawn_reason_diff,
        (c.metadata                 IS DISTINCT FROM s.metadata)                 AS metadata_diff,
        (c.disabled_at              IS DISTINCT FROM s.disabled_at)              AS disabled_at_diff,
        (c.last_activity_at         IS DISTINCT FROM s.last_activity_at)         AS last_activity_at_diff,
        (c.provisional_lineage      IS DISTINCT FROM s.provisional_lineage)      AS provisional_diff,
        (c.provisional_score_id     IS DISTINCT FROM s.provisional_score_id)     AS provisional_score_id_diff,
        (c.provisional_recorded_at  IS DISTINCT FROM s.provisional_recorded_at)  AS provisional_recorded_diff,
        (c.confirmed_at             IS DISTINCT FROM s.confirmed_at)             AS confirmed_diff,
        (c.lineage_declared_at      IS DISTINCT FROM s.lineage_declared_at)      AS lineage_declared_diff,
        (c.lineage_demoted_at       IS DISTINCT FROM s.lineage_demoted_at)       AS lineage_demoted_diff,
        (c.lineage_last_eval_at     IS DISTINCT FROM s.lineage_last_eval_at)     AS lineage_last_eval_diff,
        (c.chain_obs_count          IS DISTINCT FROM s.chain_obs_count)          AS chain_obs_count_diff,
        (c.lineage_archived_at      IS DISTINCT FROM s.lineage_archived_at)      AS lineage_archived_diff
    FROM core.identities c
    FULL OUTER JOIN core.identities_shadow s USING (agent_id)
)
SELECT 'identities' AS table_name, agent_id, canonical_missing, shadow_missing,
       api_key_hash_diff, status_diff, parent_agent_id_diff, spawn_reason_diff,
       metadata_diff, disabled_at_diff, last_activity_at_diff,
       provisional_diff, provisional_score_id_diff, provisional_recorded_diff, confirmed_diff,
       lineage_declared_diff, lineage_demoted_diff, lineage_last_eval_diff, chain_obs_count_diff,
       lineage_archived_diff
FROM ident_compare
WHERE canonical_missing OR shadow_missing
   OR api_key_hash_diff OR status_diff OR parent_agent_id_diff
   OR spawn_reason_diff OR metadata_diff
   OR disabled_at_diff OR last_activity_at_diff
   OR provisional_diff OR provisional_score_id_diff OR provisional_recorded_diff OR confirmed_diff
   OR lineage_declared_diff OR lineage_demoted_diff OR lineage_last_eval_diff OR chain_obs_count_diff
   OR lineage_archived_diff;

-- core.agents divergence
WITH agent_compare AS (
    SELECT
        COALESCE(c.id, s.id)                                          AS agent_id,
        c.id IS NULL                                                   AS canonical_missing,
        s.id IS NULL                                                   AS shadow_missing,
        (c.api_key                  IS DISTINCT FROM s.api_key)                  AS api_key_diff,
        (c.status                   IS DISTINCT FROM s.status)                   AS status_diff,
        (c.parent_agent_id          IS DISTINCT FROM s.parent_agent_id)          AS parent_agent_id_diff,
        (c.label                    IS DISTINCT FROM s.label)                    AS label_diff,
        (c.purpose                  IS DISTINCT FROM s.purpose)                  AS purpose_diff,
        (c.notes                    IS DISTINCT FROM s.notes)                    AS notes_diff,
        (c.tags                     IS DISTINCT FROM s.tags)                     AS tags_diff,
        (c.archived_at              IS DISTINCT FROM s.archived_at)              AS archived_at_diff,
        (c.spawn_reason             IS DISTINCT FROM s.spawn_reason)             AS spawn_reason_diff,
        (c.thread_id                IS DISTINCT FROM s.thread_id)                AS thread_id_diff,
        (c.thread_position          IS DISTINCT FROM s.thread_position)          AS thread_position_diff,
        (c.allow_rebind_after_exit  IS DISTINCT FROM s.allow_rebind_after_exit)  AS allow_rebind_diff,
        (c.allow_concurrent_contexts IS DISTINCT FROM s.allow_concurrent_contexts) AS allow_concurrent_diff
    FROM core.agents c
    FULL OUTER JOIN core.agents_shadow s USING (id)
)
SELECT 'agents' AS table_name, agent_id, canonical_missing, shadow_missing,
       api_key_diff, status_diff, parent_agent_id_diff, label_diff, purpose_diff,
       notes_diff, tags_diff, archived_at_diff, spawn_reason_diff, thread_id_diff, thread_position_diff,
       allow_rebind_diff, allow_concurrent_diff
FROM agent_compare
WHERE canonical_missing OR shadow_missing
   OR api_key_diff OR status_diff OR parent_agent_id_diff OR label_diff
   OR purpose_diff OR notes_diff OR tags_diff OR archived_at_diff
   OR spawn_reason_diff OR thread_id_diff OR thread_position_diff
   OR allow_rebind_diff OR allow_concurrent_diff;
