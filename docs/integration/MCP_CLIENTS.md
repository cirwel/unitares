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
   **HTTP 421** unless allowlisted; 403 is the SDK's Origin rejection, not a
   Host one. A 421 is not an auth failure and no credential fixes it. Note
   the order: this server's own bearer/OAuth gate runs *ahead* of the SDK's
   `Host` check, so on a gated deployment an uncredentialed request stops at
   401 and never reaches the allowlist. Fix:

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
     blank and self-registers. Client registrations and issued tokens are
     kept in Redis (keys `unitares:oauth:*`, token keys are SHA-256 digests,
     TTLs follow token lifetimes), so a restart does not sign connectors out;
     with Redis unavailable they fall back to memory and reset on restart.
     The protected MCP resource defaults to
     `<UNITARES_OAUTH_ISSUER_URL>/mcp`; set `UNITARES_OAUTH_RESOURCE_URL` only
     if a reverse proxy exposes the MCP resource at a different public URL.
     See `src/oauth_provider.py`.

     A connector that cannot self-register asks for a client ID and secret
     instead (Google's custom MCP connector does this, and shows a redirect
     URI to allow). Pre-register one:

     ```bash
     export UNITARES_OAUTH_STATIC_CLIENT_ID="$(python3 -c 'import secrets; print("unitares_" + secrets.token_hex(12))')"
     export UNITARES_OAUTH_STATIC_CLIENT_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
     export UNITARES_OAUTH_STATIC_REDIRECT_URIS="<redirect URI the connector shows>"
     ```

     The static client comes from the environment on every start (it is
     never written to Redis, so rotating its secret takes effect on the next
     restart); its tokens persist like any other. The token
     endpoint accepts its secret either in the form body or as HTTP Basic.
     An incomplete static-client configuration fails OAuth setup, which
     closes the gated route rather than opening it (see below).

     **Close registration if the gate should exclude anyone.** Sign-in is
     auto-approved, so while dynamic registration is open any caller can
     register a client and mint a token. Set
     `UNITARES_OAUTH_DYNAMIC_REGISTRATION=false` to admit only
     pre-registered clients. Set it (like every `UNITARES_OAUTH_*` variable)
     in the LaunchAgent plist or process environment: it is read at import,
     before `~/.env.mcp` loads, and a value that arrives only from
     `~/.env.mcp` leaves registration open (startup logs an error; the
     OAuth startup line states whether registration is open). One static client can serve several
     connectors: list each connector's redirect URI in
     `UNITARES_OAUTH_STATIC_REDIRECT_URIS` (comma-separated) and paste the
     same ID and secret into each (claude.ai: the custom connector's
     advanced settings). Connectors sharing a static client share its
     `oauth:<client_id>` session attribution, and if you enable token
     revocation (it is not mounted by default), revoking one connector's
     **access** token stops every connector on that client from refreshing,
     across restarts (their current access tokens still work until they
     expire, up to an hour); revoking a **refresh** token ends only that one. Give each connector its own client via DCR
     if either matters. Alternatively, open registration briefly to add a
     DCR connector and close it again: a registration that received a token
     is kept in Redis, so it stays connected. `POST /register` alone writes
     nothing to Redis, but while registration is open and sign-in is
     auto-approved, anyone can still register, sign in and obtain a token,
     which writes a client and its tokens to Redis; only closing
     registration bounds that. Closing it bounds **new** registrations
     only: a DCR client that already holds a token stays admitted (each
     refresh renews it). To evict every client and token, delete the
     OAuth keys and restart:

     ```bash
     redis-cli --scan --pattern 'unitares:oauth:*' | xargs -r redis-cli del
     ```

     This is targeted (never flush Redis, which holds the live session
     store); pre-registered connectors simply sign in again.

   OAuth gates `/mcp` on **every** request by default, which locks out
   local clients that do not speak OAuth. To keep them working, give the
   public tunnel its own listener:

   ```bash
   export UNITARES_OAUTH_PUBLIC_PORT=8772   # loopback only
   ```

   The server then also listens on `127.0.0.1:8772`, and the OAuth gate
   applies to that listener alone. Point the tunnel's ingress at
   `http://127.0.0.1:8772` (an explicit IPv4 address, not `localhost`).
   **Order matters when migrating an existing OAuth deployment:** a tunnel
   still pointed at the main port is served ungated the moment this variable
   takes effect. The startup warning cannot see that (the main listener is
   on loopback); `UNITARES_OAUTH_REQUIRED=1` refuses the combination unless
   a bearer allowlist gates the main listener.
   Repoint the tunnel first; until the restart it simply gets connection
   refused on the new port.

   The main listener is **not** OAuth-gated in this mode. Anything that
   reaches it — a reverse proxy or tunnel pointed at the main port, or LAN
   and tailnet callers when `UNITARES_BIND_ALL_INTERFACES=1` — gets `/mcp`
   without a credential, as with no OAuth configured. The server warns at
   startup when the main listener binds beyond loopback (`--host` included)
   with no bearer allowlist. `UNITARES_OAUTH_REQUIRED=1` ("a gate on `/mcp`
   or no service") refuses to serve with a public port and no bearer
   allowlist whatever the host, since a loopback main listener is still
   reachable by local processes and by a tunnel left on the main port. The
   refusal runs before bootstrap, so it never stops a running predecessor.
   The flag itself is read at import (a `UNITARES_OAUTH_REQUIRED` set only in
   `~/.env.mcp` does not reach it); the bearer allowlist is read when `main()`
   runs, after `~/.env.mcp`, as the per-request gate reads it. The public entry point belongs on the public port. The
   listener is identified by the socket that accepted the connection, which
   nothing in a request can forge, unlike `Host`, the peer address or
   forwarding headers. REST routes reached through the public listener never
   get the trusted-network bypass.

   On the main listener a presented OAuth token is still checked, so a
   local OAuth client keeps its session attribution; a bad token there is
   ignored rather than refused. An invalid `UNITARES_OAUTH_PUBLIC_PORT`
   is warned about and leaves OAuth on every request. So does a public port
   that cannot be bound (in use, or equal to the main port): with no public
   listener serving, OAuth gates every request on the main listener, and the
   error is logged. If OAuth
   setup fails, the public listener answers 503 and the main listener is
   still served, unless `UNITARES_OAUTH_REQUIRED=1`, which refuses to start.
   If setup fails *and* the public listener cannot bind, nothing confines
   the failed gate, so every `/mcp` request answers 503.
   `scripts/dev/unitares_doctor.py`'s `mcp_route_gate` probes the public
   listener when `UNITARES_OAUTH_PUBLIC_PORT` is exported in its shell and
   that port's `/health` names the same server process as the main
   listener's; otherwise (nothing listening, no issuer on the server, or
   another service on the port) it probes the main listener.
   An incomplete static-client configuration (any of the three variables
   without the others) fails OAuth setup. A bearer allowlist
   (`UNITARES_MCP_BEARER_TOKENS`) stays global regardless.

