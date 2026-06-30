"""Stakes classification table — the load-bearing artifact for #775.

Issue #775 ("Stakes-keyed gating") generalizes the #425 action-classification
seam from "requires identity" to "requires a governance verdict", keyed on
STAKES: low-stakes work flows under substrate observation, high-consequence
boundaries (destructive ops, fleet/global mutations, governance-state changes
on other agents, single-writer surfaces) are the ones a verdict gate would
guard.

This module is DELIBERATELY just DATA + two pure lookup functions. It holds no
gate mechanism, imports nothing from the dispatch/middleware stack, and touches
no asyncio/anyio/DB. Two reasons:

  1. Per #775, "the classification table is the load-bearing artifact, not the
     gate mechanism" — an unclassified high-stakes path slips regardless of how
     clean the gate is. Keeping the table standalone makes the classification
     auditable and testable on its own.

  2. PORTING NOTE (Wave-3, 2026-06-25 resolution). The handler-dispatch /
     identity-middleware surface is committed to a future BEAM/Elixir port
     (sequenced LAST — "worst first" — but the direction stands). When that
     port lands, the gate MECHANISM (the middleware step / REST hook) is
     re-expressed in Elixir, but this CLASSIFICATION must not be re-derived by
     hand. Serialize it instead:

         python -m src.mcp_handlers.stakes_table --export-json

     and load the JSON as Elixir config. `export_table()` is the single
     authoritative serialization point.

As of this commit the table is INERT: no gate consults it. It ships as the
durable, port-survivable artifact while the gate mechanism is parked pending
the BEAM-port sequencing (or real demand). See
docs/proposals/stakes-keyed-gating-775.md.

Resolution contract (`get_action_stakes`):
  - exact (tool, action) entry wins, else
  - (tool, None) tool-level entry wins, else
  - FAIL CLOSED to "high" for genuinely unknown tools/actions.

The fail-closed default is the #425-faithful choice: a newly added tool/action
that is genuinely low-stakes is over-classified as high until someone lists it
here — a deliberate friction that forces classification rather than letting an
unclassified high-stakes path slip through as low-stakes. `test_stakes_table.py`
asserts every *registered* tool/action is explicitly present, so the fail-closed
default only ever bites truly unregistered names. External-plugin tools (e.g.
the `unitares_pi_plugin` device tools, handler module not under `src.`) are
deliberately NOT enumerated: they fall to the fail-closed `high` default until
an operator classifies them when the gate is built — the safe direction for
tools this server does not own.
"""

from __future__ import annotations

from typing import Optional

__all__ = ["get_action_stakes", "is_high_stakes", "export_table", "STAKES_LEVELS"]

STAKES_LEVELS = ("baseline", "high")

# A "high" key is a high-consequence boundary: destructive/irreversible ops,
# fleet- or globally-scoped mutations, governance-state changes applied to
# OTHER agents, single-writer surfaces, and dialectic resolution. Everything
# else is "baseline" — observed by the substrate sink (#669), not pre-gated.
#
# Keys are (canonical_tool_name, action) for action-router tools, or
# (canonical_tool_name, None) for single-purpose tools.
_HIGH: frozenset[tuple[str, Optional[str]]] = frozenset({
    # knowledge — destructive / override mutations (store/update are routine)
    ("knowledge", "cleanup"),
    ("knowledge", "supersede"),
    # agent — mutate another agent's governance state / lifecycle
    ("agent", "update"),
    ("agent", "archive"),
    ("agent", "resume"),
    ("agent", "delete"),
    # calibration — fleet-global calibration writes
    ("calibration", "update"),
    ("calibration", "backfill"),
    ("calibration", "rebuild"),
    # config — server configuration mutation
    ("config", "set"),
    # dialectic — resolve / reassign a governance review
    ("dialectic", "synthesis"),
    ("dialectic", "reassign"),
    # admin — destructive / state-resetting maintenance (mirrors the
    # standalone reset_monitor / cleanup_stale_locks classification)
    ("admin", "reset_monitor"),
    ("admin", "cleanup_locks"),
    # single-purpose admin / destructive / pause-state tools
    ("archive_old_test_agents", None),
    ("archive_orphan_agents", None),
    ("cirs_protocol", None),
    ("cleanup_stale_locks", None),
    ("direct_resume_if_safe", None),
    ("operator_resume_agent", None),
    ("reset_monitor", None),
    ("set_thresholds", None),
    ("reassign_reviewer", None),
    ("submit_synthesis", None),
})

