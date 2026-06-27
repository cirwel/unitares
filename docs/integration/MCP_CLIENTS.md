# MCP client configuration

UNITARES is client-neutral at the MCP boundary. Any client that supports
Streamable HTTP MCP can connect to a local governance server at
`http://localhost:8767/mcp/`; clients without native HTTP support can usually
bridge through stdio. Claude is one example client family, not a server-side
assumption. Codex, Hermes, Goose, Cursor, hosted connectors, and custom hosts
can use the same server when they expose MCP or go through a thin adapter.

## Streamable HTTP clients

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

## Stdio-bridge clients

Some clients do not support `type: http` natively; use `mcp-remote` as a stdio
bridge. Claude Desktop is the common example:

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

Agents self-identify through the primary `start_session()` tool; no hardcoded
agent-name header is required. The raw implementation tool is `onboard(...)`.

Primary agent-facing workflow tools are registered for the core loop:

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

## Remote connectors (hosted Claude.ai, Perplexity, etc.)

A hosted MCP connector reaches the server over the public internet, usually via
a reverse proxy or Cloudflare tunnel (e.g. `https://gov.example.org/mcp/`).
Two server-side gates must be configured or the connection fails before any
tool runs:

1. **Host/Origin allowlist (DNS-rebinding protection, on by default).** The
   request arrives with `Host: gov.example.org`, which is rejected with
   **HTTP 403** unless allowlisted. A 403 here surfaces in the client as a
   generic auth/"API key" error even though the cause is the Host gate — it
   runs *before* auth. Fix:

   ```bash
   export UNITARES_BIND_ALL_INTERFACES=1
   export UNITARES_MCP_ALLOWED_HOSTS="gov.example.org,gov.example.org:*"
   export UNITARES_MCP_ALLOWED_ORIGINS="https://gov.example.org"
   ```

   List both the bare host and the `:*` form: over HTTPS the `Host` header is
   usually bare (`gov.example.org`), but a proxy may forward a port.

2. **Authentication.** A public endpoint must not run on the connector's
   "none" option. Two choices:

   - **API key (simplest).** Mint a secret yourself — it is not retrieved from
     anywhere — and set it on the server, then paste the identical string into
     the client's "API key" field:

     ```bash
     export UNITARES_MCP_BEARER_TOKENS="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
     ```

     The client sends it as `Authorization: Bearer <token>`. Comma-separated
     values allow rotation without a restart.

   - **OAuth 2.1.** Set `UNITARES_OAUTH_ISSUER_URL` to the public base URL. The
     server then advertises Dynamic Client Registration, so a DCR-capable
     client (e.g. Claude.ai custom connector) leaves client_id/client_secret
     blank and self-registers. Note: client registrations are in-memory and
     reset on restart. See `src/oauth_provider.py`.

The Host allowlist applies regardless of the auth choice — set it even when
using "none" locally is fine, but for a public host you need both the
allowlist entry *and* an auth gate.

After changing any of these, restart the server, then sanity-check from
outside:

```bash
curl -i https://gov.example.org/mcp/ -H 'Accept: text/event-stream'
# 403 gone -> Host gate open. 401 -> bearer gate is on (expected once a key is set).
```

## Agent Identity

For a fresh process, call `start_session(force_new=true)`. If the process is continuing prior work, call `start_session(force_new=true, parent_agent_id=<prior uuid>, spawn_reason="new_session")`.

Use raw `onboard(...)` instead when targeting older servers or when a raw
implementation response shape is required. Primary workflow responses lift
`agent_uuid`, `client_session_id`, and `next_action` while preserving the full
raw payload under `raw_governance`.

For a same-owner rebind to an existing UUID, call `identity(agent_uuid=..., continuity_token=..., resume=true)` with the matching short-lived token. Do not teach clients to use bare `identity(agent_uuid=..., resume=true)`: UUID alone is an unsigned claim and is hijack-shaped under strict identity mode.

See also: [Getting Started](../guides/START_HERE.md), [Operator Runbook](../operations/OPERATOR_RUNBOOK.md).
