# Troubleshooting Guide

Status: live troubleshooting guide. Use for failure diagnosis and operator recovery steps, not as the primary architecture reference.

**Last Updated:** June 2026

---

## Quick Diagnostics

### Check Server Status
```bash
# Health check
curl http://localhost:8767/health | python3 -m json.tool

# Check processes
ps aux | grep -E "(mcp_server|cloudflared)"

# Check logs
tail -f data/logs/mcp_server.log
tail -f data/logs/mcp_server_error.log
```

---

## Common Issues

### Issue 1: Server Won't Start

**Symptoms:**
- Error: "Port 8767 already in use"
- Server process not responding

**Solutions:**

1. **Check what's using the port:**
   ```bash
   lsof -i :8767
   ```

2. **Kill existing processes:**
   ```bash
   pkill -f "mcp_server"
   ```

3. **Restart via launchd (macOS production):**
   ```bash
   launchctl unload ~/Library/LaunchAgents/com.unitares.governance-mcp.plist
   launchctl load ~/Library/LaunchAgents/com.unitares.governance-mcp.plist
   ```

4. **Force start:**
   ```bash
   python3 src/mcp_server.py --port 8767 --host 0.0.0.0 --force
   ```

---

### Issue 2: Remote Tunnel Not Connecting

**Symptoms:**
- Remote URL returns connection error
- Tunnel process (`cloudflared`, etc.) logs show failures

**Solutions:**

1. **Check local server is running:**
   ```bash
   curl http://localhost:8767/health
   ```

2. **Check tunnel process logs** for your tunnel provider

3. **Restart the tunnel service** (e.g., via launchd, systemd, or manually)

---

### Issue 3: MCP Tools Not Loading in Client

**Symptoms:**
- Client shows "MCP server not connected"
- Tools don't appear

**Solutions:**

1. **Verify MCP config:**
   ```bash
   # Claude Code
   cat ~/.claude.json | python3 -m json.tool

   # Cursor
   cat ~/.cursor/mcp.json | python3 -m json.tool
   ```

2. **Check server is accessible:**
   ```bash
   curl http://localhost:8767/health
   ```

3. **Restart your client** (Cursor: Cmd+Q then reopen, Claude Desktop: quit and reopen)

4. **Check client logs** for connection errors

---

### Issue 4: Database Connection Errors

**Symptoms:**
- PostgreSQL connection errors
- Error: "Failed to initialize database"

**Solutions:**

1. **Check the configured PostgreSQL target:**
   ```bash
   echo "$DB_POSTGRES_URL"
   pg_isready -d "$DB_POSTGRES_URL"
   psql "$DB_POSTGRES_URL" -c "SELECT 1"
   ```

2. **Start PostgreSQL if not running:**
   ```bash
   brew services start postgresql@17
   ```

3. **Check environment variables:**
   ```bash
   echo $DB_POSTGRES_URL  # Should be set
   ```

4. **Check logs:**
   ```bash
   tail -50 data/logs/mcp_server_error.log
   ```

---

### Issue 5: Redis Connection Errors

**Symptoms:**
- Warning: "Redis unavailable"
- Session binding not persisting across restarts

**Solutions:**

1. **Check Redis:**
   ```bash
   redis-cli ping  # Should return PONG
   ```

2. **Restart Redis:**
   ```bash
   brew services restart redis
   ```

