-- 047_knowledge_check_constraints_widen.sql
--
-- The knowledge handlers accept 9 response_type values, 7 discovery
-- statuses, and 4 severities, but the base DDL
-- (db/postgres/knowledge_schema.sql) still pins the original 4 response
-- types, 3 statuses, and 3 severities in CHECK constraints. Writes with the
-- newer enum values violate the CHECK on any database built from the base
-- DDL: the supersede action sets status='superseded'
-- (src/mcp_handlers/knowledge/handlers.py), response_to accepts
-- 'answer'/'follow_up'/'correction'/'elaboration'/'supersedes', and store
-- accepts severity='critical'.
--
-- Widen the constraints to the handler-accepted sets. The vocabularies are
-- single-sourced in src/knowledge_graph.py (VALID_RESPONSE_TYPES /
-- VALID_DISCOVERY_STATUSES / VALID_SEVERITIES) and pinned against these SQL
-- files by tests/test_knowledge_enum_sync.py. knowledge_schema.sql is
-- backported in the same commit so base DDL stays honest (same convention
-- as migration 007).

ALTER TABLE knowledge.discoveries DROP CONSTRAINT IF EXISTS discoveries_severity_check;
ALTER TABLE knowledge.discoveries ADD CONSTRAINT discoveries_severity_check
    CHECK (severity IN ('low', 'medium', 'high', 'critical'));

ALTER TABLE knowledge.discoveries DROP CONSTRAINT IF EXISTS discoveries_status_check;
ALTER TABLE knowledge.discoveries ADD CONSTRAINT discoveries_status_check
    CHECK (status IN ('open', 'resolved', 'archived', 'disputed', 'closed', 'wont_fix', 'superseded'));

ALTER TABLE knowledge.discoveries DROP CONSTRAINT IF EXISTS discoveries_response_type_check;
ALTER TABLE knowledge.discoveries ADD CONSTRAINT discoveries_response_type_check
    CHECK (response_type IN ('extend', 'question', 'disagree', 'support', 'answer', 'follow_up', 'correction', 'elaboration', 'supersedes'));

ALTER TABLE knowledge.discovery_edges DROP CONSTRAINT IF EXISTS discovery_edges_response_type_check;
ALTER TABLE knowledge.discovery_edges ADD CONSTRAINT discovery_edges_response_type_check
    CHECK (response_type IN ('extend', 'question', 'disagree', 'support', 'answer', 'follow_up', 'correction', 'elaboration', 'supersedes'));

-- Register migration
INSERT INTO core.schema_migrations (version, name, applied_at)
VALUES (47, 'knowledge_check_constraints_widen', NOW())
ON CONFLICT (version) DO NOTHING;
