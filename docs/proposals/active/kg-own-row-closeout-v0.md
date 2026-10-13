# A bounded prompt to revisit a session's own findings

**Status:** Draft feature proposal; implementation held through 2026-10-22
unless the operator rules otherwise. **Evidence checked:** 2026-10-13.

## Problem and scope

The KG usability audit, discovery `2026-10-13T06:48:37.234783+00:00`, counted
543 open rows versus 120 resolved and 12 closed. This is a dated inventory,
not a measure of failure: insights, rules and unresolved questions can properly
remain open. A ratio target would encourage unsupported closures.

The project already instructs agents to close findings with a declared standard
and evidence (`AGENTS.md`, "Agents are the users"; `skills/knowledge-graph/SKILL.md`,
"Closing the Loop"). The inspected check-in paths
`src/mcp_handlers/updates/phases.py` and `enrichments.py` do not provide a
dedicated prompt to revisit the caller's own open findings at session end.
Hooks are substrate observations, not permission to speak for the agent.

## Proposed behavior after the hold

At an explicit closeout, show at most three open **actionable findings authored
by the bound process**, with a count and a retrieval pointer. Ask the agent to
resolve with evidence, supersede with a real successor, or keep with a reason.
Start as an optional closeout surface rather than querying the graph on every
check-in. A later check-in integration would need an explicit budget and a
once-per-session rule; that is not authorized here.

The prompt has no enforcement authority. It must not auto-close, block work,
assign outcome scores, count as an agent check-in, or rewrite another session's
findings. A predecessor's rows belong to a different process: lineage alone
does not make them the caller's own. An explicit handoff can separately name
rows to triage, as this audit did.

Keep is a valid outcome, and quiet sessions need no prompt. Exclude imported
sync-owned rows and recovery telemetry. Do not treat every open insight as an
uncompleted task. Repeating a declined prompt must not create graph noise;
record a keep reason only when it adds useful evidence or answers a requested
triage, not merely because a hook fired.

## Acceptance evidence and decision

Test caller binding, cross-agent and predecessor isolation, zero/large result
sets, truncation, repeated closeout, graph unavailability and exclusion rules.
Use a fixed fixture set containing both legitimately open knowledge and
completed findings. Measure prompt size, extra calls and whether agents choose
supported closures; do not optimize the resolved/open ratio. Keep this out of
the preserved EISV estimator and registered trial paths.

A later implementation touching outputs or schema descriptions needs the
interface-contract release, regenerated artifact, changelog and skill re-checks.
New actions or tool names also need operator review under the intent map.
No runtime code or hook is added in this proposal.

**Operator question:** After the freeze, should explicit session closeout offer
an optional three-row prompt for the caller's own actionable findings?