The Host allowlist applies regardless of the auth choice — set it even when
using "none" locally is fine, but for a public host you need both the
allowlist entry *and* an auth gate.

After changing any of these, restart the server, then sanity-check from
outside:

```bash
curl -i https://gov.example.org/mcp/ -H 'Accept: text/event-stream'
# 401 -> auth gate is on (expected before the client sends a valid bearer/OAuth access token).
# 421 -> the Host is not on the allowlist. 403 is an Origin rejection, not a Host one.
```

### When nothing can connect

Diagnose the gate before touching credentials. `python3
scripts/dev/unitares_doctor.py --mode operator` runs `mcp_route_gate`, which
probes the local listener carrying the public `Host` and names which gate is
closed. Set `UNITARES_DOCTOR_PUBLIC_URL` when the public authority differs from
the OAuth issuer; the check skips rather than guesses if neither is set.

Give that probe a credential if you can. This server runs its bearer/OAuth gate
*ahead* of the SDK's `Host` check, so an anonymous probe stops at 401 having
learned nothing about the allowlist — the check reports that as inconclusive
rather than as health. Export a valid `UNITARES_MCP_BEARER_TOKENS` value in the
shell you run the doctor from and it reaches the `Host` check. On an OAuth-only
deployment there is no token to borrow, and comparing the hostname against
`UNITARES_MCP_ALLOWED_HOSTS` by hand is the only route.

Three things make a lockout harder than it needs to be:

