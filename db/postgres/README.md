# PostgreSQL + Apache AGE Setup

This directory contains the schema and setup files for migrating to PostgreSQL with Apache AGE extension.

## Files

- `schema.sql` - PostgreSQL relational schema (agents, sessions, dialectic, etc.)
- `knowledge_schema.sql` - Knowledge graph relational tables (PostgreSQL FTS fallback)
- `graph_schema.cypher` - AGE graph schema documentation and setup
- `embeddings_schema.sql` - pgvector embeddings for semantic search
- `partitions.sql` - (Optional) Partition management for audit tables
- `migrations/` - Schema versioning migrations

## Setup Instructions

### PostgreSQL 17 on macOS

#### 1. Install PostgreSQL 17 and pgvector

```bash
brew install postgresql@17 pgvector
brew services start postgresql@17
```

If your Homebrew instance is not listening on `5432`, either reconfigure it or set `DB_POSTGRES_URL` to the actual host/port.

#### 2. Build/install Apache AGE against the same PostgreSQL 17

```bash
export PG_CONFIG=/opt/homebrew/opt/postgresql@17/bin/pg_config
git clone https://github.com/apache/age.git
cd age
make PG_CONFIG="$PG_CONFIG"
make install PG_CONFIG="$PG_CONFIG"
```

#### 3. Create database, extensions, relational schema, and graph

```bash
createdb -h localhost -p 5432 -U postgres governance

export DB_POSTGRES_URL=postgresql://postgres:postgres@localhost:5432/governance
export DB_AGE_GRAPH=governance_graph

psql "$DB_POSTGRES_URL" -f db/postgres/init-extensions.sql
psql "$DB_POSTGRES_URL" -f db/postgres/schema.sql
psql "$DB_POSTGRES_URL" -f db/postgres/partitions.sql
psql "$DB_POSTGRES_URL" -f db/postgres/graph_schema.sql
```

### Configure Environment

```bash
export DB_BACKEND=postgres
export DB_POSTGRES_URL=postgresql://postgres:postgres@localhost:5432/governance
export DB_AGE_GRAPH=governance_graph
```

### 4.1 Graph name convention (important)

This repo standardizes on the AGE graph name **`governance_graph`**.

- **Why**: the Postgres backend uses `DB_AGE_GRAPH` (defaulting to `governance_graph`) when calling `cypher(...)`.
- **Rule**: if you create a different graph name locally, set `DB_AGE_GRAPH` accordingly or graph queries will fail.

### 4.2 Knowledge Graph Backend Selection

The main runtime DB backend is controlled by `DB_BACKEND`, but the **knowledge graph** also supports backend override via `UNITARES_KNOWLEDGE_BACKEND`:

| Backend | Value | Description |
|---------|-------|-------------|
| **PostgreSQL FTS** (recommended) | `postgres` | Canonical KG store using native PostgreSQL with tsvector full-text search |
| **AGE** | `age` | Apache AGE graph backend for graph-specific traversal experiments |
| **Auto** (default) | `auto` | Uses PostgreSQL FTS when `DB_BACKEND=postgres` |

```bash
# Recommended: PostgreSQL FTS canonical store
export UNITARES_KNOWLEDGE_BACKEND=postgres

# Auto-select based on DB_BACKEND setting
export UNITARES_KNOWLEDGE_BACKEND=auto

# Optional: AGE graph backend for graph-specific traversal work
export UNITARES_KNOWLEDGE_BACKEND=age
```

**Note:** When `UNITARES_KNOWLEDGE_BACKEND=auto` (default), the system will:
1. Use PostgreSQL FTS if `DB_BACKEND=postgres`
2. Otherwise require an explicit supported backend

### Migrations / Backfills

The repo no longer has a single monolithic `migrate_to_postgres_age.py` entrypoint.
Current maintenance utilities are targeted scripts under `scripts/migration/` and
`scripts/age/`.

Example:

```bash
# Backfill missing pgvector embeddings
python scripts/migration/backfill_embeddings.py --dry-run
```

## Schema Overview

### Relational Tables (core schema)

- `core.agents` - Agent identity and metadata
- `core.agent_sessions` - Session bindings (fast lookup)
- `core.dialectic_sessions` - Dialectic recovery sessions
- `core.dialectic_messages` - Dialectic session messages (thesis/antithesis/synthesis)
- `core.identities` - (Legacy) Identity records for backward compatibility
- `core.schema_migrations` - Schema version tracking

### Knowledge Schema (knowledge schema)

When using PostgreSQL FTS backend (`UNITARES_KNOWLEDGE_BACKEND=postgres`):

- `knowledge.discoveries` - Knowledge discoveries with native tsvector FTS
- `knowledge.discovery_tags` - Normalized tag storage
- `knowledge.discovery_edges` - Graph-like edges (related_to, response_to)

