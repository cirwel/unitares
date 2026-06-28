"""Shared builder: finding-resolution → exogenous ``outcome_event`` args.

A resident's finding being adjudicated by an operator/human is ground truth from
*outside* the governance loop (Invariant 4 clean — not the loop validating its
own trajectory), so the resulting outcome carries
``verification_source='external_signal'``. This is the exogenous-anchor channel
the EISV residual falsifiability test needs (docs/proposals/eisv-maths-roadmap-v0.md
§6.3 / Appendix B; docs/proposals/eisv-stage0-bridge-b-label-routing.md).

Parameterized by ``finding_kind`` so each baselined resident maps to its own
``outcome_type`` (``watcher_finding_*``, ``sentinel_finding_*``, …) while sharing
identical precision/label semantics — which keeps their labels directly
comparable. The ``outcome_event`` handler auto-snapshots EISV by ``agent_id``, so
callers attribute the outcome to the **resident's own UUID** (not an operator's).

This generalizes the Watcher-local ``build_resolution_outcome_args`` in
``agents/watcher/agent.py``; Watcher can migrate onto this in a later DRY pass.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


def build_resolution_outcome_args(
    finding_kind: str,
    status: str,
    fingerprint: str,
    agent_uuid: str,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Map a finding resolution to an external-truth ``outcome_event`` payload.

    A *confirmed* finding means the resident's analytical judgment was RIGHT (a
    good outcome). Only a false-positive dismissal (``reason == "fp"``) means it
    was WRONG (a bad outcome) — the sole true-negative in precision math. Other
    dismissals (``out_of_scope``, ``wont_fix``, ``dup``, ``unclear``, ``stale``)
    drop a *valid* finding that just won't be actioned, so the resident was still
    right and the outcome is NOT bad.

    ``finding_kind`` is the resident's finding family (e.g. ``"watcher_finding"``,
    ``"sentinel_finding"``); it prefixes the outcome_type. ``agent_uuid`` must be
    the resident's own UUID so the handler snapshots that resident's EISV.
    """
    confirmed = status == "confirmed"
    is_bad = (not confirmed) and (reason == "fp")
    return {
        "agent_id": agent_uuid,
        "outcome_type": f"{finding_kind}_confirmed" if confirmed else f"{finding_kind}_dismissed",
        "is_bad": is_bad,
        "verification_source": "external_signal",
        "detail": {
            "fingerprint": fingerprint,
            "resolution": status,
            "reason": reason or "",
        },
    }