# Known-baseline keys. Listed explicitly (rather than relying on the
# fail-closed default) so the coverage test can prove every registered surface
# is a *deliberate* classification, and so default-high only catches genuinely
# unregistered names. SELF-GOVERNANCE / IDENTITY-LIFECYCLE / READ tools MUST be
# here — most importantly process_agent_update: it is the call that PRODUCES a
# verdict, so gating it on having a prior verdict would permanently block every
# new agent's first check-in (the chicken-and-egg failure mode).
_BASELINE: frozenset[tuple[str, Optional[str]]] = frozenset({
    # knowledge reads + routine writes
    ("knowledge", "store"),
    ("knowledge", "search"),
    ("knowledge", "get"),
    ("knowledge", "list"),
    ("knowledge", "update"),
    ("knowledge", "details"),
    ("knowledge", "note"),
    ("knowledge", "synthesize"),
    ("knowledge", "stats"),
    ("knowledge", "audit"),
    # agent reads
    ("agent", "list"),
    ("agent", "get"),
    # calibration read
    ("calibration", "check"),
    # config read
    ("config", "get"),
    # export (read/export only)
    ("export", "history"),
    ("export", "file"),
    # observe — read-only analytics
    ("observe", "agent"),
    ("observe", "compare"),
    ("observe", "similar"),
    ("observe", "anomalies"),
    ("observe", "aggregate"),
    ("observe", "telemetry"),
    ("observe", "audit_events"),
    ("observe", "outcome_evidence"),
    ("observe", "bridge"),
    # admin — diagnostic reads (destructive actions are in _HIGH); mirrors the
    # standalone get_server_info / get_workspace_health / ... classification
    ("admin", "server_info"),
    ("admin", "connections"),
    ("admin", "workspace_health"),
    ("admin", "tool_usage"),
    ("admin", "telemetry"),
    ("admin", "debug_context"),
    ("admin", "validate_path"),
    # dialectic — participation + reads (resolution is in _HIGH)
    ("dialectic", "get"),
    ("dialectic", "list"),
    ("dialectic", "quick"),
    ("dialectic", "request"),
    ("dialectic", "thesis"),
    ("dialectic", "antithesis"),
    # research_registry
    ("research_registry", "list"),
    ("research_registry", "query"),
    ("research_registry", "get"),
    ("research_registry", "stats"),
    ("research_registry", "export"),
    ("research_registry", "record"),
    # single-purpose: identity lifecycle + self-governance + reads
    ("bind_session", None),
    ("call_model", None),
    ("dashboard", None),
    ("debug_request_context", None),
    ("describe_tool", None),
    ("detect_stuck_agents", None),
    ("get_connection_status", None),
    ("get_governance_metrics", None),
    ("get_server_info", None),
    ("get_telemetry_metrics", None),
    ("get_thresholds", None),
    ("get_tool_usage_stats", None),
    ("get_trajectory_status", None),
    ("get_workspace_health", None),
    ("health_check", None),
    ("identity", None),
    ("leave_note", None),
    ("list_process_bindings", None),
    ("list_tools", None),
    ("mark_response_complete", None),
    ("onboard", None),
    ("outcome_correlation", None),
    ("outcome_event", None),
    ("process_agent_update", None),  # produces the verdict — MUST be baseline
    ("record_progress_pulse", None),
    ("request_dialectic_review", None),
    ("search_knowledge_graph", None),
    ("self_recovery", None),
    ("simulate_update", None),
    ("skills", None),
    ("submit_antithesis", None),
    ("submit_thesis", None),
    ("validate_file_path", None),
    ("verify_trajectory_identity", None),
})

# Build the lookup once. A key declared in both sets is a developer error.
_overlap = _HIGH & _BASELINE
if _overlap:  # pragma: no cover - guarded by test_stakes_table too
    raise ValueError(f"stakes_table: keys in both _HIGH and _BASELINE: {sorted(_overlap)}")

_STAKES: dict[tuple[str, Optional[str]], str] = {
    **{k: "high" for k in _HIGH},
    **{k: "baseline" for k in _BASELINE},
}

_UNKNOWN_DEFAULT = "high"  # fail closed


def get_action_stakes(tool_name: str, action: Optional[str]) -> str:
    """Stakes level for a (tool, action) pair: "high" or "baseline".

    Resolution: exact (tool, action) -> (tool, None) -> fail closed to "high".
    Pure and synchronous — safe to call anywhere, no I/O.
    """
    action_norm = action.lower() if action else None
    if (tool_name, action_norm) in _STAKES:
        return _STAKES[(tool_name, action_norm)]
    if (tool_name, None) in _STAKES:
        return _STAKES[(tool_name, None)]
    return _UNKNOWN_DEFAULT


def is_high_stakes(tool_name: str, action: Optional[str]) -> bool:
    return get_action_stakes(tool_name, action) == "high"


def export_table() -> dict[str, str]:
    """Serialize the table for a non-Python (e.g. BEAM) gate.

    Keys are "tool" or "tool:action" strings. This is the single authoritative
    serialization point — the porting contract in the module docstring.
    """
    out: dict[str, str] = {}
    for (tool, action), level in sorted(_STAKES.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")):
        key = tool if action is None else f"{tool}:{action}"
        out[key] = level
    return out


if __name__ == "__main__":  # pragma: no cover
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Stakes classification table (#775)")
    parser.add_argument("--export-json", action="store_true", help="emit the table as JSON")
    args = parser.parse_args()
    if args.export_json:
        print(json.dumps(export_table(), indent=2, sort_keys=True))
    else:
        high = sum(1 for v in _STAKES.values() if v == "high")
        base = sum(1 for v in _STAKES.values() if v == "baseline")
        print(f"stakes_table: {len(_STAKES)} entries ({high} high, {base} baseline). "
              f"Use --export-json to serialize.")
