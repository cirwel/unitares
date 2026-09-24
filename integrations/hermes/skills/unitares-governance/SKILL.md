---
name: unitares-governance
description: Use UNITARES from Hermes Agent for accountable long-running agent work, including identity continuity, state synchronization, evidence, review, outcomes, and reconstruction.
---

# UNITARES Governance for Hermes

UNITARES is a separate accountability runtime. This Hermes plugin connects to it over MCP at the standard local endpoint.

## Dispatched subagents

Short dispatched subagents usually do not onboard. Give the driver their work so it can report through its own session.

If a dispatched subagent needs its own identity, call `start_session(force_new=true, parent_agent_id=<driver_uuid>, spawn_reason="subagent")` using the driver's UUID from the dispatch context. Make at least one meaningful `sync_state(response_text=..., complexity=..., client_session_id=<returned_client_session_id>)` call before exit. Do not infer a parent from a shared machine or workspace.

## Persistent residents

A persistent or substrate Hermes resident uses its deployment's dedicated identity pattern and anchor across restarts. Do not apply the ordinary-session fresh identity rule to each restart. This portable plugin does not configure a resident identity; follow that deployment's identity instructions.

## Ordinary agent sessions

For a new ordinary agent process that is neither a dispatched subagent nor a persistent resident, call `start_session(force_new=true)` once to mint a fresh process identity. Save the returned `agent_uuid` and `client_session_id` for the life of that running process. Pass `client_session_id` explicitly on later UNITARES calls, especially writes. A transport-inferred binding is not sufficient for writes under strict identity.

Reserve `continuity_token` for an explicit same-process identity rebind; do not attach it to routine calls.

Do not mint a new UNITARES identity just because the user sends another message.

If this process is an intentional handoff from an exited predecessor, pass its UUID as `parent_agent_id` and use `spawn_reason="explicit"` when starting the session.

## Ongoing work

When there is meaningful work to report, call `sync_state(response_text=..., complexity=..., client_session_id=...)`. Do not manufacture a check-in for every message or tool call.

When you know them, include the model provider and model in `sync_state`'s `provenance_context`, for example `provenance_context={"model_provider": "anthropic", "model": "<model id>"}` or `{"model_provider": "openai-codex", "model": "<model id>"}`. The harness is Hermes whatever provider you run on. Do not put the provider name in `harness_type`: provider ids such as `openai-codex` or `claude` would be read as a different harness. The plugin already declares the harness.

Treat UNITARES state estimates and coherence signals as runtime telemetry, not as an oracle about task correctness or real-world outcomes.

For evidence-bearing work, distinguish what is directly observed from the repository or runtime, what is reported by another system, and what is inferred.

## Discovering the surface

UNITARES may advertise a compact progressive tool surface.

Use `list_tools(lite=true)` to inspect the live contract and capability index, `describe_tool` for full parameter details, and `use_tool` when a capability exists but is omitted from the initial listing.

## Connection troubleshooting

This plugin does not start UNITARES itself. If UNITARES tools are unavailable, verify that the UNITARES server is running and reachable at:

`http://127.0.0.1:8767/mcp/`

If the UNITARES MCP endpoint is configured to require bearer authentication, configure that authenticated MCP connection in Hermes rather than placing credentials in this plugin's checked-in `mcp.json`.
