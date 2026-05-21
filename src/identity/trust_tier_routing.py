"""Trust-tier routing — S6 Option B.

Under ontology v2 agents split into two
populations with incompatible calibration assumptions:

  - Substrate-earned agents (Lumen, Watcher, Sentinel, Vigil, Steward)
    whose identity is anchored to a dedicated substrate and accumulates
    honest per-UUID trajectory data across restarts.
  - Session-like agents (Claude Code tabs, Codex sessions, ephemeral
    subagents) whose UUID lifetime is shorter than the observation
    thresholds `compute_trust_tier` was calibrated against.

This module routes between the two paths:

  - `resolve_trust_tier(agent_uuid, metadata, ...)` — the canonical
    entrypoint. If the agent passes R4's substrate-earned predicate
    (see `src/identity/substrate.py`), returns tier=3 immediately.
    Otherwise falls through to the existing per-UUID
    `compute_trust_tier` logic.

The routing keeps substrate-earned agents' tier independent of the
observation-count thresholds that session-like agents can never reach,
and isolates future empirical recalibration (blocked on S8a tag
discipline) to `compute_trust_tier` alone.

S6 (options doc + resolution) for the
design-space analysis and the operator decision behind Option B.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Optional

from src.logging_utils import get_logger

logger = get_logger(__name__)


def _substrate_earned_tier_dict(
    *,
    agent_uuid: str,
    verdict: Dict[str, Any],
    metadata: Dict[str, Any],
) -> Dict[str, Any]:
    """Shape a substrate-earned verdict into the tier dict contract.

    Returns the same keys as `compute_trust_tier` plus a `source` tag
    for observability — callers that only check `tier`/`name` are
    unaffected by the extra field.
    """
    evidence = verdict.get("evidence") or {}
    observation_count = evidence.get("observation_count", 0)
    current = metadata.get("trajectory_current") or {}
    if not isinstance(current, dict):
        current = {}
    genesis = metadata.get("trajectory_genesis") or {}
    if not isinstance(genesis, dict):
        genesis = {}
    confidence = current.get("identity_confidence") or genesis.get("identity_confidence") or 1.0
    return {
        "tier": 3,
        "name": "verified",
        "observation_count": observation_count,
        "identity_confidence": round(float(confidence), 4),
        "lineage_similarity": None,
        "reason": "Substrate-earned (R4 three-condition pass)",
        "source": "substrate_earned",
        "conditions": verdict.get("conditions"),
    }


def _provisional_lineage_tier_dict() -> Dict[str, Any]:
    """Tier dict for an agent whose lineage is currently provisional.

    Per R1 v3.3-D consumer table: 'Read provisional_lineage; if true, do
    not contribute to tier upgrades.' The simplest faithful read of that
    contract is to return tier=1 (the baseline / lowest tier) regardless
    of substrate-earned or compute_trust_tier verdict — a provisional
    successor cannot promote to a higher tier until the lineage is
    confirmed via `confirm_lineage`.

    Same shape as `compute_trust_tier` for caller-API compatibility plus a
    `source` tag so observability / dashboards can distinguish a
    provisional gate from a substrate-earned or compute-derived tier.
    """
    return {
        "tier": 1,
        "name": "provisional",
        "observation_count": 0,
        "identity_confidence": 0.0,
        "lineage_similarity": None,
        "reason": "Provisional lineage (R1 v3.3-D); awaiting confirm_lineage to promote",
        "source": "provisional_lineage_gate",
        "conditions": None,  # parity with _substrate_earned_tier_dict + compute_trust_tier
    }


async def _is_provisional_lineage(agent_uuid: str) -> bool:
    """Lookup of `core.identities.provisional_lineage` for the gate.

    Delegates to `IdentityMixin.is_lineage_provisional` (mixin-level so
    test conftest can stub via `_isolate_db_backend` without leaking
    AsyncMock coroutines through an inline `async with backend.acquire()`).
    PR 1's migration 031 added the SQL column; extending IdentityRecord
    is deferred. When the dataclass is extended, callers can pass
    `prefetched_provisional` to skip this lookup entirely.

    Returns False on any error — a failed lookup should not silently gate
    legitimate tier upgrades. Logged at debug for observability.
    """
    try:
        from src.db import get_db
        backend = get_db()
        result = await backend.is_lineage_provisional(agent_uuid)
        # Strict-identity check: anything other than literal True (e.g. an
        # AsyncMock from a test that didn't stub the method, or a non-bool
        # return) is treated as not-provisional. The gate's failure mode is
        # to under-fire (allowing a tier upgrade for an unmocked test path)
        # rather than over-fire (silently demoting a legitimately-tiered
        # production agent because of a mock-shape accident).
        return result is True
    except Exception as e:
        logger.debug(
            f"[trust_tier_routing] provisional_lineage lookup failed for "
            f"{agent_uuid[:8]}...: {e}; treating as not-provisional"
        )
        return False


async def resolve_trust_tier(
    agent_uuid: str,
    metadata: Dict[str, Any],
    *,
    prefetched_tags: Optional[Iterable[str]] = None,
    prefetched_label: Optional[str] = None,
    prefetched_provisional: Optional[bool] = None,
) -> Dict[str, Any]:
    """Route to provisional-gate (R1 v3.3-D), substrate-earned (R4), or
    session-like (`compute_trust_tier`).

    Returns a tier dict with the same shape as `compute_trust_tier`.

    Provisional gate (R1 v3.3-D): if `provisional_lineage=True` on the
    agent's `core.identities` row, return tier=1 with source='provisional_
    lineage_gate' regardless of substrate or compute verdict. Callers that
    have provisional state in hand pass `prefetched_provisional=True/False`
    to skip the DB lookup; otherwise this function does its own read.

    Fast path: if `prefetched_tags` is provided and does not include a
    substrate-class tag (`embodied`/`persistent`), skip the R4 predicate
    and go straight to `compute_trust_tier`. This keeps the batch
    callers cheap when their in-memory metadata already has tags.

    Slow path: if `prefetched_tags` is None, run `verify_substrate_earned`
    which does its own DB lookup. This preserves correctness when callers
    don't have tags handy, at the cost of one extra DB roundtrip.

    On any exception in the substrate-earned check, fall through to
    `compute_trust_tier` — a failed R4 lookup should not block tier
    computation. The provisional gate fails-soft the same way.
    """
    from src.trajectory_identity import compute_trust_tier

    # R1 v3.3-D provisional gate — runs FIRST so a provisional successor
    # cannot collect tier=3 via the substrate-earned shortcut.
    is_provisional = prefetched_provisional
    if is_provisional is None:
        is_provisional = await _is_provisional_lineage(agent_uuid)
    if is_provisional:
        return _provisional_lineage_tier_dict()

    try:
        if prefetched_tags is not None:
            tag_set = {t for t in prefetched_tags if isinstance(t, str)}
            if not any(t in tag_set for t in ("embodied", "persistent")):
                return compute_trust_tier(metadata)
            from src.identity.substrate import evaluate_substrate_earned
            verdict = evaluate_substrate_earned(
                agent_uuid=agent_uuid,
                label=prefetched_label,
                tags=list(tag_set),
                metadata=metadata,
            )
        else:
            from src.identity.substrate import verify_substrate_earned
            verdict = await verify_substrate_earned(agent_uuid)

        if verdict.get("earned"):
            return _substrate_earned_tier_dict(
                agent_uuid=agent_uuid,
                verdict=verdict,
                metadata=metadata,
            )
    except Exception as e:
        logger.debug(
            f"[trust_tier_routing] substrate check failed for "
            f"{agent_uuid[:8]}...: {e}; falling through to compute_trust_tier"
        )

    return compute_trust_tier(metadata)


__all__ = ["resolve_trust_tier"]
