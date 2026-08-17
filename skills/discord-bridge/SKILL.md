---
name: discord-bridge
description: >
  Use when setting up or operating the UNITARES Discord bridge — a standalone bot that
  surfaces governance events, agent presence, Lumen's state, and explicit operator
  actions as a living Discord server.
last_verified: "2026-08-16"
freshness_days: 14
source_files:
  - unitares-discord-bridge/src/bridge/bot.py
  - unitares-discord-bridge/src/bridge/acks.py
  - unitares-discord-bridge/src/bridge/config.py
  - unitares-discord-bridge/src/bridge/server_setup.py
  - unitares-discord-bridge/src/bridge/hud.py
  - unitares-discord-bridge/src/bridge/lumen.py
  - unitares-discord-bridge/src/bridge/iterations.py
---

# Discord Bridge

## What It Does

The UNITARES Discord bridge is a standalone Python bot (in the
`unitares-discord-bridge` repo) that combines governance REST/MCP polling with a
typed governance WebSocket, and optionally polls Anima. It turns governance
into visible operator surfaces: activity, EISV state, findings, dialectic
facilitation, Lumen sensors/art, and bounded self-iteration attention.

## Operating Layers

The bridge operates across several visible layers:

1. **Activity and signals**: Routine lifecycle/knowledge events are separated from operator-attention events and critical alerts.
2. **HUD**: A live fleet view with agent counts and measured EISV; missing state is rendered as `no state`, never as a seed vector.
3. **Resident findings**: Sentinel and Doctor have dedicated channels by default; other residents fall back to `#residents`.
4. **Lumen**: Optional physical state, drawings, sensor availability, and weekly Q&A digest from Anima.
5. **Self-iteration attention**: Read-only, provenance-labeled proposals/reviews/canaries go to `#lumen-iterations`, with review-ready and recovery-critical mirrors to signals/alerts.
6. **Class routing / violations**: Typed WebSocket events can be mirrored to class-specific channels when enabled.
7. **Phase-B lease transitions**: An optional operator-managed channel receives lease-plane transition events.
8. **Acknowledgements**: Configured reactions emit `bridge.ack` delivery receipts; acknowledgement is never an approval signature.

## Operator Actions and Authority

The bridge is read-heavy. It does not auto-resume agents, auto-approve Lumen
proposals, or treat a Discord reaction as governance authority.

- `/resume` is an explicit human command guarded by Manage Server or the
  `Governance Admin` role and backed by the operator credential.
- A dialectic that reports `dialectic_facilitation_needed` is critical enough to
  page `#alerts`; the bridge does not synthesize the missing human decision.
- Self-iteration attention remains non-authoritative. Critical/recovery routing
  changes visibility only, not proposal state.

The bot best-effort mints its own UNITARES process identity so attributable
writes—commands and acknowledgement receipts—do not masquerade as anonymous
polling.

## Channel Structure

The base structure has three categories and eleven channels. The default
resident split adds `#sentinel` and `#doctor`; optional class routing adds a
`VIOLATIONS` category with one channel per active class.

| Category | Channels | Purpose |
|----------|----------|---------|
| **GOVERNANCE** | activity, signals, alerts, residents, governance-hud; optional resident-specific findings | Core activity and operator attention |
| **LUMEN** | lumen-art, lumen-sensors, lumen-iterations, lumen-digest | Physical state and bounded self-iteration visibility |
| **CONTROL** | commands, audit-log | Slash-command surface and bot audit trail |
| **VIOLATIONS** (optional) | `gov-<class-id>` | Per-class subscriptions when taxonomy routing is enabled |

Channel topics are declared in code and reconciled on startup by default. Set
`BRIDGE_SYNC_CHANNEL_TOPICS=false` to preserve hand-edited topics.

## Configuration

Core environment variables:

| Variable | Description |
|----------|-------------|
| `DISCORD_BOT_TOKEN` | Discord bot token with appropriate permissions |
| `DISCORD_GUILD_ID` | Target Discord server ID |
| `GOVERNANCE_MCP_URL` | Governance base URL (default: `http://localhost:8767`) |
| `ANIMA_MCP_URL` | Optional Anima/Lumen URL; leave unset to omit those live reads |

Important optional configuration:

| Variable | Description |
|----------|-------------|
| `GOVERNANCE_API_TOKEN` | Bearer credential for governance reads |
| `GOVERNANCE_OPERATOR_TOKEN` | Separate operator-tier credential; required for the HUD to resolve real per-agent EISV instead of redacted handles |
| `BRIDGE_RESIDENT_FINDING_CHANNELS` | Dedicated finding channels (default: `sentinel,doctor`; empty restores the shared feed) |
| `LUMEN_SELF_ITERATION_ENABLED` | Enable the read-only attention poller (default: true) |
| `LUMEN_SELF_ITERATION_POLL_INTERVAL` | Attention polling cadence in seconds (default: 60) |
| `LUMEN_OFFLINE_MENTION` | User/role mention used only for Lumen offline and recovery transitions; routine posts stay silent |
| `BRIDGE_ACK_EMOJI` | Comma-separated reactions that count as acknowledgement (default: `✅`) |
| `BRIDGE_ACK_HASH_SALT` | Optional salt for pseudonymous operator hashes; recommended because Discord user IDs are enumerable |

## Running

```bash
# Install
pip install -e .

# Run
python -m bridge.bot
```

The bot creates missing managed channels, reconciles their topics, starts the
available pollers/subscribers, and syncs its slash-command tree. Anima failures
degrade the Lumen surfaces without taking down governance delivery.

## Architecture

Key design decisions:

- **Polling plus event subscription**: The bridge polls MCP/REST surfaces for state and also subscribes to the governance WebSocket for typed events not present in `/api/events`.
- **Read-heavy, write-light**: The bridge reads governance state frequently and writes back only for governed identity, explicit operator commands, and acknowledgement receipts.
- **SQLite cursor-based delivery**: Tracks what has been sent to Discord to avoid duplicate messages. Uses cursors per channel per event type.
- **Rate-limited message queue**: Messages are queued and sent with 150ms spacing to respect Discord rate limits.
- **Stateless restarts**: The bridge can restart cleanly — cursor tracking means it picks up where it left off without replaying history. On a failed event fetch (`fetch_events` returns `None` on error), the poller never resets its cursor — it waits for the next poll, so governance stalls cannot trigger a feed replay to Discord.
- **Liveness heartbeat + external watchdog**: The event loop rewrites a heartbeat file (`BRIDGE_HEARTBEAT_PATH`, default `~/.unitares/discord-bridge.heartbeat`) each poll iteration. The `com.unitares.bridge-liveness-watchdog` LaunchAgent (unitares repo, `scripts/ops/`) uses it to detect a wedged loop — process alive but loop hung — and restarts the bridge, which launchd `KeepAlive` alone cannot catch.
- **Reaction acknowledgements**: A raw reaction listener accepts only configured acknowledgement emoji, ignores the bot's own reactions, joins receipts to deliveries by `discord_message_id`, and sends a hashed operator ID. Receipt delivery is best-effort and never takes down the Discord event loop.
- **Governed identity**: On startup the bridge best-effort mints its own UNITARES identity so polling traffic can be attributed when governance is available.
- **Honest HUD authority**: The operator tier supplies true agent UUIDs for
  metric joins. Without it, the HUD degrades visibly to `no state` rather than
  rendering fabricated defaults.
- **Transition-only outage paging**: Lumen offline/recovery posts may mention a
  configured user or role after consecutive failures; normal sensor and
  self-iteration posts never ping.
