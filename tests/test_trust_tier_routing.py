"""Substrate-earned tier routing — S6 Option B.

S6 (options appendix) — routing shortcuts
tier=3 for R4-verified substrate-earned agents, falls through to
`compute_trust_tier` for session-like agents.
"""
from unittest.mock import AsyncMock, patch

import pytest

from src.identity import trust_tier_routing


@pytest.mark.asyncio
async def test_session_like_no_substrate_tag_falls_through_without_db():
    """Fast path: tags lack any substrate-class entry — skip R4 check,
    run compute_trust_tier, no DB call for verify_substrate_earned."""
    metadata = {"trajectory_genesis": {"observation_count": 5, "identity_confidence": 0.3}}

    # Sentinel that will blow up if verify_substrate_earned is called
    with patch.object(trust_tier_routing, "__name__", trust_tier_routing.__name__):
        result = await trust_tier_routing.resolve_trust_tier(
            "7bf970d4-5713-4184-a6f8-58e798275f3f",
            metadata,
            prefetched_tags=["ephemeral"],
            prefetched_label="test-session",
        )
    # compute_trust_tier returns tier 1 for genesis-only + low confidence
    assert result["tier"] == 1
    assert result.get("source") != "substrate_earned"


@pytest.mark.asyncio
async def test_session_like_no_tags_and_no_substrate_falls_through_via_db():
    """Slow path: no prefetched_tags — calls verify_substrate_earned, which
    returns earned=False for a plain session-like agent; routing falls
    through to compute_trust_tier."""
    metadata = {"trajectory_genesis": {"observation_count": 5, "identity_confidence": 0.3}}

    async def _not_earned(agent_uuid):
        return {
            "earned": False,
            "conditions": {
                "dedicated_substrate": False,
                "sustained_behavior": False,
                "declared_role": False,
            },
            "reasons": ["session-like"],
            "evidence": {"agent_uuid": agent_uuid},
        }

    with patch("src.identity.substrate.verify_substrate_earned", _not_earned):
        result = await trust_tier_routing.resolve_trust_tier(
            "7bf970d4-5713-4184-a6f8-58e798275f3f",
            metadata,
        )
    assert result["tier"] == 1
    assert result.get("source") != "substrate_earned"


@pytest.mark.asyncio
async def test_substrate_earned_prefetched_short_circuits_to_tier_3():
    """With prefetched_tags including 'embodied' AND the R4 predicate
    passing, routing returns tier=3 without touching compute_trust_tier."""
    # Trajectory data that would ordinarily be well below tier 3 thresholds
    metadata = {
        "trajectory_genesis": {"observation_count": 10, "identity_confidence": 0.4},
        "trajectory_current": {"observation_count": 12, "identity_confidence": 0.5},
    }

    def _earned(**kwargs):
        return {
            "earned": True,
            "conditions": {
                "dedicated_substrate": True,
                "sustained_behavior": True,
                "declared_role": True,
            },
            "reasons": ["embodied substrate + 12 obs + declared role"],
            "evidence": {"observation_count": 12, "agent_uuid": kwargs.get("agent_uuid")},
        }

    with patch("src.identity.substrate.evaluate_substrate_earned", _earned):
        result = await trust_tier_routing.resolve_trust_tier(
            "00000000-0000-0000-0000-lumenfixture",
            metadata,
            prefetched_tags=["embodied"],
            prefetched_label="Lumen",
        )
    assert result["tier"] == 3
    assert result["name"] == "verified"
    assert result["source"] == "substrate_earned"
    assert result["observation_count"] == 12
    assert result["identity_confidence"] == 0.5


@pytest.mark.asyncio
async def test_substrate_tag_present_but_predicate_fails_falls_through():
    """prefetched_tags includes a substrate class but R4 predicate fails
    (e.g., anchor file missing) — routing falls through to
    compute_trust_tier so session-like logic still applies."""
    metadata = {
        "trajectory_genesis": {"observation_count": 2, "identity_confidence": 0.2},
    }

    def _not_earned(**kwargs):
        return {
            "earned": False,
            "conditions": {
                "dedicated_substrate": False,  # anchor missing
                "sustained_behavior": True,
                "declared_role": True,
            },
            "reasons": ["no anchor file pairs UUID with substrate slot"],
            "evidence": {"agent_uuid": kwargs.get("agent_uuid")},
        }

    with patch("src.identity.substrate.evaluate_substrate_earned", _not_earned):
        result = await trust_tier_routing.resolve_trust_tier(
            "7bf970d4-5713-4184-a6f8-58e798275f3f",
            metadata,
            prefetched_tags=["persistent"],
            prefetched_label="Watcher",
        )
    assert result["tier"] == 1
    assert result.get("source") != "substrate_earned"


@pytest.mark.asyncio
async def test_substrate_check_exception_falls_through():
    """If verify_substrate_earned raises, routing must not propagate —
    falls through to compute_trust_tier so the caller still gets a tier."""
    metadata = {"trajectory_genesis": {"observation_count": 5, "identity_confidence": 0.3}}

    async def _boom(agent_uuid):
        raise RuntimeError("DB unavailable")

    with patch("src.identity.substrate.verify_substrate_earned", _boom):
        result = await trust_tier_routing.resolve_trust_tier(
            "7bf970d4-5713-4184-a6f8-58e798275f3f",
            metadata,
        )
    assert result["tier"] == 1  # from compute_trust_tier, not a crash
