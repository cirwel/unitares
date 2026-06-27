"""Identity-bootstrap policy helpers for #425.

Centralizes the STRICT_IDENTITY_REQUIRED env-flag check so every auto-mint
path checks the same gate the same way. Without this, the gate drifts
(one path checks "true", another "1", another normalizes case differently)
and the rollout becomes a per-path negotiation instead of a single switch.
"""

from __future__ import annotations

import os


_TRUTHY = frozenset({"1", "true", "yes", "on"})


def is_strict_identity_required() -> bool:
    """True iff STRICT_IDENTITY_REQUIRED env var is set to a truthy value.

    Truthy values: "1", "true", "yes", "on" (case-insensitive). Anything
    else, including unset, is False.

    When True, all auto-mint paths MUST refuse-or-skip rather than create
    an ephemeral identity. See CLAUDE.md "STRICT_IDENTITY_REQUIRED (#425
    staged rollout)" for the contract and rollout sequence.
    """
    raw = os.getenv("STRICT_IDENTITY_REQUIRED", "").strip().lower()
    return raw in _TRUTHY


_DEFAULT_REFUSAL_HINT = (
    "This tool works once you have a governance identity — call onboard() to "
    "mint one and it's yours to use. If you're continuing work handed off from "
    "an exited session, pass parent_agent_id to declare lineage; otherwise "
    "pass force_new=true for a fresh identity."
)
_DEFAULT_REFUSAL_NEXT_STEP = (
    "Call onboard(force_new=true) to mint a fresh process identity, or pass "
    "parent_agent_id only for a real handoff from an exited predecessor."
)
_DEFAULT_REFUSAL_SAFE_OPTIONS = (
    {
        "action": "start_fresh",
        "call": "onboard(force_new=true)",
        "when": "This is a new process-instance with no causal predecessor.",
    },
    {
        "action": "declare_lineage",
        "call": "onboard(force_new=true, parent_agent_id=<prior UUID>, spawn_reason='new_session')",
        "when": "A finished predecessor explicitly handed this work to you.",
    },
    {
        "action": "stay_read_only",
        "call": "get_governance_metrics() or list_tools()",
        "when": "You do not yet have caller-proven identity for a write.",
    },
)
_DEFAULT_REFUSAL_DO_NOT = (
    "Do not retry bare identity(agent_uuid=..., resume=true); UUID alone is not ownership proof.",
)


def strict_identity_refusal_payload(
    tool_name: str,
    *,
    status: str = "identity_required",
    hint: str | None = None,
    next_step: str | None = None,
    safe_options: list[dict] | tuple[dict, ...] | None = None,
    do_not: list[str] | tuple[str, ...] | None = None,
    identity_assurance: dict | None = None,
    surface_context: dict | None = None,
) -> dict:
    """The #425 typed-refusal shape, single-sourced.

    Consumed by EVERY refusal emission point: the MCP dispatch middleware
    (identity_step.py, wrapped in success_response), the REST gate
    (http_tool_service.execute_http_tool, returned raw), the
    process_agent_update Path-C refusal (updates/phases.py, hint
    override), and the bare-onboard Path-B refusal (identity/handlers.py,
    status+hint overrides — its status is deliberately
    ``lineage_declaration_required``). Stage-1 burn-in (2026-06-11,
    docs/handoffs/strict-identity-stage1-burnin-2026-06-11.md) found the
    surfaces drifting — empty ontology_ref on two of them, divergent
    fields — so the shape lives here and the emission points cannot
    drift (same single-source discipline as core.unbound_metrics_payload).

    A structured success-shape, not an error: error responses invite
    retry-with-mint catch paths and would reintroduce the ghost leak.
    """
    payload = {
        "status": status,
        "tool": tool_name,
        "tool_class": "required",
        "hint": hint if hint is not None else _DEFAULT_REFUSAL_HINT,
        "next_step": next_step if next_step is not None else _DEFAULT_REFUSAL_NEXT_STEP,
        "safe_options": [
            dict(option) for option in (
                safe_options if safe_options is not None else _DEFAULT_REFUSAL_SAFE_OPTIONS
            )
        ],
        "do_not": list(do_not if do_not is not None else _DEFAULT_REFUSAL_DO_NOT),
        "ontology_ref": "CLAUDE.md \"STRICT_IDENTITY_REQUIRED (#425 staged rollout)\"",
        "rollout_flag": "STRICT_IDENTITY_REQUIRED",
    }
    if identity_assurance is not None:
        payload["identity_assurance"] = dict(identity_assurance)
    if surface_context is not None:
        payload["surface_context"] = dict(surface_context)
    return payload
