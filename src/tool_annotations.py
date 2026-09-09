"""One MCP tool-annotation record per advertised tool: the machine-readable half.

A client, and a scoring tool such as TDQS, reads two things about a tool. The
description is prose: it carries purpose, the choices a caller has, what comes
back, and when a sibling tool is the better call. Annotations are the
deterministic half — four booleans and a display title that a client can branch
on without parsing English. An agent framework that auto-approves reads and
prompts before writes needs ``readOnlyHint``, not a paragraph that happens to
begin with the word "Read". Neither half substitutes for the other: the hints
say what CLASS of operation this is, the description says what it actually does.

Both are advisory. The MCP spec is explicit that a client must not treat these
hints as a security boundary — the server enforces authorization itself, per
action, inside the handler. What the hints buy is a client that does not have
to guess.

The four hints, as this file uses them:

``readOnlyHint``
    True only when NO call to the tool changes stored state. Set False for
    every tool that persists a row, mints an identity, appends an audit event,
    advances an EISV monitor, or spends an agent's energy budget — including
    tools whose name and primary purpose read like a query. ``simulate_update``
    is a dry run that still appends an ``auto_attest`` audit event;
    ``observe(action='anomalies')`` writes findings; ``identity`` mints a fresh
    agent when no proof signal arrives. All three are False.

    The rule has an edge that is invisible in the handler body, and it decides
    seven of the records below. A tool registered
    ``requires_identity='required'`` with no ``pre_onboard_actions`` never
    reaches its handler on an unbound call: the identity middleware resolves
    first, and that resolve is ``force_new=True``
    (src/mcp_handlers/middleware/identity_step.py:1069), which MINTS AND
    PERSISTS an agent row before a single line of the handler runs. Such a tool
    is not read-only however purely it reads — ``dashboard``,
    ``get_thresholds``, ``get_trajectory_status``, ``get_workspace_health``,
    ``list_process_bindings``, ``outcome_correlation`` and
    ``verify_trajectory_identity`` are all False, and several of them for that
    reason alone. ``search_shared_memory`` is NOT one of them: it canonicalizes
    to ``knowledge``, whose ``pre_onboard_actions`` carry ``search``, and the
    alias injects ``action='search'``, so a bare call resolves to
    ``pre_onboard`` and never reaches the mint. Its False is earned the same
    way ``search_knowledge_graph``'s is — every successful search appends a
    ``knowledge_read`` audit event — which is what its record below says. The
    two metrics readers that stay True escape the middleware rule by being
    pre-onboard exempt AND refusing to resolve an unproven caller, which is a
    property of the handler, not of the name
    (``check_working_state`` / ``get_governance_metrics``,
    src/mcp_handlers/core.py:239-255).
``destructiveHint``
    True when a call can remove, overwrite, or supersede something a caller
    already had — archival sweeps, delete paths, status flips to superseded,
    overwriting a threshold or a per-pair coordination record. Append-only
    writes are NOT destructive: a new check-in, a new outcome row and a new
    knowledge finding all leave every prior row intact, so they are False.
    Meaningless when ``readOnlyHint`` is True, and left False there. A router
    is destructive when ANY of its actions is: ``config`` carries
    ``set_thresholds``' overwrite because ``action='set'`` dispatches straight
    into that handler (src/mcp_handlers/consolidated.py:270).
``idempotentHint``
    True when repeating the same call with the same arguments adds no further
    effect. Every read is idempotent, and so is a read-shaped tool that lost
    ``readOnlyHint`` only to the identity middleware above: the mint happens
    once, on the first unbound call, and a repeat finds a bound session. Among
    the tools that write on their own account only ``admin`` and
    ``bind_session`` qualify: each converges on a requested state rather than
    accumulating. Anything that appends a row, or stamps a fresh attempt
    timestamp even when it refuses, is False — ``set_thresholds`` converges on
    the value it is handed but writes an attempt audit row on every admitted
    call (src/mcp_handlers/admin/config.py:116), so it is False too.
``openWorldHint``
    True when the call can reach beyond this server's own datastore — an
    inference provider, a subscription CLI, a dispatched reviewer process on
    another host. False for everything served out of Postgres, Redis and
    in-process state, which is most of the surface.

Cross-checked against ``ToolMeta.operation`` in src/tool_meta.py: no tool
carrying ``readOnlyHint=True`` here is recorded there as ``write`` or ``admin``.
The reverse does not hold, deliberately — a router's ``operation`` carries the
most privileged class among its actions, and several tools recorded as ``read``
still write something on some path, so this map is the more conservative of the
two. When the two disagree in that direction, the handler decides, and the
disagreement is the point rather than a bug to normalize away.

Titles are unique. Three of them carry a ``(Primitive)`` suffix for no reason
except that: ``process_agent_update``, ``get_governance_metrics`` and
``outcome_event`` are the raw implementations behind the ``sync_state``,
``check_working_state`` and ``record_result`` aliases, and a client's tool
picker showing the same display name twice is a worse outcome than a slightly
longer one. The plain workflow name stays with the alias, which is the call a
caller should prefer.

Keys are camelCase. This is not a style choice: on mcp 1.x ``ToolAnnotations``
is declared with ``extra='allow'``, so a snake_case key is absorbed without an
error and serialized under the wrong wire name — a silent, untestable
corruption. On mcp 2.x the fields are snake_case natively with camelCase
validation aliases, so camelCase is the one spelling that constructs correctly
on both majors and dumps to the wire shape the spec requires.
tests/test_tool_annotations.py holds that invariant.

Standard library only at import time; ``mcp.types`` is imported inside the
helper. Adding a tool: one record here beside its src/tool_meta.py record and
its description, or the guard test fails with the missing name.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

# Advertised order: the eight workflow aliases first, then wire order, matching
# get_public_tool_definitions(). Titles are display names for a client's tool
# picker, not a second description.
TOOL_ANNOTATIONS: Dict[str, Dict[str, Any]] = {
    # --- workflow aliases -------------------------------------------------
    "sync_state": {
        # Persists the check-in and advances EISV; appends, never overwrites.
        "title": "Governance Check-In",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    "check_working_state": {
        # The one metrics reader that cannot mint: pre_onboard, and an unproven
        # caller gets the unbound payload instead of a resolve.
        "title": "Read Governance State",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "search_shared_memory": {
        # Every successful search appends a knowledge_read audit event, so the
        # read is a write — the same reason simulate_update is not read-only.
        "title": "Search Shared Memory",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    "store_finding": {
        # Create-only. auto_link_related writes related_to on the NEW row only
        # (_link_similar_store_discoveries, knowledge/handlers.py:1388); the rows
        # it points at are never touched. Matches leave_note, same mechanism.
        "title": "Store Knowledge Finding",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    "update_finding": {
        # summary/details replace what was there.
        "title": "Revise Knowledge Finding",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    "start_session": {
        # Mints a new agent row per call; nothing existing is displaced.
        "title": "Start Governed Session",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    "record_result": {
        "title": "Record Outcome Event",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    "request_review": {
        # The reviewer may be a peer or a dispatched process on another host.
        "title": "Request Governed Review",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
    # --- wire tools -------------------------------------------------------
    "health_check": {
        "title": "Server Health Snapshot",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "get_workspace_health": {
        # Reads only; not read-only because it is identity-gated with no
        # pre-onboard exemption, so an unbound call mints and persists an
        # identity in the middleware before the handler runs.
        "title": "Workspace Configuration Health",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "process_agent_update": {
        "title": "Governance Check-In (Primitive)",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    "get_governance_metrics": {
        # pre_onboard, and the handler refuses to resolve on an unproven call
        # (core.py:239-255) instead of minting — a genuine read even though
        # four sibling reads in this table are not.
        "title": "Read Governance State (Primitive)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "mark_response_complete": {
        # Sets status and appends a lifecycle event; last_response_at moves.
        "title": "Mark Response Complete",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    "detect_stuck_agents": {
        # auto_recover is default-off but not the only writer: it archives
        # lineage-superseded parents, and even with it off a changed stuck set
        # appends an audit entry.
        "title": "Stuck Agent Sweep",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    "archive_old_test_agents": {
        # Defaults to dry_run and needs the operator's auto-archival opt-in,
        # but the hints describe what a call CAN do, and the mutating path
        # archives live agents.
        "title": "Sweep Stale Test Agents",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    "archive_orphan_agents": {
        # Same shape as the stale-test sweep: preview by default, but the
        # engine it delegates to mutates once auto-archival is enabled and
        # dry_run is false.
        "title": "Sweep Orphan Agents",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    "simulate_update": {
        # Dry run for the caller, but every call appends an auto_attest audit
        # event and advances in-process telemetry — not read-only.
        "title": "Dry-Run Governance Cycle",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    "get_thresholds": {
        # Handler is a pure config read, but it is identity-gated with no
        # pre-onboard exemption, so an unbound caller mints a persisted
        # identity before it runs.
        "title": "Read Governance Thresholds",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "set_thresholds": {
        # Overwrites the previous override, and stamps a
        # threshold_modification_attempt audit row on every admitted call — so
        # the value converges but the audit trail accumulates.
        "title": "Set Governance Thresholds",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    "outcome_event": {
        # Title disambiguates the record_result alias, which keeps the plain
        # workflow name; the flags are that alias's flags.
        "title": "Record Outcome Event (Primitive)",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    "list_tools": {
        "title": "Browse Tool Catalog",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "describe_tool": {
        "title": "Inspect One Tool",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "skills": {
        "title": "Governance Skill Bundle",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "search_knowledge_graph": {
        # Every successful search appends a durable knowledge_read audit row,
        # so it is neither read-only nor idempotent — and it is NOT deprecated:
        # tool_meta records it STABLE and search_shared_memory's own migration
        # note routes callers here for the raw payload.
        "title": "Search Knowledge Graph (Raw)",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    "leave_note": {
        # Adds one open note; deletes and overwrites nothing.
        "title": "Leave Shared Note",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    "list_inference_hosts": {
        # Readiness is a live TCP probe of the Ollama endpoint, which is what
        # makes a listing openWorld.
        "title": "Inference Host Discovery",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
    "describe_inference_host": {
        # openWorld on a describe is not obvious: get_inference_host runs the
        # same live Ollama probe as list_inference_hosts
        # (model_inference.py:159).
        "title": "Inference Host Detail",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
    "consult": {
        # Reaches an advisory model and posts an EISV accounting update.
        "title": "Advisory Model Consultation",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
    "call_model": {
        # Each successful call advances the caller's EISV monitor as an energy
        # spend (model_inference.py:542), which is why a stateless-looking
        # model call is not read-only.
        "title": "Standard Advisory Model Call",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
    "delegate_inference": {
        # Dispatches to an operator-authorized strong-model host in another
        # process and advances the caller's EISV monitor
        # (delegated_inference.py:57).
        "title": "Delegated Strong-Model Inference",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
    "onboard": {
        "title": "Mint Agent Identity",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    "identity": {
        # An argument-less call mints a fresh agent rather than reporting one.
        "title": "Resolve Or Mint Session Identity",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    "bind_session": {
        # Re-points this session at the resolved agent; repeating converges.
        # Not destructive: the rebind overwrites this session's pointer, but
        # the previously bound agent row is untouched and re-bindable, so
        # nothing the caller had is lost.
        "title": "Bind Session to Agent Identity",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "knowledge": {
        # supersede flips an older row; cleanup archives fleet-wide; synthesize
        # narrates through the local model.
        "title": "Shared Knowledge Graph",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    },
    "agent": {
        # archive, resume and delete carry no ownership check.
        "title": "Agent Lifecycle Management",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    "calibration": {
        # rebuild discards existing calibration state.
        "title": "Confidence Calibration Ops",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    "config": {
        # action='set' overwrites the previous override in place and no action
        # restores it — the same write set_thresholds is marked destructive
        # for, reached through the same handler (consolidated.py:270).
        "title": "Governance Threshold Config",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    "export": {
        # action='file' truncates an existing file when filename is given.
        "title": "Governance History Export",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    "cirs_protocol": {
        # Each new state announcement, per-pair coherence report or boundary
        # contract replaces the previous one.
        "title": "CIRS Coordination Protocol",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    "self_recovery": {
        # A resume drops the pause and cooldown timestamps, and every attempt
        # — refused ones included — stamps a fresh recovery_attempt_at.
        "title": "Paused Agent Self-Recovery",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    "operator_resume_agent": {
        # Clears the target's paused status and additionally writes an
        # operator_intervention row into the knowledge graph, so a repeat call
        # is not a no-op.
        "title": "Operator Resume Agent",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    "observe": {
        # action='anomalies' appends an audit entry per changed finding.
        "title": "Fleet Behavior Observation",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    "dialectic": {
        # reassign overwrites the reviewer; a reviewer process may be spawned
        # on an operator-configured host.
        "title": "Governed Peer Review",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    },
    "dashboard": {
        # Read-shaped, but identity-gated with no pre-onboard exemption, so an
        # unbound call mints and persists an identity before the handler runs;
        # the module docstring's "bypasses session binding" describes the
        # handler, not the middleware.
        "title": "Fleet EISV Overview",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "admin": {
        # cleanup_locks permanently deletes lock files; reset_monitor discards
        # an in-memory monitor. Both converge rather than accumulate.
        "title": "Server Diagnostics & Maintenance",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "get_trajectory_status": {
        # Reads only; not read-only because it is identity-gated with no
        # pre-onboard exemption, so an unbound call mints and persists an
        # identity in the middleware before the handler runs.
        "title": "Trajectory Identity Status",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "verify_trajectory_identity": {
        # Compares signatures and stores none; not read-only because it is
        # identity-gated with no pre-onboard exemption, so an unbound call
        # mints and persists an identity in the middleware before the handler
        # runs.
        "title": "Trajectory Identity Check",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "list_process_bindings": {
        # Reads only; not read-only because it is identity-gated with no
        # pre-onboard exemption, so an unbound call mints and persists an
        # identity in the middleware before the handler runs.
        "title": "Live Process Bindings",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "outcome_correlation": {
        # The study itself only SELECTs, but the tool is identity-gated with no
        # pre-onboard exemption, so an unbound call mints a persisted identity
        # first.
        "title": "EISV Outcome Correlation",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "record_progress_pulse": {
        # Append-only: every call adds a row.
        "title": "Resident Progress Pulse",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
}

# The wire keys mcp.types.ToolAnnotations serializes. Anything else is either a
# typo or a snake_case field name, and mcp 1.x would swallow both.
ANNOTATION_KEYS = frozenset(
    {"title", "readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"}
)


def annotation_payload(name: str) -> Optional[Dict[str, Any]]:
    """The raw annotation dict for ``name``, or None when it has no record.

    A copy, so a caller that serializes it into an envelope cannot mutate the
    table. Imports nothing: usable from a surface that has no ``mcp`` types.
    """
    payload = TOOL_ANNOTATIONS.get(name)
    return dict(payload) if payload else None


def tool_annotations(name: str) -> Any:
    """``ToolAnnotations`` for ``name``, or None when it has no record.

    None is the correct value for an unannotated tool: ``Tool.annotations`` is
    optional in the MCP spec and omitted from the wire when unset, so a plugin
    tool or a future addition simply advertises no hints rather than the wrong
    ones. ``mcp.types`` is imported here rather than at module scope so this
    module stays importable with no third-party dependency.
    """
    payload = TOOL_ANNOTATIONS.get(name)
    if not payload:
        return None
    from mcp.types import ToolAnnotations

    return ToolAnnotations(**payload)
