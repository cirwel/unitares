"""Bootstrap check-in helper for onboard.initial_state.


The handler's job at the call site is small: build the BootstrapStateParams,
fill server defaults, decide substrate-earned exemption, then run the write
under a tight timeout. This module owns the digest contract, the defaults,
and the timeout-safe write so the handler stays readable.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict, Optional, Tuple

from src.mcp_handlers.schemas.core import BootstrapStateParams

logger = logging.getLogger(__name__)


# Cross-substrate Pi residents not in core.substrate_claims (which is the
# Mac-side S19 registry). Keep this list short — when a Pi-side equivalent
# of S19 lands, this allowlist gets retired.
PI_RESIDENT_ALLOWLIST: frozenset[str] = frozenset()


# Per-field defaults for fields the caller omitted (spec §3.1 table).
_DEFAULTS = {
    "complexity": 0.5,
    "confidence": 0.5,
    "task_type": "introspection",
    "ethical_drift": [0.0, 0.0, 0.0],
}


def compute_bootstrap_digest(params: BootstrapStateParams) -> str:
    """SHA-256 hex digest over caller-supplied fields only.

    Server-applied defaults are excluded so two callers passing the same
    explicit fields produce the same digest, regardless of default-fill
    behavior. Only fields the caller actually set (i.e. not None) are
    included in the canonical JSON.
    """
    explicit = {
        k: v for k, v in params.model_dump(exclude_none=True).items()
    }
    canonical = json.dumps(explicit, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def derive_bootstrap_response_text(
    params: BootstrapStateParams,
    *,
    client_hint: Optional[str],
    purpose: Optional[str],
) -> str:
    """Default `response_text` per spec §3.1: prefer caller, then client_hint,
    then purpose, then a constant fallback."""
    if params.response_text:
        return params.response_text
    seed = client_hint or purpose or "session-start"
    return f"[bootstrap] {seed}"


def fill_defaults(
    params: BootstrapStateParams,
    *,
    client_hint: Optional[str] = None,
    purpose: Optional[str] = None,
) -> Dict[str, Any]:
    """Resolve a BootstrapStateParams to the concrete state-row payload.

    Caller-supplied fields win; absent fields get the §3.1 defaults.
    Returns the dict we hand to record_bootstrap_state plus the state_json
    metadata (`source`, `bootstrap_digest`).
    """
    digest = compute_bootstrap_digest(params)
    return {
        "response_text": derive_bootstrap_response_text(
            params, client_hint=client_hint, purpose=purpose
        ),
        "complexity": params.complexity if params.complexity is not None else _DEFAULTS["complexity"],
        "confidence": params.confidence if params.confidence is not None else _DEFAULTS["confidence"],
        "task_type": params.task_type or _DEFAULTS["task_type"],
        "ethical_drift": params.ethical_drift or list(_DEFAULTS["ethical_drift"]),
        "bootstrap_digest": digest,
    }


def build_state_json(filled: Dict[str, Any]) -> Dict[str, Any]:
    """Compose the JSONB blob persisted on the bootstrap row.

    `source` is descriptive; `bootstrap_digest` is the audit trail for
    payload_digest_match comparisons. The behavioral fields are stored too
    so future read paths can introspect what the caller asked for without
    re-deriving from EISV columns."""
    return {
        "source": "bootstrap",
        "bootstrap_digest": filled["bootstrap_digest"],
        "response_text": filled["response_text"],
        "complexity": filled["complexity"],
        "confidence": filled["confidence"],
        "task_type": filled["task_type"],
        "ethical_drift": filled["ethical_drift"],
        "epistemic_class": "synthetic",
    }


async def write_bootstrap(
    db: Any,
    *,
    identity_id: int,
    agent_id: str,
    params: BootstrapStateParams,
    client_hint: Optional[str] = None,
    purpose: Optional[str] = None,
) -> Dict[str, Any]:
    """Run the full bootstrap-write flow.

    Returns the `bootstrap` block to embed in the onboard response. Possible shapes:

      {written: True,  state_id: <int>, next_step: "..."}
      {written: False, state_id: <existing>, payload_digest_match: <bool>}
      {written: False, reason: "substrate-earned-exempt"}
      {written: False, reason: "error", detail: "<class>"}

    Idempotent (DB-level via the unique partial index from migration 018):
    a second call for the same identity returns the existing row's state_id
    with payload_digest_match telling the caller whether their payload
    matched the stored one.
    """
    if await db.is_substrate_earned(agent_id):
        return {"written": False, "reason": "substrate-earned-exempt"}

    filled = fill_defaults(params, client_hint=client_hint, purpose=purpose)
    state_json = build_state_json(filled)

    # EISV column values — bootstrap defaults map cleanly to nominal regime,
    # midpoint stability, low volatility. These seed the trajectory ODE; the
    # synthetic flag keeps them out of measured-state aggregations.
    insert_kwargs = {
        "identity_id": identity_id,
        "entropy": 0.5,
        "integrity": 0.5,
        "stability_index": 0.5,
        "void": 0.0,
        "regime": "nominal",
        "coherence": 1.0,
        "state_json": state_json,
    }

    try:
        state_id, was_written = await db.record_bootstrap_state(**insert_kwargs)
    except Exception as exc:  # noqa: BLE001 — bootstrap is fail-open
        logger.warning(
            "[BOOTSTRAP] record_bootstrap_state failed for identity_id=%s: %s",
            identity_id, exc,
        )
        return {"written": False, "reason": "error", "detail": type(exc).__name__}

    if was_written:
        return {
            "written": True,
            "state_id": state_id,
            "next_step": (
                "Call process_agent_update with real measurements when you "
                "have any — bootstrap is provisional and excluded from "
                "calibration."
            ),
        }

    # Idempotent re-call: compare digests so the caller sees divergence.
    existing = await db.get_bootstrap_state(identity_id)
    stored_digest = (existing or {}).get("state_json", {}).get("bootstrap_digest")
    return {
        "written": False,
        "state_id": state_id,
        "payload_digest_match": stored_digest == filled["bootstrap_digest"],
    }
