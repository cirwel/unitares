-- Knowledge Graph Schema for PostgreSQL
-- Version: 1.0.0
--
-- Migrates SQLite knowledge graph (discoveries, tags, edges) to PostgreSQL.
-- Uses native FTS (tsvector) instead of FTS5.
-- Integrates with pgvector embeddings (embeddings_schema.sql).
--
-- Run AFTER schema.sql and embeddings_schema.sql

-- =============================================================================
-- KNOWLEDGE SCHEMA
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS knowledge;

-- =============================================================================
-- CORE TABLES
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Discoveries (main knowledge graph nodes)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge.discoveries (
    id                  TEXT PRIMARY KEY,
    agent_id            TEXT NOT NULL,
    type                TEXT NOT NULL,
    -- severity/status/response_type CHECK sets widened by migration 047;
    -- backported here so base DDL is honest. Single-sourced from
    -- src/knowledge_graph.py (see tests/test_knowledge_enum_sync.py).
    severity            TEXT DEFAULT 'low'
                        CHECK (severity IN ('low', 'medium', 'high', 'critical')),
    status              TEXT NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open', 'resolved', 'archived', 'disputed', 'closed', 'wont_fix', 'superseded')),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ,
    resolved_at         TIMESTAMPTZ,

    -- Content
    summary             TEXT NOT NULL,
    details             TEXT,

    -- Relationships (denormalized for common queries)
    tags                TEXT[] DEFAULT '{}',
    references_files    TEXT[] DEFAULT '{}',
    related_to          TEXT[] DEFAULT '{}',

    -- Response chain (dialectic)
    response_to_id      TEXT REFERENCES knowledge.discoveries(id) ON DELETE SET NULL,
    response_type       TEXT CHECK (response_type IN ('extend', 'question', 'disagree', 'support', 'answer', 'follow_up', 'correction', 'elaboration', 'supersedes')),

    -- Confidence/calibration
    confidence          REAL,

    -- Provenance (agent state at creation time)
    provenance          JSONB,
    provenance_chain    JSONB,

    -- Epoch (added by migration 007; backported here so base DDL is honest
    -- under R1 v3.3-F.)
    epoch               INTEGER NOT NULL DEFAULT 1,

    -- Full-text search vector (auto-generated)
    search_vector       TSVECTOR GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(summary, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(details, '')), 'B')
    ) STORED
);