### Graph (AGE)

- **Nodes:**
  - `:Discovery` - Knowledge discoveries (insights, questions, self_observations)
  - `:Agent` - Agent nodes (mirror of relational table)
  - `:Tag` - Tag nodes for efficient traversal

- **Edges:**
  - `:AUTHORED` - (Agent)-[:AUTHORED]->(Discovery)
  - `:RESPONDS_TO` - (Discovery)-[:RESPONDS_TO]->(Discovery)
  - `:RELATED_TO` - (Discovery)-[:RELATED_TO]->(Discovery)
  - `:TAGGED` - (Discovery)-[:TAGGED]->(Tag)
  - `:TEMPORALLY_NEAR` - (Discovery)-[:TEMPORALLY_NEAR]->(Discovery)

## Example Queries

See `db/postgres/graph_schema.cypher` for example Cypher queries.

## Sanity checks (quick validation)

After running the schema, these checks catch 90% of setup mistakes:

```bash
# 1) Confirm Postgres connectivity
psql "$DB_POSTGRES_URL" -c "SELECT 1"

# 2) Confirm AGE extension exists
psql "$DB_POSTGRES_URL" -c "SELECT name, installed_version FROM pg_available_extensions WHERE name='age'"

# 2b) Confirm pgvector exists
psql "$DB_POSTGRES_URL" -c "SELECT extname, extversion FROM pg_extension WHERE extname IN ('age', 'vector') ORDER BY extname"

# 3) Confirm the graph exists
psql "$DB_POSTGRES_URL" -c "SELECT graphid, name FROM ag_catalog.ag_graph WHERE name='governance_graph'"
```

## Schema Versioning

Schema versions are tracked in `core.schema_migrations`:

```sql
SELECT version, name, applied_at FROM core.schema_migrations ORDER BY version;
```

| Version | Migration | Description |
|---------|-----------|-------------|
| 1 | `initial_schema` | Core tables (agents, sessions, dialectic) |
| 2 | `knowledge_schema` | Knowledge graph tables for PostgreSQL FTS |
| 3 | `dialectic_messages` | Dialectic messages table (migrated from SQLite) |
| 15 | `agent_process_bindings` | Concurrent identity binding invariant (#123): `core.agent_process_bindings` + `allow_rebind_after_exit` / `allow_concurrent_contexts` flags on `core.agents` |

The health check returns `schema_version` from this table.

## Health Check Status

The `/health_check` tool returns a three-tier aggregate status:

| Status | Condition |
|--------|-----------|
| `healthy` | All components report healthy |
| `moderate` | Some components have warnings/deprecated status, but no errors |
| `critical` | One or more components report error |

The response includes a `status_breakdown` field showing counts per status type.

## Migration Phases

1. **Phase 1**: PostgreSQL tables for agents and sessions
2. **Phase 2**: Install AGE, create graph, dual-write discoveries
3. **Phase 3**: Backfill historical discoveries to graph
4. **Phase 4**: Cut over reads to AGE
5. **Phase 5**: Remove JSON/SQLite knowledge graph paths
6. **Phase 6**: Migrate dialectic sessions/messages to PostgreSQL (current state)

## Troubleshooting

### AGE query errors / “cypher function not found”

- Ensure the extension is installed and loaded:
  - `CREATE EXTENSION IF NOT EXISTS age;`
- In some setups you may need to load AGE per-session:
  - `LOAD 'age';`
  - `SET search_path = ag_catalog, "$user", public;`

### AGE Extension Not Found

```sql
-- Check if AGE is installed
SELECT * FROM pg_available_extensions WHERE name = 'age';

-- If not installed, follow AGE installation guide
```

### Graph Already Exists

```sql
-- Drop and recreate (WARNING: deletes all graph data)
SELECT * FROM ag_catalog.drop_graph('governance_graph', true);
SELECT * FROM ag_catalog.create_graph('governance_graph');
```

### Connection Issues

```bash
# Test connection
psql "$DB_POSTGRES_URL" -c "SELECT 1"

# Check pool settings
export DB_POSTGRES_MIN_CONN=2
export DB_POSTGRES_MAX_CONN=10
```

### Common pitfalls

- **Graph name mismatch**: your graph is not `governance_graph` but `DB_AGE_GRAPH` wasn’t updated.
- **Extension not enabled in the DB**: you installed AGE on the host but didn’t run `CREATE EXTENSION age;` inside the target database.
- **pgvector missing**: the relational schema creates `core.discovery_embeddings`, so `CREATE EXTENSION vector;` must succeed too.
- **Running graph/data migration before schema**: apply `db/postgres/schema.sql` before running any AGE backfill or migration tooling.
