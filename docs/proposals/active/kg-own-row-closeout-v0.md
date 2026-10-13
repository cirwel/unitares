# A bounded prompt to revisit a session's own findings

**Status:** Friction fix authorized by the 2026-10-13 addendum and KG decision
`2026-10-13T09:17:42.808916+00:00`; implemented as instructions in the existing
closeout command and lifecycle skill. **Evidence checked:** 2026-10-13.

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

## Implemented prompt

At explicit closeout, the existing `commands/closeout.md` and
`skills/governance-lifecycle/SKILL.md` remind an agent that wrote actionable
findings to search its exact process UUID (open, limit 10, lean) and review at
most three actionable rows. The agent opens details and resolves with evidence,
supersedes with a real successor, or keeps with a useful reason.
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
No runtime code, tool, action, hook, query scheduler or check-in behavior changes.
The plugin receives the canonical lifecycle skill through the existing automated
skill-mirror PR; it is not edited by hand.

Validation is a documentation/skill consistency check for this instruction-only
implementation, not a claim that a running host displayed the prompt. Server
identity and closure validation are unchanged. The fixture scenarios above are
requirements for any later automated implementation, not tests of new runtime
code in this PR.
