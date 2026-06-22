# 3 · Running the server

[← Installation](02-install.md) · [Manual index](README.md) · [Next: Integrating agents →](04-integrating-agents.md)

## 3.1 Starting it

```bash
python src/mcp_server.py --port 8767
```

Within a few seconds you should see a log line ending like `Uvicorn running on http://127.0.0.1:8767`. Under Docker, `docker compose up -d --wait` starts it for you.

**The server binds to `127.0.0.1` only by default** — it is not reachable from your LAN until you opt in (see [§3.5](#35-exposing-beyond-loopback)). That default is intentional: the threat model is internal fleet hygiene, not hostile external clients.

## 3.2 Ports and services

A full UNITARES deployment is several bound services. All bind loopback by default.

| Service | Port | Endpoint(s) | Purpose |
|---|---|---|---|
| Governance MCP | `8767` | `/mcp/` (Streamable HTTP), `/v1/tools/call` (REST), `/dashboard`, `/health` | Primary agent surface — check-ins, queries, verdicts |
| Gateway MCP | `8768` | `/mcp/` | Reduced surface for weak external clients |
| Lease plane | `8788` | `/v1/lease/*` (bearer-auth, fail-closed) | Elixir/OTP coordination for single-writer surfaces |
| PostgreSQL + AGE | `5432` | `postgresql://…/governance` | Single source of truth |
| Redis | `6379` | `redis://…/0` | Session cache (optional) |

Full port map: [`../operations/DEFINITIVE_PORTS.md`](../operations/DEFINITIVE_PORTS.md).

## 3.3 Transports — three ways to call it

| Endpoint | Transport | Use case |
|---|---|---|
| `/mcp/` | Streamable HTTP | MCP clients (Cursor, Claude Code, Claude Desktop via bridge) |
| `/v1/tools/call` | REST POST | CLI, scripts, non-MCP clients |
| `/dashboard` | HTTP | The web dashboard |
| `/health`, `/health/live` | HTTP | Health checks |

REST call shape (any tool):

```bash
curl -s -X POST http://127.0.0.1:8767/v1/tools/call \
  -H 'Content-Type: application/json' \
  -d '{"tool":"<tool_name>","arguments":{ ... }}'
```

Client-specific MCP JSON (Cursor, Claude Code, Claude Desktop) is in [chapter 4](04-integrating-agents.md#42-pointing-a-client-at-the-server) and canonically in [`../integration/MCP_CLIENTS.md`](../integration/MCP_CLIENTS.md).

## 3.4 The dashboard

Open `http://127.0.0.1:8767/dashboard` (or `/` ). It reads PostgreSQL directly with the same auth model as MCP and gives operators a human view of the fleet:

- **Stats** — fleet coherence, active/total agents, stuck agents, discoveries, dialectic sessions, system health, calibration, anomalies, trust-tier distribution.
- **Pulse** — latest decision and risk/confidence/complexity vitals, event sparkline.
- **EISV** — fleet and per-agent time-series charts.
- **Agents** — searchable/filterable table with status, metrics, trust tiers, lineage/supersession badges, and operator actions.
- **Discoveries / Dialectic / Activity** — knowledge-graph entries, peer-review sessions, and a live timeline of check-ins, verdicts, and lifecycle events.
- **Residents** and per-resident panels (Chronicler, Watcher, Sentinel, Vigil).
- **Phase space** at `/phase` — E/I particles, basin contours, flow field, live updates.

Live updates stream over a WebSocket at `/ws/eisv`, falling back to 30-second polling. If `UNITARES_HTTP_API_TOKEN` is configured, append `?token=<token>` (or set `localStorage.unitares_api_token`); write actions under strict-identity mode additionally need an operator token. Implementation detail: [`dashboard/README.md`](../../dashboard/README.md). Public deployment screenshots: [`../PRODUCTION_SNAPSHOT.md`](../PRODUCTION_SNAPSHOT.md).

## 3.5 Exposing beyond loopback

Anything past `127.0.0.1` is an explicit operator decision. Two gates must be set or the connection fails before any tool runs.

### LAN

```bash
export UNITARES_BIND_ALL_INTERFACES=1
export UNITARES_MCP_ALLOWED_HOSTS="<your-lan-ip>:*,<your-hostname>.local"
export UNITARES_MCP_ALLOWED_ORIGINS="http://<your-lan-ip>:*"
python src/mcp_server.py --port 8767
```

### Public host / remote connector (Claude.ai, Perplexity, …)

1. **Host/Origin allowlist (DNS-rebinding protection, on by default).** A request with an un-allowlisted `Host` is rejected with **HTTP 403** *before* auth — which surfaces in clients as a generic auth/"API key" error. List both the bare host and the `:*` form:

   ```bash
   export UNITARES_BIND_ALL_INTERFACES=1
   export UNITARES_MCP_ALLOWED_HOSTS="gov.example.org,gov.example.org:*"
   export UNITARES_MCP_ALLOWED_ORIGINS="https://gov.example.org"
   ```

2. **Authentication** — a public endpoint must not run the connector's "none" option:

   ```bash
   # Simplest: a bearer token you mint yourself, then paste into the client's API-key field
   export UNITARES_MCP_BEARER_TOKENS="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
   ```

   Comma-separated values rotate without a restart. For OAuth 2.1 with Dynamic Client Registration, set `UNITARES_OAUTH_ISSUER_URL` instead (see [`../integration/MCP_CLIENTS.md`](../integration/MCP_CLIENTS.md#remote-connectors-claudeai-perplexity-etc) and `src/oauth_provider.py`).

After changing any of these, restart and sanity-check from outside:

```bash
curl -i https://gov.example.org/mcp/ -H 'Accept: text/event-stream'
# 403 gone → Host gate open. 401 → bearer gate on (expected once a key is set).
```

## 3.6 Key environment variables

| Variable | Effect |
|---|---|
| `DB_BACKEND` / `DB_POSTGRES_URL` | Database backend and DSN |
| `DB_AGE_GRAPH` | AGE graph name (e.g. `governance_graph`) |
| `UNITARES_KNOWLEDGE_BACKEND` | `postgres` (default, FTS) or `age` (cypher-style traversal) |
| `UNITARES_DISABLE_ODE=1` | Behavioral-EISV verdict path only; skip the ODE math model |
| `UNITARES_BIND_ALL_INTERFACES` / `UNITARES_MCP_HOST` | Bind beyond loopback |
| `UNITARES_MCP_ALLOWED_HOSTS` / `UNITARES_MCP_ALLOWED_ORIGINS` | Host/Origin allowlists |
| `UNITARES_MCP_BEARER_TOKENS` | Bearer auth (comma-separated, hot-rotatable) |
| `UNITARES_OAUTH_ISSUER_URL` | Enable OAuth 2.1 / DCR |
| `UNITARES_HTTP_API_TOKEN` / `UNITARES_OPERATOR_TOKENS` | Dashboard read / operator-write tokens |
| `UNITARES_RESIDENTS` | The named resident agent set (config, not hardcoded) |

## 3.7 Run at login (macOS LaunchAgent)

The repo ships a plist template that renders with your paths and generated secrets:

```bash
sed -e "s|/PATH/TO/UNITARES|$PWD|g" \
    -e "s|/PATH/TO/PYTHON3|$PWD/.venv/bin/python|g" \
    -e "s|/YOUR/HOME|$HOME|g" \
    -e "s|GENERATE_YOUR_OWN_TOKEN|$(openssl rand -hex 32)|g" \
    -e "s|GENERATE_YOUR_OWN_SECRET|$(openssl rand -hex 32)|g" \
    scripts/ops/com.unitares.governance-mcp.plist \
    > ~/Library/LaunchAgents/com.unitares.governance-mcp.plist
launchctl load ~/Library/LaunchAgents/com.unitares.governance-mcp.plist
launchctl list | grep unitares
```

Defaults bind loopback-only and use the trust-auth Postgres connection. Other tunables are inline at the top of the rendered plist.

---

[← Installation](02-install.md) · [Manual index](README.md) · [Next: Integrating agents →](04-integrating-agents.md)