-- Indexes for common query patterns
CREATE INDEX IF NOT EXISTS idx_knowledge_discoveries_agent ON knowledge.discoveries(agent_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_discoveries_type ON knowledge.discoveries(type);
CREATE INDEX IF NOT EXISTS idx_knowledge_discoveries_status ON knowledge.discoveries(status);
CREATE INDEX IF NOT EXISTS idx_knowledge_discoveries_created ON knowledge.discoveries(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_knowledge_discoveries_response_to ON knowledge.discoveries(response_to_id) WHERE response_to_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_discoveries_epoch ON knowledge.discoveries(epoch);

-- GIN indexes for array and FTS
CREATE INDEX IF NOT EXISTS idx_knowledge_discoveries_tags ON knowledge.discoveries USING GIN (tags);
CREATE INDEX IF NOT EXISTS idx_knowledge_discoveries_fts ON knowledge.discoveries USING GIN (search_vector);

-- Trigger for updated_at
CREATE TRIGGER trg_knowledge_discoveries_updated_at
    BEFORE UPDATE ON knowledge.discoveries
    FOR EACH ROW EXECUTE FUNCTION core.update_timestamp();

-- -----------------------------------------------------------------------------
-- Discovery Tags (normalized many-to-many, kept for complex tag queries)
-- NOTE: Primary tag storage is in discoveries.tags[] array for simple cases
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge.discovery_tags (
    discovery_id        TEXT NOT NULL REFERENCES knowledge.discoveries(id) ON DELETE CASCADE,
    tag                 TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (discovery_id, tag)
);

CREATE INDEX IF NOT EXISTS idx_knowledge_tags_tag ON knowledge.discovery_tags(tag);

-- -----------------------------------------------------------------------------
-- Discovery Edges (graph relationships)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge.discovery_edges (
    src_id              TEXT NOT NULL REFERENCES knowledge.discoveries(id) ON DELETE CASCADE,
    dst_id              TEXT NOT NULL REFERENCES knowledge.discoveries(id) ON DELETE CASCADE,
    edge_type           TEXT NOT NULL,
    response_type       TEXT CHECK (response_type IN ('extend', 'question', 'disagree', 'support', 'answer', 'follow_up', 'correction', 'elaboration', 'supersedes')),
    weight              REAL DEFAULT 1.0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by          TEXT,
    metadata            JSONB,
    PRIMARY KEY (src_id, dst_id, edge_type)
);

CREATE INDEX IF NOT EXISTS idx_knowledge_edges_src ON knowledge.discovery_edges(src_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_edges_dst ON knowledge.discovery_edges(dst_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_edges_type ON knowledge.discovery_edges(edge_type);

-- -----------------------------------------------------------------------------
-- Rate Limits (for knowledge graph rate limiting)
-- NOTE: Also exists in audit schema - this is knowledge-specific
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge.rate_limits (
    agent_id            TEXT NOT NULL,
    timestamp           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (agent_id, timestamp)
);

CREATE INDEX IF NOT EXISTS idx_knowledge_rate_limits ON knowledge.rate_limits(agent_id, timestamp);

-- -----------------------------------------------------------------------------
-- Schema Version (for migrations)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge.schema_version (
    version             INTEGER PRIMARY KEY,
    applied_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    description         TEXT
);

INSERT INTO knowledge.schema_version (version, description)
VALUES (1, 'Initial knowledge graph schema')
ON CONFLICT (version) DO NOTHING;

-- =============================================================================
-- HELPER FUNCTIONS
-- =============================================================================

-- Full-text search with ranking
CREATE OR REPLACE FUNCTION knowledge.full_text_search(
    query_text TEXT,
    limit_count INTEGER DEFAULT 20
)
RETURNS TABLE (
    discovery_id TEXT,
    rank REAL,
    summary TEXT,
    agent_id TEXT,
    type TEXT,
    created_at TIMESTAMPTZ
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        d.id,
        ts_rank(d.search_vector, websearch_to_tsquery('english', query_text))::REAL,
        d.summary,
        d.agent_id,
        d.type,
        d.created_at
    FROM knowledge.discoveries d
    WHERE d.search_vector @@ websearch_to_tsquery('english', query_text)
    ORDER BY ts_rank(d.search_vector, websearch_to_tsquery('english', query_text)) DESC
    LIMIT limit_count;
END;
$$ LANGUAGE plpgsql STABLE;

-- Find similar discoveries by tag overlap
CREATE OR REPLACE FUNCTION knowledge.find_similar_by_tags(
    source_discovery_id TEXT,
    limit_count INTEGER DEFAULT 10
)
RETURNS TABLE (
    discovery_id TEXT,
    overlap_count INTEGER,
    summary TEXT,
    agent_id TEXT
) AS $$
DECLARE
    source_tags TEXT[];
BEGIN
    SELECT tags INTO source_tags FROM knowledge.discoveries WHERE id = source_discovery_id;

    IF source_tags IS NULL OR array_length(source_tags, 1) IS NULL THEN
        RETURN;
    END IF;

    RETURN QUERY
    SELECT
        d.id,
        cardinality(ARRAY(SELECT unnest(d.tags) INTERSECT SELECT unnest(source_tags)))::INTEGER,
        d.summary,
        d.agent_id
    FROM knowledge.discoveries d
    WHERE d.id != source_discovery_id
      AND d.tags && source_tags
    ORDER BY cardinality(ARRAY(SELECT unnest(d.tags) INTERSECT SELECT unnest(source_tags))) DESC
    LIMIT limit_count;
END;
$$ LANGUAGE plpgsql STABLE;

-- Get response chain (recursive CTE)
CREATE OR REPLACE FUNCTION knowledge.get_response_chain(
    root_discovery_id TEXT,
    max_depth INTEGER DEFAULT 10
)
RETURNS TABLE (
    discovery_id TEXT,
    depth INTEGER,
    summary TEXT,
    agent_id TEXT,
    response_type TEXT
) AS $$
BEGIN
    RETURN QUERY
    WITH RECURSIVE chain AS (
        -- Base case: root discovery
        SELECT
            d.id,
            0 AS depth,
            d.summary,
            d.agent_id,
            d.response_type
        FROM knowledge.discoveries d
        WHERE d.id = root_discovery_id

        UNION ALL

        -- Recursive case: discoveries that respond to items in chain
        SELECT
            d.id,
            c.depth + 1,
            d.summary,
            d.agent_id,
            d.response_type
        FROM knowledge.discoveries d
        JOIN chain c ON d.response_to_id = c.id
        WHERE c.depth < max_depth
    )
    SELECT * FROM chain
    ORDER BY depth;
END;
$$ LANGUAGE plpgsql STABLE;

-- Find agents with similar interests (tag overlap analysis)
CREATE OR REPLACE FUNCTION knowledge.find_similar_agents(
    source_agent_id TEXT,
    min_overlap INTEGER DEFAULT 2,
    limit_count INTEGER DEFAULT 10
)
RETURNS TABLE (
    agent_id TEXT,
    overlap_count BIGINT,
    shared_tags TEXT[]
) AS $$
BEGIN
    RETURN QUERY
    WITH my_tags AS (
        SELECT DISTINCT unnest(tags) AS tag
        FROM knowledge.discoveries
        WHERE agent_id = source_agent_id
    ),
    other_agent_tags AS (
        SELECT
            d.agent_id,
            unnest(d.tags) AS tag
        FROM knowledge.discoveries d
        WHERE d.agent_id != source_agent_id
    )
    SELECT
        oat.agent_id,
        COUNT(DISTINCT oat.tag)::BIGINT AS overlap_count,
        array_agg(DISTINCT oat.tag) AS shared_tags
    FROM other_agent_tags oat
    JOIN my_tags mt ON oat.tag = mt.tag
    GROUP BY oat.agent_id
    HAVING COUNT(DISTINCT oat.tag) >= min_overlap
    ORDER BY overlap_count DESC
    LIMIT limit_count;
END;
$$ LANGUAGE plpgsql STABLE;

-- Check rate limit (returns TRUE if limit exceeded)
CREATE OR REPLACE FUNCTION knowledge.check_rate_limit(
    p_agent_id TEXT,
    p_limit_per_hour INTEGER DEFAULT 20
)
RETURNS BOOLEAN AS $$
DECLARE
    v_count INTEGER;
BEGIN
    -- Count recent entries
    SELECT COUNT(*) INTO v_count
    FROM knowledge.rate_limits
    WHERE agent_id = p_agent_id
      AND timestamp > now() - INTERVAL '1 hour';

    RETURN v_count >= p_limit_per_hour;
END;
$$ LANGUAGE plpgsql;

-- Record rate limit entry and cleanup old ones
CREATE OR REPLACE FUNCTION knowledge.record_rate_limit(
    p_agent_id TEXT
)
RETURNS VOID AS $$
BEGIN
    -- Insert new entry
    INSERT INTO knowledge.rate_limits (agent_id, timestamp)
    VALUES (p_agent_id, now());

    -- Cleanup entries older than 1 hour (opportunistic)
    DELETE FROM knowledge.rate_limits
    WHERE timestamp < now() - INTERVAL '1 hour';
END;
$$ LANGUAGE plpgsql;

-- Get knowledge graph statistics
CREATE OR REPLACE FUNCTION knowledge.get_stats()
RETURNS TABLE (
    total_discoveries BIGINT,
    by_agent JSONB,
    by_type JSONB,
    by_status JSONB,
    total_tags BIGINT,
    total_agents BIGINT,
    total_edges BIGINT
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        (SELECT COUNT(*) FROM knowledge.discoveries)::BIGINT,
        (SELECT jsonb_object_agg(agent_id, cnt) FROM (
            SELECT agent_id, COUNT(*) AS cnt
            FROM knowledge.discoveries
            GROUP BY agent_id
        ) sub),
        (SELECT jsonb_object_agg(type, cnt) FROM (
            SELECT type, COUNT(*) AS cnt
            FROM knowledge.discoveries
            GROUP BY type
        ) sub),
        (SELECT jsonb_object_agg(status, cnt) FROM (
            SELECT status, COUNT(*) AS cnt
            FROM knowledge.discoveries
            GROUP BY status
        ) sub),
        (SELECT COUNT(DISTINCT tag) FROM knowledge.discovery_tags)::BIGINT,
        (SELECT COUNT(DISTINCT agent_id) FROM knowledge.discoveries)::BIGINT,
        (SELECT COUNT(*) FROM knowledge.discovery_edges)::BIGINT;
END;
$$ LANGUAGE plpgsql STABLE;

-- =============================================================================
-- COMMENTS
-- =============================================================================

COMMENT ON SCHEMA knowledge IS 'Knowledge graph storage for agent discoveries';
COMMENT ON TABLE knowledge.discoveries IS 'Main knowledge graph nodes - insights, questions, observations from agents';
COMMENT ON TABLE knowledge.discovery_edges IS 'Graph edges linking discoveries (response_to, related_to, disputes, etc)';
COMMENT ON FUNCTION knowledge.full_text_search IS 'Search discoveries using PostgreSQL FTS with websearch syntax';
COMMENT ON FUNCTION knowledge.get_response_chain IS 'Get dialectic response chain using recursive CTE';
