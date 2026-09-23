---
name: unitares-governance
description: Use UNITARES from Hermes Agent for accountable long-running agent work, including identity continuity, state synchronization, evidence, review, outcomes, and reconstruction.
---

# UNITARES Governance for Hermes

UNITARES is a separate accountability runtime. This Hermes plugin connects to it over MCP at the standard local endpoint.

## Start of a new agent process

Call `start_session(force_new=true)` once to mint a fresh process identity. Save the returned `agent_uuid` and `client_session_id` for the life of that running process. Pass `client_session_id` explicitly on later UNITARES calls, especially writes. A transport-inferred binding is not sufficient for writes under strict identity.

Reserve `continuity_token` for an explicit same-process identity rebind; do not attach it to routine calls.

Do not mint a new UNITARES identity just because the user sends another message.

If this process is an intentional handoff from an exited predecessor, declare the parent identity and explicit spawn reason when starting the session.

## Ongoing work

When there is meaningful work to report, call `sync_state(response_text=..., complexity=..., client_session_id=...)`. Do not manufacture a check-in for every message or tool call.

Treat UNITARES state estimates and coherence signals as runtime telemetry, not as an oracle about task correctness or real-world outcomes.

For evidence-bearing work, distinguish what is directly observed from the repository or runtime, what is reported by another system, and what is inferred.

## Discovering the surface

UNITARES may advertise a compact progressive tool surface.

Use `list_tools(lite=true)` to inspect the live contract and capability index, `describe_tool` for full parameter details, and `use_tool` when a capability exists but is omitted from the initial listing.

## Connection troubleshooting

This plugin does not start UNITARES itself. If UNITARES tools are unavailable, verify that the UNITARES server is running and reachable at:

`http://127.0.0.1:8767/mcp/`

If the UNITARES MCP endpoint is configured to require bearer authentication, configure that authenticated MCP connection in Hermes rather than placing credentials in this plugin's checked-in `mcp.json`.
