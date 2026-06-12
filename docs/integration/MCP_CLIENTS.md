# MCP client configuration

Client-specific JSON for pointing an MCP-aware tool at a local Unitares governance server. Assumes the server is running on `http://localhost:8767/mcp/` — see the root README for startup.

## Cursor / Claude Code

Native `type: http` support:

```json
{
  "mcpServers": {
    "unitares": {
      "type": "http",
      "url": "http://localhost:8767/mcp/"
    }
  }
}
```

## Claude Desktop

Claude Desktop does not support `type: http` natively; use `mcp-remote` as a stdio bridge:

```json
{
  "mcpServers": {
    "unitares": {
      "command": "npx",
      "args": ["mcp-remote", "http://localhost:8767/mcp/"]
    }
  }
}
```

Agents self-identify through `start_session()` (`onboard(...)` canonically); no hardcoded agent-name header is required.

Agent-facing workflow aliases are registered for the core loop:

- `start_session(...)` → `onboard(...)`
- `sync_state(...)` → `process_agent_update(...)`
- `check_working_state(...)` → `get_governance_metrics(...)`
- `search_shared_memory(...)` → `knowledge(action="search", ...)`
- `record_result(...)` → `outcome_event(...)`
- `request_review(...)` → `dialectic(action="request", ...)`

## Endpoints

| Endpoint | Transport | Use case |
|----------|-----------|----------|
| `/mcp/` | Streamable HTTP | MCP clients |
| `/v1/tools/call` | REST POST | CLI, scripts, non-MCP clients |
| `/dashboard` | HTTP | Web dashboard |
| `/health` | HTTP | Health checks |

## Bind address and security

The server binds to `127.0.0.1` by default. For LAN or remote access:

- Set `UNITARES_BIND_ALL_INTERFACES=1` (or `UNITARES_MCP_HOST` to an explicit interface).
- Configure `UNITARES_MCP_ALLOWED_HOSTS` and `UNITARES_MCP_ALLOWED_ORIGINS` (comma-separated) to allowlist Host and Origin headers.
- Optional: `UNITARES_HTTP_CORS_EXTRA_ORIGINS`, `UNITARES_MCP_ALLOW_NULL_ORIGIN` (default on for `file://`).

See [`scripts/ops/`](../../scripts/ops/) for an example LaunchAgent plist with bind-all plus allowlists.

## Agent Identity

For a fresh process, call `start_session(force_new=true)`. If the process is continuing prior work, call `start_session(force_new=true, parent_agent_id=<prior uuid>, spawn_reason="new_session")`.

Use canonical `onboard(...)` instead when targeting older servers or when a raw
canonical response shape is required. Friendly alias responses lift
`agent_uuid`, `client_session_id`, and `next_action` while preserving the full
canonical payload under `raw_governance`.

For a same-owner rebind to an existing UUID, call `identity(agent_uuid=..., continuity_token=..., resume=true)` with the matching short-lived token. Do not teach clients to use bare `identity(agent_uuid=..., resume=true)`: UUID alone is an unsigned claim and is hijack-shaped under strict identity mode.

See also: [Getting Started](../guides/START_HERE.md), [Operator Runbook](../operations/OPERATOR_RUNBOOK.md).