- **421 is not an auth failure and no credential fixes it.** DNS-rebinding
  protection validates the `Host` on `/mcp/` only, so `/health` and every other
  custom route keeps returning 200 while MCP calls are refused, and both the
  deploy check and the status table read green. Add the hostname to
  `UNITARES_MCP_ALLOWED_HOSTS`. An entry without a port does not match a `Host`
  that carries one, so use an explicit `host:port` or a `host:*` wildcard.
- **Do not let OAuth be the only credential.** `UNITARES_MCP_BEARER_TOKENS` is
  an independent path, and comma-separated values rotate without a restart. Keep
  one set, and keep a copy somewhere you can reach while locked out.
- **`/mcp` has no loopback or UDS bypass, by design.** `check_mcp_bearer` has no
  trusted-network branch, because a hosted server sees only its proxy's address;
  every request to `/mcp` authenticates, over UDS as much as over TCP. The
  loopback, RFC1918 and Tailscale bypass exists on the REST surface only — and
  setting `UNITARES_MCP_BEARER_TOKENS` flips REST into strict posture, which
  removes it there too. So the break-glass is a credential you kept, not a
  network position; for REST specifically, `UNITARES_REST_STRICT=0` restores the
  bypass.

OAuth state now survives a restart (`src/oauth_provider.py`, `RedisOAuthStore`),
so a restart is no longer a way to clear it. To sign every OAuth client out,
delete the `unitares:oauth:*` keys (targeted — never flush Redis, which holds
the live session store; command above) and restart. There is no per-client
sign-out by default: the revocation endpoint is not mounted.

If provider construction fails, `/mcp` **closes rather than opening**. The route
answers `503 auth_unavailable`, because serving it unauthenticated would answer a
different question than the one `UNITARES_OAUTH_ISSUER_URL` asked, and on a
public deployment that means the whole tool catalog and the knowledge graph. The
closure is scoped to that route: `/health*`, `/v1`, the websocket, the dashboard
and the UDS resident listener keep their own gates, which is why the process
stays up instead of refusing to start. Setting `UNITARES_MCP_BEARER_TOKENS`
reopens `/mcp` with a credential that does not depend on OAuth, so hardening
never locks you out on its own.

`UNITARES_OAUTH_REQUIRED=1` goes further and refuses to start the process at all.
It is satisfied by a bearer allowlist as well as by OAuth — what it requires is a
gate on `/mcp`, not OAuth specifically — and it must be in the process
environment (LaunchAgent plist or a shell export), since `~/.env.mcp` is loaded
after import and cannot reach it. Weigh it against the route-scoped closure
above, which already prevents an unauthenticated `/mcp`: the process-level
refusal exits before binding, launchd respawns it every ten seconds
indefinitely, and that outage is not self-announcing, because the gateway's
`/health` on `:8768` is hardcoded to `ok`.

## Agent Identity

For a fresh process, call `start_session(force_new=true)`. If the process is taking over from a predecessor that has **exited**, call `start_session(force_new=true, parent_agent_id=<prior uuid>, spawn_reason="explicit")`. Sharing a workspace with a still-running agent is co-location, not lineage: a succession claim naming a live parent is rejected (`lineage_coincidental_rejected`) and cleared.

Keep the returned `client_session_id` and pass it on every later call from the same process. Without it, writes fall back to a weak transport-fingerprint binding that can merge or split co-resident sessions.

Two kinds of process do not follow this default. A short dispatched subagent usually should not onboard at all; if it needs its own identity, it calls `start_session(force_new=true, parent_agent_id=<driver uuid>, spawn_reason="subagent")` and lands at least one real `sync_state()` before it exits. Persistent or substrate agents use their dedicated substrate identity pattern, not an ordinary session.

Use raw `onboard(...)` instead when targeting older servers or when a raw
implementation response shape is required. Primary workflow responses lift
`agent_uuid`, `client_session_id`, and `next_action`. Read aliases default to a
compact envelope; request their documented full mode when the canonical payload
is needed under `raw_governance`.

For a same-owner rebind to an existing UUID, call `identity(agent_uuid=..., continuity_token=..., resume=true)` with the matching short-lived token. Do not teach clients to use bare `identity(agent_uuid=..., resume=true)`: UUID alone is an unsigned claim and is hijack-shaped under strict identity mode.

See also: [Getting Started](../guides/START_HERE.md), [Operator Runbook](../operations/OPERATOR_RUNBOOK.md).
