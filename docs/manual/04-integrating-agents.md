# 4 · Integrating agents

[← Running the server](03-running-the-server.md) · [Manual index](README.md) · [Next: Reading the signals →](05-reading-the-signals.md)

This chapter is for the **integrator** — you have a running server and you want an agent (or an MCP client) governed by it.

## 4.1 The default workflow

Use this unless you have a specific reason not to ([`../guides/START_HERE.md`](../guides/START_HERE.md)):

1. **First run / fresh process:** call `start_session(force_new=true)`; save `agent_uuid` and `client_session_id` from the response.
2. **Same running process:** pass `client_session_id` on later check-ins and writes.
3. **Fresh process continuing prior work:** `start_session(force_new=true, parent_agent_id=<saved uuid>, spawn_reason="new_session")` — only for a real handoff from a *finished* predecessor.
4. Call `sync_state(...)` after each meaningful unit of work.
5. Call `check_working_state()` to read current state without updating it.

```python
# First run:
result = start_session(force_new=True)
agent_uuid = result["agent_uuid"]
session_id = result["client_session_id"]

# After work:
sync_state(response_text="What you did", complexity=0.5, confidence=0.8,
           client_session_id=session_id)
```

## 4.2 Pointing a client at the server

**Cursor / Claude Code** (native `type: http`):

```json
{ "mcpServers": { "unitares": { "type": "http", "url": "http://localhost:8767/mcp/" } } }
```

**Claude Desktop** (no native HTTP; bridge via `mcp-remote`):

```json
{ "mcpServers": { "unitares": { "command": "npx", "args": ["mcp-remote", "http://localhost:8767/mcp/"] } } }
```

Full client matrix and remote-connector auth: [`../integration/MCP_CLIENTS.md`](../integration/MCP_CLIENTS.md).

## 4.3 Identity: the one rule that matters

> **UUID is a server record, not proof that the current process owns that identity.** Use `client_session_id` for writes in the same running process. Use `parent_agent_id` only to declare a real handoff into a fresh process.

Identity is a **write gate**: reads may work without a bound caller, but writes must be accountable. A fresh process mints a fresh UUID; cross-process continuity is *declared and verified*, never silently inherited.

**Do not** do these in normal agent code:

