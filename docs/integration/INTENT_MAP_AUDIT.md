# Intent map audit: overlaps, gaps, open questions

**Status: draft for maintainer reaction (2026-10-10).** Companion to the
[intent map](INTENT_MAP.md), which is the agent-facing front door. This page
holds the surface audit behind it: where several tools serve one need, needs
no single tool expresses, tools hidden from the initial listing, and a
proposed check for additions. Pointers name the source each observation rests
on; if the catalog disagrees, the catalog wins.

## Overlaps and gaps

Observations, each with a pointer. None is a proposal to remove a capability;
the questions at the end are for the maintainer.

**Overlaps (one need, several tools)**

1. *Model calls are split by transport.* `call_model` accepts only the
   synchronous HTTP hosts and `delegate_inference` only the orchestrator CLI
   hosts (`accepts_host_id_from` in `list_inference_hosts`; the refusal at
   `src/mcp_handlers/support/model_inference.py`, "Pick a host whose
   accepts_host_id_from includes call_model"). For an agent, both mean "ask
   model X".
2. *Descriptions route agents between each other.* `consult`: "on-record:
   request_review"; `call_model`: "for advisory help prefer consult";
   `delegate_inference`: "for a raw completion use call_model"; `dialectic`:
   "For advice outside a governed-review session, use consult"
   (`src/tool_descriptions.py` and the catalog).
3. *"consult" names two things.* The `consult` tool gives advice with no
   review record; `dialectic(action="consult")` files an outside verdict on an
   existing session as a non-authoritative record (`dialectic` description).
4. *Three search entry points, one handler:* `search_shared_memory`,
   `search_knowledge_graph`, `knowledge(action="search")` (stated in the
   `search_knowledge_graph` description).
5. *Two note entry points, one handler:* `leave_note` and
   `knowledge(action="note")` (`handle_leave_note`,
   `src/mcp_handlers/knowledge/handlers.py`).
6. *Alias pairs carry different timeouts.* `sync_state` 30 s vs
   `process_agent_update` 60 s; `check_working_state` 30 s vs
   `get_governance_metrics` 10 s (`timeout` field in `list_tools(lite=false)`).
   Whether that is intended is not established here.
7. *Operator reads and writes appear twice:* `get_thresholds`/`set_thresholds`
   vs `config(action="get" | "set")`; `get_workspace_health` vs
   `admin(action="workspace_health")`; `archive_*` vs `agent(action="archive")`;
   `operator_resume_agent` vs `agent(action="resume")`.

**Needs no single tool expresses**

1. *"A strong model that is not family X."* `consult` excludes only the
   caller's own inferred family. A Claude caller wanting a non-Codex answer
   gets Codex first (`_THOROUGH_PEERS["anthropic"]`); to choose, it must drop
   to `delegate_inference`, which is hidden and reached through `use_tool`.
2. *"A cloud model, chosen by me, as advice."* `consult` has no model or host
   field (`ConsultParams` forbids extra keys); `call_model` has them but is
   hidden. Whether `call_model` leaves an audit row comparable to
   `consult`'s was not checked for this draft.
3. *"A strong model, on the record, chosen by me."* `request_review` has no
   reviewer field; the backend is deployment config, and the default reviewer
   is the local model.
4. *"What is another agent working on now?"* `agent(action="list")` and
   `observe` report lifecycle and EISV state; check-in text is not kept as
   history, and `cirs_protocol` state announcements carry EISV state, not a
   task (`src/mcp_handlers/cirs/state.py`).
5. *Coordinating over a surface.* No lease or claim tool is in the catalog;
   `cirs_protocol` is the only live channel and is lost on restart.

**Hidden tools that a listed need depends on** (absent from
`PROGRESSIVE_MODE_TOOLS`)

- Revising a finding needs `update_finding`. `src/tool_modes.py` says why it
  is hidden: advertising it costs ~3.4 KB against a down-only byte ratchet.
- Choosing a model needs `list_inference_hosts`, plus `call_model` or
  `delegate_inference`.
- Advancing an open review needs `dialectic`.
- Tier and advertisement disagree: `leave_note` and `health_check` are tier
  `essential` but hidden; `self_recovery` is tier `common` but advertised.

**Open questions for the maintainer**

- Could one advice entry point take an optional "who answers" input (any,
  a named family, not a named family, local only) and route to the existing
  lanes, leaving `call_model` and `delegate_inference` as they are?
- Should every tool a listed need depends on be advertised, or should the
  initial listing at least name them?
- Should `dialectic(action="consult")` get a name that does not collide with
  `consult`?

## Adding to the surface (proposal)

Before a new tool, alias, action or model channel is added:

1. Find the need on the [intent map](INTENT_MAP.md) it serves. If none fits, add the need first.
2. Say in the PR why the entries already listed for that need do not cover it.
   "A different transport or host" is an implementation reason, not a need.
3. Update the intent map (and this audit, if it adds an overlap) in the same PR.

The check only works where agents and contributors already look, so one way
to place it would be a checkbox in the PR template and one line in the shared
AGENTS.md/CLAUDE.md contract; neither has been edited. This is a proposal for
the maintainer, not an adopted rule.
