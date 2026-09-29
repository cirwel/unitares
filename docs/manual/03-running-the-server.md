# 3 · Running the server

[← Installation](02-install.md) · [Manual index](README.md) · [Next: Integrating agents →](04-integrating-agents.md)

## 3.1 Starting it

```bash
python src/mcp_server.py --port 8767
```

Within a few seconds you should see a log line ending like `Uvicorn running on http://127.0.0.1:8767`. Under Docker, `docker compose up -d --wait` starts it for you.

From a Docker checkout, `./scripts/unitares` wraps the same steps: `start` and `stop` bring the stack up and down, and `logs [service]` follows a service's log (the governance server by default). The same script picks the model (`model`, see [Choose a model](02-install.md#choose-a-model-optional)) and moves the install to a new release (`update`, see [Updating](02-install.md#updating)).

**The server binds to `127.0.0.1` only by default** — it is not reachable from your LAN until you opt in (see [§3.5](#35-exposing-beyond-loopback)). That default is intentional: the threat model is internal fleet hygiene, not hostile external clients.

## 3.2 Ports and services

A full UNITARES deployment is several bound services. All bind loopback by default.

| Service | Port | Endpoint(s) | Purpose |
|---|---|---|---|
| Governance MCP | `8767` | `/mcp/` (Streamable HTTP), `/v1/tools/call` (REST), `/dashboard`, `/health` | Primary agent surface — check-ins, queries, verdicts |
| Gateway MCP | `8768` | `/mcp/` | Reduced surface for weak external clients |
| Lease plane | `8788` | `/v1/lease/*` (bearer-auth, fail-closed) | Elixir/OTP coordination for single-writer surfaces |
| PostgreSQL + AGE | `5432` | `postgresql://…/governance` | Canonical durable store — source of truth for governance data (live sessions/identity bindings are the exception: de-facto Redis-primary today, see `docs/proposals/archive/redis-retirement-v0.md`) |
| Redis | `6379` | `redis://…/0` | Session/identity store — degrades to local-only without it, but de-facto primary for most live sessions (not optional) |

Full port map: [`../operations/DEFINITIVE_PORTS.md`](../operations/DEFINITIVE_PORTS.md).

## 3.3 Transports — three ways to call it

| Endpoint | Transport | Use case |
|---|---|---|
| `/mcp/` | Streamable HTTP | MCP clients (Claude Code, Codex, Hermes, any Streamable HTTP client; stdio-only clients via bridge) |
| `/v1/tools/call` | REST POST | CLI, scripts, non-MCP clients |
| `/dashboard` | HTTP | The web dashboard |
| `/health`, `/health/live` | HTTP | Health checks |

REST call shape (any tool):

```bash
curl -s -X POST http://127.0.0.1:8767/v1/tools/call \
  -H 'Content-Type: application/json' \
  -d '{"name":"<tool_name>","arguments":{ ... }}'
```

Client-specific MCP configuration (Claude Code, Codex, Claude Desktop, others) is routed from
[chapter 4](04-integrating-agents.md#45-connect-a-client-or-resident) and lives
canonically in [`../integration/MCP_CLIENTS.md`](../integration/MCP_CLIENTS.md).

## 3.4 The dashboard

Open `http://127.0.0.1:8767/dashboard` (or `/` ). It reads PostgreSQL directly with the same auth model as MCP and gives operators a human view of the fleet:

- **Overview** — agent-first cards (Agents checked in within the hour, Check-ins by verdict, Dialectic, Discoveries, System Health), then the latest check-in with its risk and EISV readout and a feed over any agent. A resident strip appears only when the deployment configures residents.
- **EISV** — fleet and per-agent time-series charts.
- **Agents** — searchable table, filterable by lifecycle, with status, metrics, trust-tier and lineage/supersession badges, and operator actions.
- **Discoveries / Dialectic / Activity** — knowledge-graph entries, peer-review sessions, and a live timeline of check-ins, verdicts, and lifecycle events.
- **Risk / Security** — the risk trend over time, and passkey and dashboard-session management.
- **Extensions** — a deployment adds its own tabs (residents, automations and the like) from `UNITARES_DASHBOARD_EXT_DIR`; see [`dashboard/EXTENSIONS.md`](../../dashboard/EXTENSIONS.md).
- **Phase space** at `/phase` — E/I particles, basin contours, flow field, live updates.

Live updates stream over a WebSocket at `/ws/eisv`, falling back to 30-second polling. If `UNITARES_HTTP_API_TOKEN` is configured and you are not signed in with a passkey, set `localStorage.unitares_api_token` in the browser console; the dashboard sends it as an `Authorization` header on REST calls and as a `Sec-WebSocket-Protocol` entry on the socket, never in a URL. The socket does not read a `?token=` query (the server logs request lines, and a tunnel or proxy sees the URL). Opening the dashboard once with `?token=<token>` still works as a handoff — the page stores it and scrubs it from the address bar — but that first request line has already carried it. Write actions under strict-identity mode additionally need an operator token. Implementation detail: [`dashboard/README.md`](../../dashboard/README.md).

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

1. **Host/Origin allowlist (DNS-rebinding protection, on by default).** A request with an un-allowlisted `Host` is rejected with **HTTP 421**, and only *after* auth: `make_streamable_mcp_asgi` in `src/services/mcp_transport_service.py` runs `authorize_mcp_request` before handing the request to the SDK session manager that validates the `Host`, so an uncredentialed client stops at 401 and learns nothing about the allowlist. (403 is an Origin rejection, not a Host one.) List both the bare host and the `:*` form:

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

   Comma-separated values rotate without a restart. For OAuth 2.1 with Dynamic Client Registration, set `UNITARES_OAUTH_ISSUER_URL` instead (see [`../integration/MCP_CLIENTS.md`](../integration/MCP_CLIENTS.md#remote-connectors-claudeai-perplexity-etc) and `src/oauth_provider.py`). OAuth protects the `/mcp` resource by default; use `UNITARES_OAUTH_RESOURCE_URL` only if a proxy exposes it at a different public URL.

After changing any of these, restart and sanity-check from outside:

```bash
curl -i https://gov.example.org/mcp/ -H 'Accept: text/event-stream'
# 403 gone → Host gate open. 401 → auth gate on (expected before the client sends a valid bearer/OAuth access token).
```

## 3.6 Key environment variables

| Variable | Effect |
|---|---|
| `DB_BACKEND` / `DB_POSTGRES_URL` | Database backend and DSN |
| `DB_AGE_GRAPH` | AGE graph name (e.g. `governance_graph`) |
| `UNITARES_KNOWLEDGE_BACKEND` | `postgres` (default, FTS) or `age` (cypher-style traversal) |
| `UNITARES_BIND_ALL_INTERFACES` / `UNITARES_MCP_HOST` | Bind beyond loopback |
| `UNITARES_MCP_ALLOWED_HOSTS` / `UNITARES_MCP_ALLOWED_ORIGINS` | Host/Origin allowlists |
| `UNITARES_MCP_BEARER_TOKENS` | Bearer auth (comma-separated, hot-rotatable) |
| `UNITARES_OAUTH_ISSUER_URL` | Enable OAuth 2.1 / DCR |
| `UNITARES_OAUTH_RESOURCE_URL` | Optional OAuth protected-resource URL override (defaults to `<issuer>/mcp`) |
| `UNITARES_HTTP_API_TOKEN` / `UNITARES_OPERATOR_TOKENS` | Dashboard read / operator-write tokens |
| `UNITARES_RESIDENTS` | The named resident agent set (config, not hardcoded) |
| `UNITARES_MODEL_BASE_URL` / `UNITARES_MODEL_ID` | OpenAI-compatible base URL (`/v1` included) and model id for `consult` and the local reviewer; the older `UNITARES_OLLAMA_BASE`, `UNITARES_OLLAMA_BASE_URL` and `UNITARES_LLM_MODEL` are read until v3.3.0 ([Choose a model](02-install.md#choose-a-model-optional)) |

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

The template sets `UNITARES_BIND_ALL_INTERFACES=1` and passes no `--host`, so the rendered plist binds `0.0.0.0`, not loopback — remove that key, or set `UNITARES_MCP_HOST=127.0.0.1`, for a loopback-only install (`default_listen_host` in `src/mcp_listen_config.py`). Its `DB_POSTGRES_URL` names the `postgres` role and password rather than the user/password-free trust-auth DSN. Other tunables are inline at the top of the rendered plist.

---

[← Installation](02-install.md) · [Manual index](README.md) · [Next: Integrating agents →](04-integrating-agents.md)
