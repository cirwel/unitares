# 4 · Integrating agents

[← Running the server](03-running-the-server.md) · [Manual index](README.md) · [Next: Reading the record →](05-reading-the-signals.md)

This chapter owns the minimal agent workflow. Transport-specific configuration
belongs in [`MCP_CLIENTS.md`](../integration/MCP_CLIENTS.md), and exact runtime
schemas belong to `list_tools()` / `describe_tool()` and
[`src/tool_schemas.py`](../../src/tool_schemas.py).

For the 15-second bootstrap before the details below, read the
[six-rule field guide](../../skills/governance-lifecycle/SKILL.md#dont-panic-six-rules).
It is part of the server-authored lifecycle skill, so agents and human
integrators read the same operational rules.

## 4.1 The default workflow

1. **Fresh process:** `start_session(force_new=true)`; save `agent_uuid` and
   `client_session_id`.
2. **Same running process:** pass `client_session_id` on later check-ins and
   writes.
3. **Real handoff from a finished predecessor:** start fresh and declare
   `parent_agent_id=<prior uuid>, spawn_reason="explicit"`.
4. Call `sync_state(...)` after meaningful work.
5. Call `record_result(...)` when a bounded external outcome exists. To grade
   a specific check-in, pass the `prediction_id` that check-in returned;
   `sync_state` issues one only when you pass `confidence`, and it expires, so
   record the outcome while the work is fresh.
6. Use `check_working_state()` for a read-only state check. Calling it once with
   `client_session_id` confirms threading: it should return your `agent_uuid`.

`search_shared_memory` is a read and works before `start_session`, so search
first.

```python
session = start_session(force_new=True)

result = sync_state(
    response_text="Implemented the change and ran the focused tests.",
    complexity=0.5,
    confidence=0.8,
    client_session_id=session["client_session_id"],
)
```

For a complete two-process session captured from a fresh install, with every
call and the server's responses, see the
[agent session walkthrough](../guides/AGENT_SESSION_WALKTHROUGH.md).

## 4.2 Identity rule

> A UUID is a server record, not proof that the current process owns it.

Reads may work without a bound caller; writes must be accountable. Do not use a
bare UUID as resume proof, treat a display name as identity, pass a continuity
token on every ordinary call, or declare a parent merely because another process
shares the workspace. Do not call bare `identity()` or `onboard()` to find out
who you are: with no arguments they resolve whatever the transport suggests,
which proves no ownership. Full rules: [`identity.md`](../ontology/identity.md).

Short dispatched subagents usually should not onboard. If one genuinely needs
its own identity, declare the dispatcher as parent with
`spawn_reason="subagent"` and leave at least one real check-in.

## 4.3 Primary workflow tools

By default the server advertises a small initial `tools/list`: `start_session`,
`identity`, `sync_state`, `check_working_state`, `record_result`,
`search_shared_memory`, `store_finding`, `request_review`, `consult`,
`self_recovery`, `list_tools`, `describe_tool` and `use_tool`. Every other
capability stays callable through three steps: `list_tools(lite=true)` reports
every capability name and the contract version (interface contract 1.13.0 and
later), `describe_tool(tool_name=..., action=...)` gives the full parameters,
and `use_tool(tool_name=..., arguments={...})` invokes it. Each action still
enforces its own authorization and identity requirements. Set
`UNITARES_TOOL_ADVERTISEMENT=full` when a client needs every schema up front;
legacy `GOVERNANCE_TOOL_MODE` values are ignored.

| Job | Primary tool | Canonical implementation (via `use_tool` on the default surface) |
|---|---|---|
| Start a process session | `start_session(...)` | `onboard` |
| Check in after work | `sync_state(...)` | `process_agent_update` |
| Read current state | `check_working_state()` | `get_governance_metrics` |
| Search shared memory | `search_shared_memory(...)` | `knowledge(action="search")` |
| Write a finding | `store_finding(...)` | `knowledge(action="store")` |
| Revise a finding | `update_finding(...)` (via `use_tool`) | `knowledge(action="update")` |
| Record an outcome | `record_result(...)` | `outcome_event` |
| Request review | `request_review(...)` | `dialectic(action="request")` |
| Recover from a pause | `self_recovery(...)` | — |

Canonical implementations remain callable for independently upgraded clients.
On the default surface, a form such as `knowledge(action="details", ...)` below
means `use_tool(tool_name="knowledge", arguments={"action": "details", ...})`;
the server writes its own hints in that form.

The primary tools return a compact agent-facing envelope. Read aliases,
bounded `sync_state` modes, and the write acknowledgements of `store_finding`,
`update_finding` and `record_result` omit the repeated raw payload by default,
as does a plain fresh `start_session` (pass `response_mode="full"` on the mint
to keep it) and a routine `request_review`: one whose review opened and now
waits on a known move (`response_shape: "routine"`, with `state_summary`
keeping `session_id`, `phase` and `whose_move`). A review ack that carries the
review itself (a verdict, recusal, facilitation request or reviewer-host limit)
keeps the raw payload, with `response_shape: "full"` and a
`response_shape_reason` naming what kept it. Other state-changing tools retain
it. `raw_governance_hint` names where to read more; on a routine
`request_review` that is `dialectic(action="get", session_id=...)`, which
returns the session as it stands later, not the ack. That is `response_mode="full"` on `sync_state`,
`search_shared_memory` and `record_result`, and `verbosity="full"` on
`check_working_state`. `record_result` has no read by outcome id. For an
outcome recorded with a `prediction_id`, repeating the identical call under the
same identity (pass `client_session_id`) with
`response_mode="full"` returns the stored outcome in full
(`idempotent_replay: true`) and records no second outcome while the binding is
retained; a changed outcome is refused with `PREDICTION_REUSE_CONFLICT`.
Without a `prediction_id` a repeat records a second outcome, so the option
applies only to a later outcome and the ack's own payload cannot be fetched
again. For `store_finding` and `update_finding`, where `response_mode` does not
apply, it is a `knowledge(action="details", discovery_id=...)` read, which
returns the stored record rather than the ack's payload. No read returns the
omitted payload, so these three write acks do not set
`raw_governance_available`, and neither does a bounded `sync_state`: no read
returns a check-in's own decision payload, and `response_mode="full"` applies
to the next check-in, which writes one. Write-time warnings and a bounded
`related_discoveries` snapshot stay in the ack because that read does not
return the warnings or the snapshot's summary previews; the snapshot's ids are
the stored record's `related_to`, so a `store_finding` ack that carries the
snapshot does not repeat them as `state_summary.related_to`. The canonical
`knowledge` tool returns their whole payload directly. These three write
acknowledgements keep the ids a caller needs next (`discovery_id`,
`state_summary.outcome_id`). The finding writes also carry `agent_uuid` and
`written_as`, the writer's `agent_id`, `display_name` and assurance tier, so a
caller can see which identity a write was recorded under; `record_result`
carries them only when its binding was not server-inferred (for example, the
call passed `client_session_id`), because `outcome_event` signs nothing for a
server-inferred binding.

For `sync_state`, read `action_summary` first. It always keeps the effective action,
one-line reason and risk score. A non-routine response also keeps the verdict
and its maturity; a cold-start result is labeled
`verdict_confidence="provisional"` at that surface. A routine proceed instead
sets `response_shape="routine"` and omits the repeated safe verdict plus its
unspecified maturity. If the response contains `legacy_diagnostics`, treat that
block as compatibility telemetry, not behavioral health evidence. Routine
bounded responses omit repeated mode coaching and size receipts;
`response_mode="full"` restores the canonical payload and its diagnostics. The
friendly `state_summary` does not carry the all-clear band `status: "healthy"`:
read the risk from `risk_score` and `risk_summary`. A `moderate` or `critical`
band is still shown, and `raw_governance` keeps the canonical value.

To learn whether you are paused before writing anything, call
`check_working_state`. Its `action_summary` carries `paused` and
`writes_held`, read from the same lifecycle state the write gates use, and
`held` says which writes are refused when they are. This works for a fresh
identity that has not checked in yet.

`sync_state` defaults to `response_mode="auto"`, which resolves to `compact`
for a steady check-in and to `mirror` (actionable self-signals) for a guide,
pause, at-risk, degraded-identity or warning-bearing response.

For routine `proceed` / `approve` check-ins, `compact` omits the duplicated
`policy_evaluation` and advisory-only `enforcement` blocks. A guide, raw pause
recommendation, reject, suppression, or actuator event restores bounded summaries marked
`_detail_level="summary"`; use `full` for the self-contained maturity gates and
audit diagnostics. `minimal` remains the smallest legacy shape and `standard`
is a bounded interpreted summary for agents: action/reason, explained state and
verdict provenance, EISV/risk, and only actionable policy/enforcement summaries.
Both retain the effective action and cold-start verdict caveat. In the default
`observe` posture, clients must read `policy_evaluation.action` for the raw
recommendation and `enforcement.applied` for actual authority; the effective
`decision.action` stays non-coercive so older clients do not halt on an
observation. Compatibility
aliases are exact: `lite=compact`, `verbose=full`, and `interpreted=standard`.

A consolidated router (`knowledge`, `dialectic`, `observe`, `agent`, and the
rest) advertises one flat schema covering every action it routes, because the
MCP wrapper builds a tool's argument model from top-level properties. That
union is not what a single call takes: `knowledge` advertises every parameter
of every action it routes, whichever one you mean. Pass the action to
`describe_tool` to see only that action's parameters:

```
describe_tool(tool_name="knowledge", action="search")
```

Without an action, the response lists the actions the tool routes. The
narrowed schema describes one call; the wire is unchanged, so parameters
belonging to other actions are still accepted and ignored.

`search_shared_memory` defaults to a lean discovery digest. Its
`memory_suggestions` retain ids, one-line summaries, lifecycle/type metadata,
bounded tags, and one relevance score; detail previews and score maps are omitted.
Where the AGE supersession edge exists, a superseded result names its
replacement as `superseded_by`. An open result last written more than 60 days
ago carries `last_activity_days`, which `state_summary.staleness_note` explains
once per response.
`discovery_retrieval_options` shows how to open one record or deliberately
expand all results. Expanding every result through the friendly alias requires
both `response_mode="full"` and `include_details=true`; compact mode remains a
digest even if details were requested. It forces digest retrieval before the
canonical search serializes results, and reports that policy explicitly.

`sync_state` is pull-based with respect to shared memory: it performs no KG
lookup unless the caller sets `include_memory_suggestions=true`. Prefer an
explicit `search_shared_memory` call when recall is the task rather than a side
effect of governance check-in.

## 4.4 Handle the policy response

```python
summary = result.get("action_summary", {})
if summary.get("action") == "pause":
    # Governed writes are held. Read the reason and next_action, then try
    # self_recovery(action="check"); a dialectic review, an operator, or the
    # pause's expiry also lift it.
    handle_pause(summary.get("reason"), result.get("next_action"))
elif summary.get("sub_action") == "guide":
    adjust(summary.get("reason"))  # advisory; work may continue
```

`action_summary.action` is always `proceed` or `pause`. A guide verdict arrives
as `proceed` with `sub_action: "guide"`, and a reject-severity decision as
`pause` with `sub_action: "reject"`.

Treat the response as a policy affordance, not a moral judgment or outcome
label. For finer client behavior, read the E/I/S/V fields and the named risk
components. Their meaning and maturity stages are in
[the EISV instrument](05-reading-the-signals.md#55-the-eisv-instrument) in chapter 5.

## 4.5 Connect a client or resident

- MCP client, hosted connector, authentication, and remote transport matrix:
  [`MCP_CLIENTS.md`](../integration/MCP_CLIENTS.md) (Streamable HTTP `/mcp/`,
  `npx mcp-remote` stdio bridge, and Bearer or OAuth 2.1 on a dedicated
  `UNITARES_OAUTH_PUBLIC_PORT` listener for hosted connectors like Claude.ai,
  ChatGPT, and Perplexity).
- Long-running or scheduled agents: use the
  [`unitares-sdk`](../../agents/sdk/README.md) and its `GovernanceAgent` pattern.
- Codex and Claude Code lifecycle hooks: use the
  [governance plugin](https://github.com/cirwel/unitares-governance-plugin) for
  MCP, lifecycle hooks, skills, and per-file edit leases via the lease plane
  ([§2.1](02-install.md#21-docker-quickstart)); see
  [`CODEX_START.md`](../guides/CODEX_START.md) for the Codex and ChatGPT
  workflow.
- Hermes Agent: install the reviewed `unitares` plugin from the Hermes plugin
  catalog with `hermes plugins install unitares`
  (`hermes plugins install cirwel/unitares-host-adapter` installs the
  unreleased repository version instead). At install it asks
  for `UNITARES_MCP_URL`, your server's endpoint (for example
  `http://localhost:8767/mcp/`); with it unset, the plugin logs a warning and
  sends nothing.
- Other model hosts and thin clients: use the
  [host adapter](https://github.com/cirwel/unitares-host-adapter).

---

[← Running the server](03-running-the-server.md) · [Manual index](README.md) · [Next: Reading the record →](05-reading-the-signals.md)
