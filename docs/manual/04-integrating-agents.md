# 4 · Integrating agents

[← Running the server](03-running-the-server.md) · [Manual index](README.md) · [Next: Reading the signals →](05-reading-the-signals.md)

This chapter owns the minimal agent workflow. Transport-specific configuration
belongs in [`MCP_CLIENTS.md`](../integration/MCP_CLIENTS.md), and exact runtime
schemas belong to `list_tools()` / `describe_tool()` and
[`src/tool_schemas.py`](../../src/tool_schemas.py).

## 4.1 The default workflow

1. **Fresh process:** `start_session(force_new=true)`; save `agent_uuid` and
   `client_session_id`.
2. **Same running process:** pass `client_session_id` on later check-ins and
   writes.
3. **Real handoff from a finished predecessor:** start fresh and declare
   `parent_agent_id=<prior uuid>, spawn_reason="new_session"`.
4. Call `sync_state(...)` after meaningful work.
5. Call `record_result(...)` when a bounded external outcome exists.
6. Use `check_working_state()` for a read-only state check.

```python
session = start_session(force_new=True)

result = sync_state(
    response_text="Implemented the change and ran the focused tests.",
    complexity=0.5,
    confidence=0.8,
    client_session_id=session["client_session_id"],
)
```

## 4.2 Identity rule

> A UUID is a server record, not proof that the current process owns it.

Reads may work without a bound caller; writes must be accountable. Do not use a
bare UUID as resume proof, treat a display name as identity, pass a continuity
token on every ordinary call, or declare a parent merely because another process
shares the workspace. Full rules: [`identity.md`](../ontology/identity.md).

Short dispatched subagents usually should not onboard. If one genuinely needs
its own identity, declare the dispatcher as parent with
`spawn_reason="subagent"` and leave at least one real check-in.

## 4.3 Primary workflow tools

| Job | Primary tool | Raw implementation |
|---|---|---|
| Start a process session | `start_session(...)` | `onboard` |
| Check in after work | `sync_state(...)` | `process_agent_update` |
| Read current state | `check_working_state()` | `get_governance_metrics` |
| Search shared memory | `search_shared_memory(...)` | `knowledge(action="search")` |
| Record an outcome | `record_result(...)` | `outcome_event` |
| Request review | `request_review(...)` | `dialectic(action="request")` |

The primary tools return a compact agent-facing envelope. State-changing tools
preserve the raw payload under `raw_governance`; read aliases omit that repeated
payload by default and expose a full-mode escape hatch. Call `list_tools()` for
the current full surface rather than relying on a copied catalog in prose.

For `sync_state`, read `action_summary` first. It keeps the policy action,
one-line reason, risk score, and verdict maturity together; a cold-start result
is labeled `verdict_confidence="provisional"` at that surface. If the response
also contains `legacy_diagnostics`, treat that block as compatibility telemetry,
not behavioral health evidence. `response_options` documents the routine,
actionable, and complete modes in-band, while `_response_size` reports the
approximate serialized size and suggests a smaller mode when the payload is
large.

For routine `proceed` / `approve` check-ins, `compact` omits the duplicated
`policy_evaluation` and advisory-only `enforcement` blocks. A guide, pause,
reject, suppression, or actuator event restores bounded summaries marked
`_detail_level="summary"`; use `full` for the self-contained maturity gates and
audit diagnostics. `minimal` remains the smallest legacy shape and `standard`
the interpreted legacy shape, but both still retain the normalized action and
cold-start verdict caveat. Compatibility aliases are exact: `lite=compact`,
`verbose=full`, and `interpreted=standard`.

`search_shared_memory` uses its compact envelope as a discovery digest. Its
`memory_suggestions` retain lifecycle metadata and bounded detail previews;
`discovery_retrieval_options` shows how to open one record or deliberately
expand all results. Expanding every result through the friendly alias requires
both `response_mode="full"` and `include_details=true`; compact mode remains a
digest even if details were requested and reports that downgrade explicitly.

## 4.4 Handle the policy response

```python
action = result.get("action_summary", {}).get("action")
if action == "pause":
    agent.require_human_review(result.get("next_action"))
```

Treat the response as a policy affordance, not a moral judgment or outcome
label. For finer client behavior, read the E/I/S/V fields and the named risk
components. Their meaning and maturity stages are in
[Reading the signals](05-reading-the-signals.md).

## 4.5 Connect a client or resident

- MCP client, hosted connector, authentication, and remote transport matrix:
  [`MCP_CLIENTS.md`](../integration/MCP_CLIENTS.md).
- Long-running or scheduled agents: use the
  [`unitares-sdk`](../../agents/sdk/README.md) and its `GovernanceAgent` pattern.
- Codex and Claude Code lifecycle hooks: use the
  [governance plugin](https://github.com/cirwel/unitares-governance-plugin).
- Other model hosts and thin clients: use the
  [host adapter](https://github.com/cirwel/unitares-host-adapter).

---

[← Running the server](03-running-the-server.md) · [Manual index](README.md) · [Next: Reading the signals →](05-reading-the-signals.md)
