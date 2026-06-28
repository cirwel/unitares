---
name: discord-bridge
description: >
  Use when setting up or operating the UNITARES Discord bridge — a standalone bot that
  surfaces governance events, agent presence, Lumen's state, and autonomous governance
  actions as a living Discord server.
last_verified: "2026-06-27"
freshness_days: 14
source_files:
  - unitares-discord-bridge/src/bridge/bot.py
---

# Discord Bridge

## What It Does

The UNITARES Discord bridge is a standalone Python bot (in the `unitares-discord-bridge` repo) that polls both the governance MCP server and the anima MCP server, then forwards events and state to a Discord server. It turns governance into something visible — agent presence, EISV changes, dialectic sessions, knowledge graph updates, and Lumen's physical state all appear as Discord messages and embeds.

## Operating Layers

The bridge operates across several visible layers:

1. **Events**: Governance events (verdicts, state changes, alerts) forwarded to Discord channels
2. **HUD**: Heads-up display with current system state, agent counts, risk levels
3. **Presence**: Agent online/offline status, activity indicators, EISV summaries
4. **Lumen**: Physical state from anima-mcp — temperature, humidity, light, neural bands, drawing state
5. **Dialectic**: Active dialectic sessions surfaced as threads with thesis/antithesis/synthesis
6. **Knowledge**: New discoveries and updates from the knowledge graph
7. **Class routing / violations**: WebSocket governance events routed to class-specific channels when enabled
8. **Phase-B lease transitions**: Optional operator-managed channel for lease-plane transition events
9. **Lumen digest**: Optional weekly Q&A / connection-signal digest

## Autonomous Governance

The bridge can take autonomous governance actions, but only in response to governance events:

- **Auto-resume**: When an agent's EISV recovers past threshold, the bridge can trigger resume
- **Auto-dialectic**: On pause/reject verdicts, the bridge can initiate a dialectic session
- **Neighbor warnings**: When one agent enters high risk, nearby agents get notified

The bridge never modifies governance state unprompted. Autonomous actions only fire when governance events trigger them (pause, reject, critical drift, high risk score).

## Channel Structure

The Discord server is organized into 5 categories with 13+ channels and 2 forum channels:

| Category | Channels | Purpose |
|----------|----------|---------|
| **GOVERNANCE** | events, verdicts, alerts, system-hud | Core governance activity |
| **AGENTS** | presence, check-ins, agent-detail | Agent lifecycle and status |
| **LUMEN** | state, sensors, drawings, neural | Lumen's physical and computational state |
| **KNOWLEDGE** | discoveries, graph-updates, search | Knowledge graph activity |
| **CONTROL** | commands, config, audit-log | Bot configuration and audit trail |

Forum channels are used for dialectic sessions (one thread per session) and knowledge discussions.

## Configuration

Required environment variables:

| Variable | Description |
|----------|-------------|
| `DISCORD_BOT_TOKEN` | Discord bot token with appropriate permissions |
| `DISCORD_GUILD_ID` | Target Discord server ID |
| `GOVERNANCE_MCP_URL` | Governance MCP endpoint (default: `http://localhost:8767/mcp/`) |
| `ANIMA_MCP_URL` | Anima MCP endpoint (default: `http://<pi-tailscale-ip>:8766/mcp/` — get IP from `tailscale status`) |

## Running

```bash
# Install
pip install -e .

# Run
python -m bridge.bot
```

The bot will create missing channels on first startup and begin polling both MCP servers.

## Architecture

Key design decisions:

- **Polling plus event subscription**: The bridge polls MCP/REST surfaces for state and also subscribes to the governance WebSocket for typed events not present in `/api/events`.
- **Read-heavy, write-light**: The bridge reads governance state frequently but writes back rarely (only autonomous actions).
- **SQLite cursor-based delivery**: Tracks what has been sent to Discord to avoid duplicate messages. Uses cursors per channel per event type.
- **Rate-limited message queue**: Messages are queued and sent with 150ms spacing to respect Discord rate limits.
- **Stateless restarts**: The bridge can restart cleanly — cursor tracking means it picks up where it left off without replaying history.
- **Governed identity**: On startup the bridge best-effort mints its own UNITARES identity so polling traffic can be attributed when governance is available.
