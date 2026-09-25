# MCP client configuration

UNITARES is client-neutral at the MCP boundary. Any client that supports
Streamable HTTP MCP can connect to a local governance server at
`http://localhost:8767/mcp/`; clients without native HTTP support can usually
bridge through stdio. Claude is one example client family, not a server-side
assumption. Codex, Hermes, other MCP-capable editors and agent CLIs, hosted
connectors, and custom hosts can use the same server when they expose MCP or go
through a thin adapter.

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
     blank and self-registers. Note: client registrations are in-memory and
     reset on restart. The protected MCP resource defaults to
     `<UNITARES_OAUTH_ISSUER_URL>/mcp`; set `UNITARES_OAUTH_RESOURCE_URL` only
     if a reverse proxy exposes the MCP resource at a different public URL.
     See `src/oauth_provider.py`.

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

OAuth state cannot strand you across a restart: client registrations and tokens
are in-memory and reset when the process does (`src/oauth_provider.py`).

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
`agent_uuid`, `client_session_id`, and `next_action`; `start_session` also lifts
`agent_id`, `display_name`, and `continuity_token` when one is issued. Read
aliases and the write aliases except `request_review` default to a compact
envelope whose `raw_governance_hint` names the full-payload route:
`response_mode="full"` on `sync_state`, `search_shared_memory` and
`record_result`, `verbosity="full"` on `check_working_state`, a
`knowledge(action="details", discovery_id=...)` read for `store_finding` and
`update_finding`, whose schemas declare no response mode, and an
`identity(client_session_id=...)` read for `start_session`. Repeating a write to
see its payload writes again, and a second `start_session(force_new=true)` mints
a second identity; pass `response_mode="full"` on the first `start_session` call
if the full onboard payload is needed. `start_session` also lifts
`resident_registration`, `label_renamed` and `bootstrap` when onboard reports
them.

For a same-owner rebind to an existing UUID, call `identity(agent_uuid=..., continuity_token=..., resume=true)` with the matching short-lived token. Do not teach clients to use bare `identity(agent_uuid=..., resume=true)`: UUID alone is an unsigned claim and is hijack-shaped under strict identity mode.

See also: [Getting Started](../guides/START_HERE.md), [Operator Runbook](../operations/OPERATOR_RUNBOOK.md).
