# PostgreSQL + Apache AGE Setup

This directory contains the schema, migrations, and setup files for the PostgreSQL backend with Apache AGE available for graph-specific traversal work.

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
# Re-embed the discovery corpus into the active pgvector table
python scripts/migration/reembed_corpus.py --dry-run
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
SELECT version, name, applied_at, checksum FROM core.schema_migrations ORDER BY version;
```

### Content anchoring (migration 062)

A registered version says *which* migration ran. `checksum` — sha256 of the file
as applied — says *what* ran. The two came apart once already: 034 registered at
`2026-05-03T21:28:22Z` while the commit finishing its file landed 74 minutes
later, so production ran a 3-of-4-constraint version of that migration for three
months and nothing could see it, because `apply_migrations.py` plans by
registered version and never re-reads an applied file.

With a checksum recorded, a file edited after its apply is a hard failure in both
`apply_migrations.py` (refuses to apply anything further) and the doctor's
`migration_checksum_drift` check.

**`checksum IS NULL` means unverifiable, not OK.** Rows applied before 062 have
no anchor and what actually ran is unknowable. Never back-fill them from the
current source files: that asserts the applied content matched the file, which is
the precise false-green being eliminated. The NULL count falls only when
migrations are legitimately applied to a fresh database.

### Attestation

```bash
python3 scripts/dev/unitares_doctor.py --attest --db-url "$DB_POSTGRES_URL"
```

Reduces the registry to a digest over the ordered `version:name:checksum` chain,
plus its coverage. Two deployments compare digests to establish that they carry
the same schema contract — each computes over its own state, neither consults a
registry, and neither answer is authoritative over the other.

Read `fully_anchored` before treating equal digests as agreement. Two databases
whose rows are all unverifiable can produce matching digests while having run
different SQL — the coverage fields are what keep the digest an honest claim
rather than a stronger one than the data supports.

**What it is not.** The digest is self-computed and unsigned. It detects
*accidental* divergence between cooperating peers — the failure mode that
actually happens, and the one that produced 034 — and it is not evidence against
a peer that misreports its own state. Nothing binds it to anything unforgeable.
And an all-NULL registry is cheap to match, because the chain then reduces to
`version:name` pairs anyone can reproduce, which is why a partially-anchored
match is close to no evidence at all.

The table below is a milestone summary, not the full migration ledger. See `db/postgres/migrations/` for every numbered migration; the current checked-in series runs through `036_r2_lineage_lifecycle.sql`.

| Version | Migration | Description |
|---------|-----------|-------------|
| 1 | `initial_schema` | Core tables (agents, sessions, dialectic) |
| 2 | `knowledge_schema` | Knowledge graph tables for PostgreSQL FTS |
| 3 | `dialectic_messages` | Dialectic messages table (migrated from SQLite) |
| 15 | `agent_process_bindings` | Concurrent identity binding invariant (#123): `core.agent_process_bindings` + `allow_rebind_after_exit` / `allow_concurrent_contexts` flags on `core.agents` |
| 24 | `lease_plane` | Surface lease-plane contract anchor |
| 31 | `r1_provisional_lineage` | R1 provisional-lineage columns and audit table |
| 35 | `coordination_events` | Wave 0 coordination-event instrumentation |
| 36 | `r2_lineage_lifecycle` | R2 lineage lifecycle columns |

The health check returns `schema_version` from this table.

## Health Check Status

The `/health_check` tool returns a three-tier aggregate status:

| Status | Condition |
|--------|-----------|
| `healthy` | All components report healthy |
| `moderate` | Some components have warnings/deprecated status, but no errors |
| `critical` | One or more components report error |

The response includes a `status_breakdown` field showing counts per status type.

## Current Storage Posture

1. PostgreSQL is the canonical runtime store for agents, identities, sessions, dialectic, audit events, outcome events, and resident/coordination telemetry.
2. Knowledge graph reads/writes default to PostgreSQL FTS (`UNITARES_KNOWLEDGE_BACKEND=postgres` or `auto` with `DB_BACKEND=postgres`).
3. Apache AGE remains available for explicit graph traversal experiments; it is not the default KG read path.
4. SQLite/JSON files under `data/` are legacy or local runtime artifacts, not the source of truth for current production state.

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