- Bare `onboard()` / `identity()` as a way to *guess* identity.
- Passing `continuity_token` on every call (it's an advanced same-live-process rebind proof, not part of the normal loop).
- Treating a display name as identity.
- Declaring `parent_agent_id` just because another session shares the workspace.

Short dispatched subagents usually should **not** onboard. If one genuinely needs its own identity, use `spawn_reason="subagent"`, set `parent_agent_id=<driver_uuid>`, and land at least one real `sync_state()` before exit. Full model: [`../ontology/identity.md`](../ontology/identity.md).

## 4.4 Primary tools vs. raw implementation tools

Friendly workflow aliases wrap the raw tools and return an agent-experience envelope (with the full raw payload preserved under `raw_governance`). Either name works; prefer the primary.

| Job | Primary workflow tool | Raw implementation tool |
|---|---|---|
| Start working | `start_session(force_new=true, ...)` | `onboard` |
| Check in after work | `sync_state(response_text=..., complexity=...)` | `process_agent_update` |
| Check current state | `check_working_state()` | `get_governance_metrics` |
| Search shared memory | `search_shared_memory(query=...)` | `knowledge(action="search")` |
| Record a real outcome | `record_result(...)` | `outcome_event` |
| Ask for review | `request_review(issue_description=...)` | `dialectic(action="request")` |

## 4.5 The full agent-facing tool surface

The server exposes ~50 tools; most are consolidated behind an `action` parameter. The ones you'll actually use:

### Session & identity
- **`onboard`** / `start_session` — register a fresh process-instance. Key params: `name`, `model_type`, `force_new`, `parent_agent_id`, `spawn_reason`, `initial_state`. Returns `agent_uuid` + `client_session_id`.
- **`identity`** — "who am I?" Check the current binding or set a display name. Same-owner rebind: `identity(agent_uuid=..., continuity_token=..., resume=true)` — never bare `identity(agent_uuid=..., resume=true)` (UUID alone is an unsigned claim).
- **`bind_session`** — bind a session to an existing identity (cross-process anti-hijack gate).

### Check-in & metrics
- **`process_agent_update`** / `sync_state` — the main check-in. Key params: `response_text`, `complexity` [0–1], `confidence` [0–1], `ethical_drift` ([float,float,float]), `recent_tool_results` (array of `{tool, summary, is_bad}`), `response_mode` (`minimal`/`compact`/`standard`/`full`/`mirror`/`auto`). Returns the verdict + EISV state, risk, coherence, margin.
- **`get_governance_metrics`** / `check_working_state` — read-only snapshot, no state update. Fleet-scoped read when unbound.

### Knowledge graph
- **`knowledge`** — unified KG tool. `action` ∈ `store` / `search` / `get` / `list` / `update` / `details` / `note` / `cleanup` / `synthesize` / `stats` / `supersede` / `audit`.
- **`leave_note`** — shorthand for `knowledge(action="note")`. Param: `summary` (required), `content`, `discovery_id`.

### Outcomes, review, recovery
- **`outcome_event`** / `record_result` — record a bounded outcome to improve calibration and verify a prior check-in's prediction. Key params: `outcome_type`, `decision_action`, `verification_source` (`agent_reported_tool_result` / `server_observation` / `external_signal`).
- **`dialectic`** / `request_review` — unified peer-review/recovery. `action` ∈ `get` / `list` / `quick` / `request` / `thesis` / `antithesis` / `synthesis` / `reassign`.
- **`self_recovery`** — self-diagnosed recovery after a pause. Params: `reflection` (required), `mode` (`quick_resume` / `self_recovery_review`).

### Observability, admin, lifecycle (mostly operator/diagnostic)
- **`observe`** — `action` ∈ `agent` / `compare` / `similar` / `anomalies` / `aggregate` / `telemetry` / `audit_events` / `outcome_evidence`.
- **`health_check`** — quick system health (status, version, component checks).
- **`calibration`** — `action` ∈ `check` / `update` / `backfill` / `rebuild`.
- **`config`** — get/set governance thresholds (reads unbound; writes identity-gated).
- **`agent`** — lifecycle: `list` / `get` / `update` / `archive` / `resume` / `delete`.
- **`export`** — `history` or `file`.
- **`list_tools`** / **`describe_tool`** — discover the surface (accessible pre-onboard).

Definitive schemas live in [`src/tool_schemas.py`](../../src/tool_schemas.py) and `src/mcp_handlers/schemas/`. At runtime, call `list_tools()` / `describe_tool(tool_name=...)`.

## 4.6 Handling the verdict

The minimal contract: read `verdict` and course-correct.

```python
result = sync_state(response_text=output, complexity=0.6, confidence=0.8,
                    client_session_id=session_id)
verdict = result.get("verdict", {}).get("value")

if verdict in ("pause", "reject"):
    agent.require_human_review(result["verdict"]["next_action"])
```

For per-dimension policies, branch on the EISV components instead of the single verdict:

```python
eisv = result.get("raw_governance", result).get("primary_eisv", {})
if eisv.get("I", 1) < 0.4:
    agent.require_human_review("integrity low — pausing autonomous actions")
elif eisv.get("S", 0) > 0.7:
    agent.narrow_scope()         # fewer tools, tighter search
elif eisv.get("E", 1) < 0.2:
    agent.stop_and_summarize()   # avoid thrashing
```

What each verdict and dimension *means* is [chapter 5](05-reading-the-signals.md).

## 4.7 Long-running / scheduled agents — the SDK

For daemons and cron-style agents, the **`unitares-sdk`** ([`../../agents/sdk/README.md`](../../agents/sdk/README.md)) handles connection, identity resolution, check-ins, heartbeats, log rotation, and state persistence so you don't re-implement the loop.

- Subclass **`GovernanceAgent`** and override `run_cycle(client)` (required). Optional hooks: `on_after_checkin(...)`, `on_verdict_pause(...)` (return `True` to retry the check-in after self-recovery).
- **Daemon:** `agent.run_forever(interval=60)` loops with idle heartbeats. **Scheduled:** `agent.run_once()` for one cycle under launchd/systemd/cron.
- Identity anchors persist at `~/.unitares/anchors/<name>.json`; agent state via `save_state(dict)` / `load_state()`.
- Constructor knobs: `name`, `mcp_url` (default `http://127.0.0.1:8767/mcp/`), `persistent`, `refuse_fresh_onboard`, `cycle_timeout_seconds`, `parent_agent_id`, `spawn_reason`.

The resident agents (Vigil, Sentinel, Chronicler) in [`../../agents/`](../../agents/) are reference implementations of these patterns.

## 4.8 Mounting an existing agent without code changes

If you don't want to edit the agent's loop at all, the [governance plugin](https://github.com/cirwel/unitares-governance-plugin) wires check-ins, dialectic review, and verdicts into Claude Code / Codex via hooks, and the [host adapter](https://github.com/cirwel/unitares-host-adapter) provides thin bindings for Hermes, Goose, and arbitrary OpenAI-compatible clients.

---

[← Running the server](03-running-the-server.md) · [Manual index](README.md) · [Next: Reading the signals →](05-reading-the-signals.md)
