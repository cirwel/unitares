# Start Here

Status: thin entrypoint kept for compatibility. README is the primary overview; this page exists to point agents and operators at the current workflow and canonical sources without duplicating architecture docs.

## Default Workflow

Use this unless you have a specific reason not to:

1. First run or fresh process: call `start_session(force_new=true)` and save `agent_uuid` / `client_session_id` from the response
2. Fresh process continuing prior work: call `start_session(force_new=true, parent_agent_id=<saved uuid>, spawn_reason="new_session")`
3. Call `sync_state()` after meaningful work
4. Call `check_working_state()` for state
5. Use `identity(agent_uuid=<uuid>, continuity_token=<token>, resume=true)` only for same-owner proof-owned rebinds

Agent-facing workflow aliases are also registered:

| Job | Workflow alias | Canonical tool |
| --- | --- | --- |
| Start working | `start_session(force_new=true, ...)` | `onboard` |
| Check in after meaningful work | `sync_state(response_text=..., complexity=...)` | `process_agent_update` |
| Check current state | `check_working_state()` | `get_governance_metrics` |
| Search shared memory | `search_shared_memory(query=...)` | `knowledge(action="search")` |
| Record a real outcome | `record_result(...)` | `outcome_event` |
| Ask for review | `request_review(issue_description=...)` | `dialectic(action="request")` |

The workflow aliases return the agent-experience envelope, with the full
canonical payload preserved under `raw_governance`.

```python
# First run:
result = start_session(force_new=True)
save_to_file(result["agent_uuid"])

# New process inheriting prior work:
start_session(force_new=True, parent_agent_id=saved_uuid, spawn_reason="new_session")

# After work:
sync_state(response_text="What you did", complexity=0.5)
```

## Identity Rule

UUID is an identity anchor, not proof that the current process owns that identity. Use `parent_agent_id` to declare lineage for a fresh process. Use `continuity_token` only as short-lived ownership proof when rebinding the same UUID.

## What To Trust

When docs disagree, use this order:

1. Runtime code that computes or returns behavior
2. [Canonical Sources](../dev/CANONICAL_SOURCES.md)
3. Live docs such as [README.md](../../README.md) and [UNIFIED_ARCHITECTURE.md](../UNIFIED_ARCHITECTURE.md)
4. Archived docs for historical context only

Important current semantics:

- `response_text` is the primary check-in input
- `complexity` and `confidence` are optional reflective inputs, not the sole substrate
- Behavioral EISV is the primary measurement source for governance policy when its confidence is sufficient
- ODE state is diagnostic/fallback, not the main verdict source
- Governance responses separate measurement (`primary_eisv`, `behavioral_eisv`, `ode_eisv`), policy evaluation (`policy_evaluation`), and actuator state (`enforcement`)

## Read Next

- [README.md](../../README.md): top-level overview and quick start
- [UNIFIED_ARCHITECTURE.md](../UNIFIED_ARCHITECTURE.md): current architecture summary
- [CANONICAL_SOURCES.md](../dev/CANONICAL_SOURCES.md): authority ordering and source-of-truth map
- [OPERATOR_RUNBOOK.md](../operations/OPERATOR_RUNBOOK.md): operational usage and procedures
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md): common issues and fixes

## Why This File Is Short

This file used to be a larger onboarding guide from an earlier MCP/tooling phase. It is intentionally kept small now to avoid duplicated explanations drifting out of sync with the runtime.

**Last Updated:** 2026-06-12 (measurement-policy-enforcement semantics aligned with runtime response layers)
