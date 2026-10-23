# 3 · Running the server

[← Installation](02-install.md) · [Manual index](README.md) · [Next: Integrating agents →](04-integrating-agents.md)

## 3.1 Starting it

```bash
python src/mcp_server.py --port 8767
```

Within a few seconds you should see a log line ending like `Uvicorn running on http://127.0.0.1:8767`. Under Docker, `docker compose up -d --wait` starts it for you.

From a Docker checkout, `./scripts/unitares` wraps the same steps: `start` and `stop` start and stop the stack's containers (`stop` does not remove them), and `logs [service]` follows a service's log (the governance server by default). `status`, `health` and `diag` are read-only checks; `./scripts/unitares help` lists the rest. The same script picks the model (`model`, see [Choose a model](02-install.md#choose-a-model-optional)) and moves the install to a new release (`update`, see [Updating](02-install.md#updating)).

**The server binds to `127.0.0.1` only by default** — it is not reachable from your LAN until you opt in (see [§3.5](#35-exposing-beyond-loopback)). That default is intentional: it assumes a single trusted operator network, and multi-tenant or internet-facing use needs a harder auth posture (see [`SCOPE_AND_THREAT_MODEL.md`](../SCOPE_AND_THREAT_MODEL.md#scope-and-threat-model)).

## 3.2 Ports and services

A full UNITARES deployment is several bound services. All bind loopback by default.

| Service | Port | Endpoint(s) | Purpose |
|---|---|---|---|
| Governance MCP | `8767` | `/mcp/` (Streamable HTTP), `/v1/tools/call` (REST), `/dashboard`, `/health` | Primary agent surface — check-ins, queries, verdicts |
| Gateway MCP | `8768` | `/mcp/` | Reduced surface for weak external clients (optional; not started by Compose) |
| Lease plane | `8788` | `/v1/lease/*`, `/v1/effects`, `/v1/dialectic/*`, `/v1/msg/*` (bearer-auth, fail-closed); open `GET /health` | Elixir/OTP coordination for single-writer surfaces and governed effects |
| PostgreSQL + AGE | `5432` | `postgresql://…/governance` | Canonical durable store — source of truth for governance data (live sessions/identity bindings are the exception: de-facto Redis-primary today, see `docs/proposals/archive/redis-retirement-v0.md`) |
| Redis | `6379` | `redis://…/0` | Session/identity store — degrades to local-only without it, but de-facto primary for most live sessions (not optional) |

Full port map: [`../operations/DEFINITIVE_PORTS.md`](../operations/DEFINITIVE_PORTS.md).

## 3.3 Transports — three ways to call it

| Endpoint | Transport | Use case |
|---|---|---|
| `/mcp/` | Streamable HTTP | MCP clients (Claude Code, Codex, Hermes, any Streamable HTTP client; stdio-only clients via bridge) |
| `/v1/tools/call` | REST POST | CLI, scripts, non-MCP clients |
| `/dashboard` | HTTP | The web dashboard |
| `/health`, `/health/live`, `/health/ready` | HTTP | Health checks (the Compose healthcheck uses `/health/ready`) |
| `/health/deep` | HTTP | Component-level health; gated like a data route, and what `update` reports |

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

Open `http://127.0.0.1:8767/dashboard` (or `/` ). It reads the server's REST endpoints (and `/v1/tools/call`) with the REST auth model (a passkey session or `UNITARES_HTTP_API_TOKEN`), shows a bundled snapshot when the server does not answer, and gives operators a human view of the fleet:

- **Overview** — agent-first cards (Agents checked in within the hour, Check-ins by verdict, Dialectic, Discoveries, System Health), then the latest check-in with its risk and EISV readout and a feed over any agent. A resident strip appears only when the deployment configures residents.
- **EISV** — fleet and per-agent time-series charts.
- **Agents** — searchable table, filterable by lifecycle, with status, metrics, trust-tier and lineage/supersession badges. The governance views are read-only; archive, resume and review requests go through the MCP tools or CLI.
- **Discoveries / Dialectic / Activity** — knowledge-graph entries, peer-review sessions, and a live timeline of check-ins, verdicts, and lifecycle events.
- **Risk / Security** — the risk trend over time, and passkey and dashboard-session management.
- **Extensions** — a deployment adds its own tabs (residents, automations and the like) from `UNITARES_DASHBOARD_EXT_DIR`; see [`dashboard/EXTENSIONS.md`](../../dashboard/EXTENSIONS.md).
- **Phase space** at `/phase` — E/I particles, basin contours, flow field, live updates.

Live updates stream over a WebSocket at `/ws/eisv`, falling back to 10-second polling of the active monitor view while the stream reconnects. If `UNITARES_HTTP_API_TOKEN` is configured and you are not signed in with a passkey, set `localStorage.unitares_api_token` in the browser console; the dashboard sends it as an `Authorization` header on REST calls and as a `Sec-WebSocket-Protocol` entry on the socket, never in a URL. The socket does not read a `?token=` query (the server logs request lines, and a tunnel or proxy sees the URL). Opening the dashboard once with `?token=<token>` still works as a handoff — the page stores it and scrubs it from the address bar — but that first request line has already carried it. Minting a passkey enrollment code on the Security tab needs an operator token (`UNITARES_OPERATOR_TOKENS`, sent as `X-Unitares-Operator`). Implementation detail: [`dashboard/README.md`](../../dashboard/README.md).

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

   Comma-separated values let an old and a new token overlap during a rotation; the server reads the list from its environment, so a changed list takes effect on restart. For OAuth 2.1 with Dynamic Client Registration, set `UNITARES_OAUTH_ISSUER_URL` instead (see [`../integration/MCP_CLIENTS.md`](../integration/MCP_CLIENTS.md#remote-connectors-hosted-claudeai-perplexity-etc) and `src/oauth_provider.py`). OAuth protects the `/mcp` resource by default; use `UNITARES_OAUTH_RESOURCE_URL` only if a proxy exposes it at a different public URL. OAuth gates `/mcp` on every request, which locks out local clients that do not speak OAuth; set `UNITARES_OAUTH_PUBLIC_PORT` to give the tunnel its own OAuth-gated loopback listener. While dynamic registration is open (the default) and sign-in auto-approves, any caller can mint a token: set `UNITARES_OAUTH_DYNAMIC_REGISTRATION=false` to admit only pre-registered clients.

After changing any of these, restart and sanity-check from outside:

```bash
curl -i https://gov.example.org/mcp/ -H 'Accept: text/event-stream'
# 401 → auth gate on (expected here: this request carries no bearer/OAuth access token, so the Host check never runs).
# With a valid credential: 421 → Host not allowlisted, 403 → Origin rejected.
```

## 3.6 Key environment variables

| Variable | Effect |
|---|---|
| `DB_POSTGRES_URL` | PostgreSQL DSN (PostgreSQL is the only database backend) |
| `DB_AGE_GRAPH` | AGE graph name (e.g. `governance_graph`) |
| `UNITARES_KNOWLEDGE_BACKEND` | `age` (the compose and `.env.example` default: adds graph edges, semantic search and `superseded_by`; see below) or `postgres` (findings and full-text search only); unset, a native install resolves to `postgres` |
| `UNITARES_BIND_ALL_INTERFACES` / `UNITARES_MCP_HOST` | Bind beyond loopback |
| `UNITARES_MCP_ALLOWED_HOSTS` / `UNITARES_MCP_ALLOWED_ORIGINS` | Host/Origin allowlists |
| `UNITARES_MCP_BEARER_TOKENS` | Bearer auth (comma-separated, so old and new tokens can overlap during a rotation) |
| `UNITARES_OAUTH_ISSUER_URL` | Enable OAuth 2.1 / DCR |
| `UNITARES_OAUTH_RESOURCE_URL` | Optional OAuth protected-resource URL override (defaults to `<issuer>/mcp`) |
| `UNITARES_OAUTH_PUBLIC_PORT` | Confine OAuth to a separate loopback listener, so local non-OAuth clients keep `/mcp` |
| `UNITARES_OAUTH_DYNAMIC_REGISTRATION` | `false` admits only pre-registered OAuth clients (open by default) |
| `UNITARES_TRUSTED_NETWORKS` | Extra CIDRs whose callers skip REST auth, added to the built-in loopback and private RFC1918 ranges |
| `REDIS_URL` | Redis URL (default `redis://localhost:6379/0`) |
| `UNITARES_TOOL_ADVERTISEMENT` | `progressive` (default, small initial `tools/list`) or `full` |
| `UNITARES_HTTP_API_TOKEN` / `UNITARES_OPERATOR_TOKENS` | Dashboard read / operator-write tokens |
| `UNITARES_RESIDENTS` | The named resident agent set (config, not hardcoded) |
| `UNITARES_MODEL_BASE_URL` / `UNITARES_MODEL_ID` | OpenAI-compatible base URL (`/v1` included) and model id for `consult` and the local reviewer; the older `UNITARES_OLLAMA_BASE`, `UNITARES_OLLAMA_BASE_URL` and `UNITARES_LLM_MODEL` were removed in v3.3.0 and are ignored, so rename them ([Choose a model](02-install.md#choose-a-model-optional)) |

### Search and retrieval flags

Shared-memory search is full-text on the `postgres` backend and semantic on
`age` (pass `search_mode="fts"` to force full-text). Hybrid fusion, reranking and
related-finding expansion are opt-in.

| Variable | Effect |
|---|---|
| `UNITARES_KNOWLEDGE_BACKEND` | `postgres` (what a native install resolves to when the variable is unset) stores findings and serves full-text search, with no graph edges. `age` (the compose default) stores the same rows and also writes the graph in Apache AGE: an `AUTHORED` edge from the writing process, `RELATED_TO` edges to earlier findings that share tags, `TAGGED`, `RESPONDS_TO` for a `response_to` link, and `SUPERSEDES` when a finding replaces another. Semantic search and the replacement id on a superseded result (`superseded_by`) both need `age`; a superseded result is flagged, and its replacement is not fetched into the results. |
| `UNITARES_EMBEDDING_MODEL` | Picks the embedding model for semantic search (for example `bge-m3`). Unset means `minilm` (all-MiniLM-L6-v2); it does not turn semantic search off. Semantic search needs `sentence-transformers` and the `age` backend. |
| `UNITARES_ENABLE_HYBRID` | `1` fuses semantic and full-text results with reciprocal rank fusion. Off by default; needs semantic search. |
| `UNITARES_ENABLE_RERANKER` | `1` re-scores the top candidates with a cross-encoder (`UNITARES_RERANKER_MODEL`). Off by default; needs `sentence-transformers`. |
| `UNITARES_ENABLE_RELATED_EXPANSION` | Off by default; leave it off. When on, it adds the findings each top result lists in its stored `related_to` column (its tag-overlap links), not the AGE edges, and it lets a finding's retrievability grow with its own link count. The docstring of `related_expansion_enabled` in `src/retrieval.py` explains the feedback loop. The old name, `UNITARES_ENABLE_GRAPH_EXPANSION`, is still read, with a deprecation warning, when this one is unset. |

The graph records who wrote what and what replaced it. It is not an entity or
fact graph, and search does not traverse its edges.

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

The template sets `UNITARES_BIND_ALL_INTERFACES=0` and passes no `--host`, so the rendered plist binds `127.0.0.1` — set that key to `1`, or set `UNITARES_MCP_HOST`, only when LAN clients connect directly; a tunnel or reverse proxy on the same host does not need it (`default_listen_host` in `src/mcp_listen_config.py`). Its `DB_POSTGRES_URL` names the `postgres` role and password rather than the user/password-free trust-auth DSN. Other tunables are inline at the top of the rendered plist.

---

[← Installation](02-install.md) · [Manual index](README.md) · [Next: Integrating agents →](04-integrating-agents.md)
