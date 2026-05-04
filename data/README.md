# Data Directory

Runtime data for UNITARES Governance. Everything here is generated at runtime and **not tracked in git** (except this README, `.gitkeep`, and `agent_metadata.example.json`).

## Structure

```
data/
├── agents/              # Active agent state files
├── history/             # Historical exports
├── dialectic_sessions/  # Dialectic session JSON snapshots
├── logs/                # Server logs (mcp_server.log, mcp_server_error.log)
├── locks/               # Runtime locks
├── processes/           # Process tracking
├── telemetry/           # Drift/calibration telemetry
├── governance.db        # SQLite database (legacy, mostly superseded by PostgreSQL)
├── audit_log.jsonl      # Audit trail
└── tool_usage.jsonl     # Tool usage statistics
```

## Primary Storage

As of v2.6.0, PostgreSQL is the primary backend for agents, dialectic sessions, and the knowledge graph. The KG uses PostgreSQL FTS by default; AGE remains available for explicit graph traversal work. SQLite (`governance.db`) remains for some legacy state but is not the source of truth.

## Tracked in Git

Only these files are tracked:
- `README.md` — this file
- `.gitkeep` — directory placeholder
- `agent_metadata.example.json` — template

Everything else is gitignored.
