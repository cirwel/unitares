# Database Architecture

Status: thin infrastructure reference. Use this for storage/backing-service facts only. For runtime semantics see [UNIFIED_ARCHITECTURE.md](../UNIFIED_ARCHITECTURE.md); for operational procedures see [OPERATOR_RUNBOOK.md](OPERATOR_RUNBOOK.md).

## Canonical Rule

The canonical Unitares database is whatever instance `DB_POSTGRES_URL` points to. Query that instance directly.

## Current Storage Model

- PostgreSQL is the sole durable database backend
- Apache AGE lives inside PostgreSQL for graph data
- Redis is an optional ephemeral cache for session continuity and fast lookups
- `audit_log.jsonl` is a raw append-only audit artifact, not a second source of truth

## What Lives Where

### PostgreSQL

Stores the durable system of record:

- agent metadata and identity state
- governance state and history
- dialectic sessions
- knowledge graph data
- calibration data
- audit/event records

### Redis

Stores optional ephemeral support data:

- session bindings
- metadata cache
- distributed-lock helpers
- other short-lived cache/coordination entries

If Redis is unavailable, the system falls back gracefully with weaker continuity guarantees.

## Required Configuration

```bash
export DB_POSTGRES_URL="postgresql://postgres:postgres@localhost:5432/governance"
export DB_AGE_GRAPH="governance_graph"  # optional but recommended
```

## Quick Checks

```bash
# PostgreSQL connectivity
pg_isready -d "$DB_POSTGRES_URL"
psql "$DB_POSTGRES_URL" -c "SELECT 1;"

# Local health endpoint
curl http://127.0.0.1:8767/health

# Redis optional cache
redis-cli PING
```

## Read Next

- [../UNIFIED_ARCHITECTURE.md](../UNIFIED_ARCHITECTURE.md): runtime architecture
- [OPERATOR_RUNBOOK.md](OPERATOR_RUNBOOK.md): startup, health, triage
- [../dev/CANONICAL_SOURCES.md](../dev/CANONICAL_SOURCES.md): authority ordering

**Last Updated:** 2026-04-04 (reduced to thin infrastructure reference)