3. **Note:** the server boots without Redis in a degraded local-only mode (in-memory session cache; sessions won't persist across restarts) — but in production Redis is the de-facto primary session store, so treat an unavailable Redis as an incident to fix, not a config option.

---

### Issue 6: High Memory Usage

**Symptoms:**
- Server becomes slow
- High memory consumption

**Solutions:**

1. **Check memory usage:**
   ```bash
   ps aux | grep mcp_server | awk '{print $4, $11}'
   ```

2. **Restart server:**
   ```bash
   launchctl unload ~/Library/LaunchAgents/com.unitares.governance-mcp.plist
   launchctl load ~/Library/LaunchAgents/com.unitares.governance-mcp.plist
   ```

---

### Issue 7: Checked-In Agent Not Visible on Dashboard

**Symptoms:**
- `identity()` or `process_agent_update()` succeeds.
- `observe(action='agent', target_agent_id='<uuid>')` returns current state.
- The browser dashboard does not show the agent.

**Most common causes:**
- Agent search box still contains a previous query.
- Status filter is not set to `All`.
- Metrics-only, production-only, trust-tier, or pagination state is hiding the row.
- Browser cache/state is stale after a dashboard refresh.
- The browser is pointed at a different server instance than the MCP session.

**Verify backend ingestion first:**

```bash
curl -s -X POST http://127.0.0.1:8767/v1/tools/call \
  -H "Authorization: Bearer $UNITARES_HTTP_API_TOKEN" \
  -H "Content-Type: application/json" \
  --data '{"name":"agent","arguments":{"action":"list","include_metrics":true,"recent_days":30,"limit":200,"min_updates":0,"status_filter":"all"}}'
```

Search the response for the agent UUID or label. If present, the backend and Postgres state are good; clear dashboard filters and hard refresh the browser.

If the agent is absent from the API response but `observe()` works, check whether the dashboard's HTTP server and the MCP transport are the same running instance:

```bash
curl http://127.0.0.1:8767/health
lsof -nP -iTCP:8767 -sTCP:LISTEN
launchctl print gui/501/com.unitares.governance-mcp
```

Also verify the shell token matches the running service token. Launchd deployments may use the token from `~/Library/LaunchAgents/com.unitares.governance-mcp.plist`, not the repo-local `.env`.

---

### Issue 8: Check-In (or Other Write Tool) Fails From a Web / Streamable-HTTP Session

**Symptoms:**
- A governance tool call fails with:
  ```
  Streamable HTTP error: Error POSTing to endpoint: MCP tool call requires approval
  ```
- It fails on every retry, even though a matching `permissions.allow` entry exists in `.claude/settings.local.json`.
- Read-only / non-gated tools (e.g. `onboard`) succeed on the **same** connection, so the MCP server and transport are healthy.
- Most often hits the stateful/write tools (`process_agent_update` / check-ins, operator actions) while reads pass.

**Cause:** This is a Claude Code **harness / remote-transport** limitation, *not* a UNITARES server defect — the governance server processes check-ins normally (residents and local/plugin clients are unaffected). When an MCP tool requires per-call approval, the **streamable HTTP transport in the web/remote environment has no way to surface and resolve the approval prompt**, so the tool-call POST hard-errors instead of pausing for approval. The gate is enforced at the **remote environment's MCP gateway, above Claude Code's local permission engine** — confirmed empirically: the call still returns `requires approval` even with `permissions.defaultMode: "bypassPermissions"` and an `mcp__<server>__*` wildcard set in `.claude/settings.local.json`. No repo-local setting overrides it. Two further facts make a settings-based workaround impossible even in principle:
- environment-injected MCP servers have **ephemeral per-session IDs** (`mcp__<uuid>__…`), so an allow rule can't persist across sessions;
- allow rules can't wildcard the server-name segment (`mcp__*__tool` is invalid), so there's no stable rule to write.

The only place to "allow all" for the injected server is the **environment's permission policy** (chosen when the environment is created — see https://code.claude.com/docs/en/claude-code-on-the-web), not anything in the repo.

**Workarounds:**

1. **Use a transport that supports approval / pre-exemption.** Local stdio or direct-loopback MCP (the plugin / `~/.claude.json` config against `http://localhost:8767`) handles the approval handshake. The `unitares-governance-plugin` session-start path calls the governance REST API directly and is unaffected.
2. **Drive check-ins over REST instead of the gated MCP tool** when in a web session:
   ```bash
   curl -s -X POST http://127.0.0.1:8767/v1/tools/call \
     -H "Authorization: Bearer $UNITARES_HTTP_API_TOKEN" \
     -H "Content-Type: application/json" \
     --data '{"name":"process_agent_update","arguments":{"client_session_id":"<sid>","response_text":"...","complexity":0.5}}'
   ```
3. **Pre-exempt the tool at the environment's MCP gateway** (the same mechanism that already lets `onboard` through), if you control the remote environment config.
4. **Report it upstream** via `/feedback` in your Claude Code client — it is a transport-layer bug (approval handshake unsupported over streamable HTTP), not specific to this repo.

**Note on governance coverage:** because of this, agents working in web/streamable-HTTP sessions **cannot complete check-ins through the MCP tool** and will run ungoverned unless they fall back to REST or a local transport. Onboard in-conversation (there is no plugin hook to auto-onboard a server-only/web session — that is the expected default), and use a REST/local path for the check-in loop.

---

## Debugging Steps

### Step 1: Check Basic Connectivity

```bash
# Server health
curl http://localhost:8767/health

# Dashboard
curl http://localhost:8767/dashboard | head -20
```

### Step 2: Check Processes

```bash
# List all related processes
ps aux | grep -E "(mcp_server|cloudflared|python.*governance)"

# Check port usage
lsof -i :8767
```

### Step 3: Check Logs

```bash
# Server logs
tail -50 data/logs/mcp_server.log

# Error logs
tail -50 data/logs/mcp_server_error.log

# Look for errors
grep -i error data/logs/mcp_server.log | tail -20
```

### Step 4: Verify Configuration

```bash
# MCP config
cat ~/.claude.json | python3 -m json.tool

# Environment variables
env | grep -E "(DB_|UNITARES_)"

# Python path
which python3
python3 --version
```

---

## Recovery Procedures

### Service Restart

```bash
# macOS launchd (production)
launchctl unload ~/Library/LaunchAgents/com.unitares.governance-mcp.plist
launchctl load ~/Library/LaunchAgents/com.unitares.governance-mcp.plist

# Verify
curl http://localhost:8767/health
```

### Database Reset (DANGEROUS)

**WARNING: This will delete all agent data!**

```bash
# Backup first!
pg_dump "$DB_POSTGRES_URL" > backup_$(date +%Y%m%d).sql

# Reset PostgreSQL (use an admin DB on the same server)
psql "${DB_POSTGRES_ADMIN_URL:-postgresql://postgres:postgres@localhost:5432/postgres}" -c "DROP DATABASE IF EXISTS governance;"
psql "${DB_POSTGRES_ADMIN_URL:-postgresql://postgres:postgres@localhost:5432/postgres}" -c "CREATE DATABASE governance;"

# Restart server (schema auto-creates)
launchctl unload ~/Library/LaunchAgents/com.unitares.governance-mcp.plist
launchctl load ~/Library/LaunchAgents/com.unitares.governance-mcp.plist
```

---

## Getting Help

### Documentation

1. [START_HERE.md](START_HERE.md) — Thin default workflow and doc map
2. [database_architecture.md](../operations/database_architecture.md) — Database details

### Health Monitoring

```bash
# MCP health check tool
curl http://localhost:8767/health | python3 -m json.tool
```

---

*Last updated: June 2026*
