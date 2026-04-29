#!/usr/bin/env python3
"""
Tests for trajectory identity integration.

Verifies the trajectory identity framework from:
- src/trajectory_identity.py (UNITARES side)
- Integration with process_agent_update

Tests cover:
1. TrajectorySignature dataclass and serialization
2. Genesis storage (Σ₀) - first signature becomes anchor
3. Lineage comparison - subsequent signatures compared to genesis
4. Anomaly detection - similarity < 0.6 triggers warning
5. Two-tier verification (coherence + lineage)
"""

import pytest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch


class TestTrajectorySignature:
    """Unit tests for TrajectorySignature dataclass."""

    def test_create_empty_signature(self):
        """Should create signature with default empty values."""
        from src.trajectory_identity import TrajectorySignature

        sig = TrajectorySignature()

        assert sig.preferences == {}
        assert sig.beliefs == {}
        assert sig.attractor is None
        assert sig.recovery == {}
        assert sig.relational == {}
        assert sig.observation_count == 0
        assert sig.stability_score == 0.0
        assert sig.identity_confidence == 0.0

    def test_from_dict(self):
        """Should create signature from dictionary."""
        from src.trajectory_identity import TrajectorySignature

        data = {
            "preferences": {"vector": [0.5, 0.3, 0.7]},
            "beliefs": {"values": [0.8, 0.6], "avg_confidence": 0.7},
            "attractor": {"center": [0.5, 0.5, 0.5, 0.5], "variance": 0.1},
            "recovery": {"tau_estimate": 10.5},
            "relational": {"n_relationships": 3},
            "observation_count": 100,
            "stability_score": 0.85,
            "identity_confidence": 0.9,
        }

        sig = TrajectorySignature.from_dict(data)

        assert sig.preferences == {"vector": [0.5, 0.3, 0.7]}
        assert sig.beliefs["avg_confidence"] == 0.7
        assert sig.attractor["center"] == [0.5, 0.5, 0.5, 0.5]
        assert sig.recovery["tau_estimate"] == 10.5
        assert sig.observation_count == 100
        assert sig.identity_confidence == 0.9

    def test_to_dict(self):
        """Should serialize signature to dictionary."""
        from src.trajectory_identity import TrajectorySignature

        sig = TrajectorySignature(
            preferences={"vector": [0.5, 0.3]},
            beliefs={"values": [0.8]},
            observation_count=50,
            stability_score=0.75,
        )

        result = sig.to_dict()

        assert result["preferences"] == {"vector": [0.5, 0.3]}
        assert result["observation_count"] == 50
        assert result["stability_score"] == 0.75
        assert "computed_at" in result

    def test_similarity_identical(self):
        """Identical signatures should have similarity ~1.0."""
        from src.trajectory_identity import TrajectorySignature

        sig1 = TrajectorySignature(
            attractor={"center": [0.5, 0.5, 0.5, 0.5]},
            beliefs={"values": [0.8, 0.6, 0.7]},
            recovery={"tau_estimate": 10.0},
            stability_score=0.8,
        )
        sig2 = TrajectorySignature(
            attractor={"center": [0.5, 0.5, 0.5, 0.5]},
            beliefs={"values": [0.8, 0.6, 0.7]},
            recovery={"tau_estimate": 10.0},
            stability_score=0.8,
        )

        similarity = sig1.similarity(sig2)
        assert similarity > 0.95, f"Expected ~1.0, got {similarity}"

    def test_similarity_different(self):
        """Very different signatures should have low similarity."""
        from src.trajectory_identity import TrajectorySignature

        sig1 = TrajectorySignature(
            attractor={"center": [0.1, 0.1, 0.1, 0.1]},
            beliefs={"values": [0.2, 0.2, 0.2]},
            recovery={"tau_estimate": 5.0},
            stability_score=0.3,
        )
        sig2 = TrajectorySignature(
            attractor={"center": [0.9, 0.9, 0.9, 0.9]},
            beliefs={"values": [0.9, 0.9, 0.9]},
            recovery={"tau_estimate": 50.0},
            stability_score=0.9,
        )

        similarity = sig1.similarity(sig2)
        assert similarity < 0.5, f"Expected low similarity, got {similarity}"

    def test_similarity_no_data(self):
        """Signatures with no comparable data should return 0.5."""
        from src.trajectory_identity import TrajectorySignature

        sig1 = TrajectorySignature()
        sig2 = TrajectorySignature()

        similarity = sig1.similarity(sig2)
        assert similarity == 0.5, f"Expected 0.5 (no data), got {similarity}"


class TestGenesisStorage:
    """Tests for genesis signature (Σ₀) storage."""

    @pytest.mark.asyncio
    async def test_store_genesis_new_agent(self):
        """First signature should be stored as genesis."""
        from src.trajectory_identity import store_genesis_signature, TrajectorySignature

        sig = TrajectorySignature(
            observation_count=10,
            identity_confidence=0.8,
        )

        # Mock database (patch at source, not import location)
        with patch('src.db.get_db') as mock_db:
            mock_identity = MagicMock()
            mock_identity.metadata = {}  # No existing genesis

            mock_db_instance = AsyncMock()
            mock_db_instance.get_identity = AsyncMock(return_value=mock_identity)
            mock_db_instance.update_identity_metadata = AsyncMock()
            mock_db.return_value = mock_db_instance

            result = await store_genesis_signature("test-agent-uuid", sig)

            assert result is True
            mock_db_instance.update_identity_metadata.assert_called_once()

    @pytest.mark.asyncio
    async def test_genesis_immutable_at_tier_2(self):
        """Genesis should not be overwritten at tier 2+."""
        from src.trajectory_identity import store_genesis_signature, TrajectorySignature

        sig = TrajectorySignature(observation_count=100, identity_confidence=0.9)

        with patch('src.db.get_db') as mock_db:
            mock_identity = MagicMock()
            mock_identity.metadata = {
                "trajectory_genesis": {"observation_count": 10, "identity_confidence": 0.3},
                "trust_tier": {"tier": 2, "name": "established"},
            }

            mock_db_instance = AsyncMock()
            mock_db_instance.get_identity = AsyncMock(return_value=mock_identity)
            mock_db.return_value = mock_db_instance

            result = await store_genesis_signature("test-agent-uuid", sig)

            # Should return False - genesis immutable at tier 2+
            assert result is False

    @pytest.mark.asyncio
    async def test_genesis_reseed_at_tier_1(self):
        """Genesis can be reseeded at tier 1 if new confidence is 1.5x higher."""
        from src.trajectory_identity import store_genesis_signature, TrajectorySignature

        # New signature with much higher confidence
        sig = TrajectorySignature(observation_count=50, identity_confidence=0.25)

        with patch('src.db.get_db') as mock_db:
            mock_identity = MagicMock()
            mock_identity.metadata = {
                "trajectory_genesis": {"observation_count": 10, "identity_confidence": 0.05},
                "trust_tier": {"tier": 1, "name": "emerging"},
            }

            mock_db_instance = AsyncMock()
            mock_db_instance.get_identity = AsyncMock(return_value=mock_identity)
            mock_db_instance.update_identity_metadata = AsyncMock()
            mock_db.return_value = mock_db_instance

            result = await store_genesis_signature("test-agent-uuid", sig)

            assert result is True
            mock_db_instance.update_identity_metadata.assert_called_once()

    @pytest.mark.asyncio
    async def test_genesis_no_reseed_insufficient_improvement(self):
        """Genesis not reseeded if new confidence isn't 1.5x higher."""
        from src.trajectory_identity import store_genesis_signature, TrajectorySignature

        sig = TrajectorySignature(observation_count=30, identity_confidence=0.10)

        with patch('src.db.get_db') as mock_db:
            mock_identity = MagicMock()
            mock_identity.metadata = {
                "trajectory_genesis": {"observation_count": 10, "identity_confidence": 0.08},
                "trust_tier": {"tier": 1, "name": "emerging"},
            }

            mock_db_instance = AsyncMock()
            mock_db_instance.get_identity = AsyncMock(return_value=mock_identity)
            mock_db.return_value = mock_db_instance

            result = await store_genesis_signature("test-agent-uuid", sig)

            # 0.10 <= 0.08 * 1.5 = 0.12 → not enough improvement
            assert result is False

    @pytest.mark.asyncio
    async def test_store_genesis_agent_not_found(self):
        """Should return False if agent doesn't exist."""
        from src.trajectory_identity import store_genesis_signature, TrajectorySignature

        sig = TrajectorySignature()

        with patch('src.db.get_db') as mock_db:
            mock_db_instance = AsyncMock()
            mock_db_instance.get_identity = AsyncMock(return_value=None)
            mock_db.return_value = mock_db_instance

            result = await store_genesis_signature("nonexistent-uuid", sig)

            assert result is False


class TestUpdateCurrentSignature:
    """Tests for update_current_signature function."""

    @pytest.mark.asyncio
    async def test_first_update_creates_genesis(self):
        """First trajectory update should create genesis."""
        from src.trajectory_identity import update_current_signature, TrajectorySignature

        sig = TrajectorySignature(
            observation_count=15,
            identity_confidence=0.75,
        )

        with patch('src.db.get_db') as mock_db, \
             patch('src.trajectory_identity.store_genesis_signature', new_callable=AsyncMock) as mock_store:

            mock_identity = MagicMock()
            mock_identity.metadata = {}  # No genesis yet

            mock_db_instance = AsyncMock()
            mock_db_instance.get_identity = AsyncMock(return_value=mock_identity)
            mock_db_instance.update_identity_metadata = AsyncMock()
            mock_db.return_value = mock_db_instance

            mock_store.return_value = True

            result = await update_current_signature("test-uuid", sig)

            assert result["stored"] is True
            assert result.get("genesis_created") is True
            mock_store.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_compares_to_genesis(self):
        """Subsequent updates should compare to genesis."""
        from src.trajectory_identity import update_current_signature, TrajectorySignature

        # Current signature (slightly different from genesis)
        current_sig = TrajectorySignature(
            attractor={"center": [0.52, 0.48, 0.51, 0.49]},
            observation_count=50,
        )

        with patch('src.db.get_db') as mock_db:
            mock_identity = MagicMock()
            mock_identity.metadata = {
                "trajectory_genesis": {
                    "attractor": {"center": [0.5, 0.5, 0.5, 0.5]},
                    "observation_count": 10,
                }
            }

            mock_db_instance = AsyncMock()
            mock_db_instance.get_identity = AsyncMock(return_value=mock_identity)
            mock_db_instance.update_identity_metadata = AsyncMock()
            mock_db.return_value = mock_db_instance

            result = await update_current_signature("test-uuid", current_sig)

            assert result["stored"] is True
            assert "lineage_similarity" in result
            assert result["lineage_threshold"] == 0.6

    @pytest.mark.asyncio
    async def test_anomaly_detected_low_similarity(self):
        """Should flag anomaly when similarity < 0.6."""
        from src.trajectory_identity import update_current_signature, TrajectorySignature

        # Very different signature
        current_sig = TrajectorySignature(
            attractor={"center": [0.9, 0.9, 0.9, 0.9]},
            beliefs={"values": [0.9, 0.9, 0.9]},
            recovery={"tau_estimate": 100.0},
            stability_score=0.95,
            observation_count=100,
        )

        with patch('src.db.get_db') as mock_db:
            mock_identity = MagicMock()
            mock_identity.metadata = {
                "trajectory_genesis": {
                    "attractor": {"center": [0.1, 0.1, 0.1, 0.1]},
                    "beliefs": {"values": [0.1, 0.1, 0.1]},
                    "recovery": {"tau_estimate": 5.0},
                    "stability_score": 0.2,
                    "observation_count": 10,
                },
                # Tier 2: genesis is immutable, so anomaly is detected, not auto-resolved
                "trust_tier": {"tier": 2, "name": "established"},
            }

            mock_db_instance = AsyncMock()
            mock_db_instance.get_identity = AsyncMock(return_value=mock_identity)
            mock_db_instance.update_identity_metadata = AsyncMock()
            mock_db.return_value = mock_db_instance

            result = await update_current_signature("test-uuid", current_sig)

            assert result["is_anomaly"] is True
            assert "warning" in result
            assert result["lineage_similarity"] < 0.6

    @pytest.mark.asyncio
    async def test_established_drift_does_not_demote_or_reseed(self):
        """Lineage drift should not reset earned trust into a reseed/promotion loop."""
        from src.trajectory_identity import update_current_signature, TrajectorySignature

        current_sig = TrajectorySignature(
            attractor={"center": [0.9, 0.9, 0.9, 0.9]},
            beliefs={"values": [0.9, 0.9, 0.9]},
            recovery={"tau_estimate": 100.0},
            stability_score=0.95,
            observation_count=300,
            identity_confidence=0.9,
        )

        with patch('src.db.get_db') as mock_db, \
             patch('src.trajectory_identity.store_genesis_signature',
                   new_callable=AsyncMock) as mock_store, \
             patch('src.broadcaster.broadcaster_instance') as mock_bc:
            mock_identity = MagicMock()
            mock_identity.metadata = {
                "trajectory_genesis": {
                    "attractor": {"center": [0.1, 0.1, 0.1, 0.1]},
                    "beliefs": {"values": [0.1, 0.1, 0.1]},
                    "recovery": {"tau_estimate": 5.0},
                    "stability_score": 0.2,
                    "observation_count": 10,
                    "identity_confidence": 0.4,
                },
                "trust_tier": {"tier": 2, "name": "established"},
            }

            mock_db_instance = AsyncMock()
            mock_db_instance.get_identity = AsyncMock(return_value=mock_identity)
            mock_db_instance.update_identity_metadata = AsyncMock()
            mock_db.return_value = mock_db_instance

            result = await update_current_signature("test-uuid", current_sig)

            assert result["is_anomaly"] is True
            assert result["trust_tier"]["tier"] == 2
            mock_store.assert_not_called()
            for call in mock_bc.broadcast_event.call_args_list:
                assert call.args[0] != "identity_assurance_change"

    @pytest.mark.asyncio
    async def test_substrate_earned_drift_keeps_verified_assurance(self):
        """Resident substrate routing should keep verified trust separate from drift alerts."""
        from src.trajectory_identity import update_current_signature, TrajectorySignature

        current_sig = TrajectorySignature(
            attractor={"center": [0.9, 0.9, 0.9, 0.9]},
            beliefs={"values": [0.9, 0.9, 0.9]},
            recovery={"tau_estimate": 100.0},
            stability_score=0.95,
            observation_count=300,
            identity_confidence=0.9,
        )

        with patch('src.db.get_db') as mock_db, \
             patch('src.trajectory_identity.store_genesis_signature',
                   new_callable=AsyncMock) as mock_store, \
             patch('src.broadcaster.broadcaster_instance') as mock_bc:
            mock_identity = MagicMock()
            mock_identity.metadata = {
                "tags": ["embodied"],
                "label": "Sentinel",
                "trajectory_genesis": {
                    "attractor": {"center": [0.1, 0.1, 0.1, 0.1]},
                    "beliefs": {"values": [0.1, 0.1, 0.1]},
                    "recovery": {"tau_estimate": 5.0},
                    "stability_score": 0.2,
                    "observation_count": 10,
                    "identity_confidence": 0.4,
                },
                "trust_tier": {"tier": 3, "name": "verified"},
            }

            mock_db_instance = AsyncMock()
            mock_db_instance.get_identity = AsyncMock(return_value=mock_identity)
            mock_db_instance.update_identity_metadata = AsyncMock()
            mock_db.return_value = mock_db_instance

            result = await update_current_signature("test-uuid", current_sig)

            assert result["is_anomaly"] is True
            assert result["trust_tier"]["tier"] == 3
            assert result["trust_tier"]["name"] == "verified"
            assert result["trust_tier"]["source"] == "substrate_earned"
            mock_store.assert_not_called()
            for call in mock_bc.broadcast_event.call_args_list:
                assert call.args[0] != "identity_assurance_change"

    @pytest.mark.asyncio
    async def test_reseed_preserves_trust_tier_no_spurious_broadcast(self):
        """Reseed path must not lose trust_tier, causing broadcast on every check-in.

        Regression test: store_genesis_signature writes metadata without trust_tier,
        then update_current_signature re-reads from DB losing the tier.  This made
        old_tier=0 on every call, so the tier-change broadcast fired every time.
        """
        from src.trajectory_identity import update_current_signature, TrajectorySignature

        current_sig = TrajectorySignature(
            attractor={"center": [0.5, 0.5, 0.5, 0.5]},
            beliefs={"values": [0.5, 0.5, 0.5]},
            recovery={"tau_estimate": 10.0},
            stability_score=0.6,
            observation_count=30,
            identity_confidence=0.8,
        )

        existing_trust_tier = {"tier": 1, "name": "emerging",
                               "observation_count": 20, "identity_confidence": 0.6,
                               "lineage_similarity": None, "reason": "test"}

        # Metadata before the call: has genesis, trust_tier at 1
        pre_metadata = {
            "trajectory_genesis": {
                "attractor": {"center": [0.4, 0.4, 0.4, 0.4]},
                "beliefs": {"values": [0.4, 0.4, 0.4]},
                "recovery": {"tau_estimate": 8.0},
                "stability_score": 0.4,
                "observation_count": 10,
                "identity_confidence": 0.4,
            },
            "trust_tier": existing_trust_tier,
        }

        # After reseed, DB returns metadata WITHOUT trust_tier (the bug scenario)
        post_reseed_metadata = {
            "trajectory_genesis": current_sig.to_dict(),
            "trajectory_genesis_at": "2026-04-15T00:00:00+00:00",
            # trust_tier is MISSING — this is what store_genesis_signature wrote
        }

        call_count = [0]

        async def get_identity_side_effect(agent_id):
            call_count[0] += 1
            mock = MagicMock()
            if call_count[0] == 1:
                mock.metadata = dict(pre_metadata)  # first read: has trust_tier
            else:
                mock.metadata = dict(post_reseed_metadata)  # after reseed: no trust_tier
            return mock

        with patch('src.db.get_db') as mock_db, \
             patch('src.trajectory_identity.store_genesis_signature', new_callable=AsyncMock) as mock_store, \
             patch('src.broadcaster.broadcaster_instance') as mock_bc:

            mock_db_instance = AsyncMock()
            mock_db_instance.get_identity = AsyncMock(side_effect=get_identity_side_effect)
            mock_db_instance.update_identity_metadata = AsyncMock()
            mock_db.return_value = mock_db_instance

            mock_store.return_value = True  # reseed happens

            result = await update_current_signature("test-uuid", current_sig)

            assert result.get("genesis_reseeded") is True

            # The trust tier should still be "emerging" (tier 1) — compute_trust_tier
            # may recompute it but the OLD tier should also be 1, so no broadcast.
            # The key assertion: broadcast_event should NOT have been called with
            # identity_assurance_change (tier didn't actually change from 1).
            for call in mock_bc.broadcast_event.call_args_list:
                if call.args and call.args[0] == "identity_assurance_change":
                    payload = call.kwargs.get("payload", {})
                    # If it did fire, old and new must differ
                    assert payload.get("old_tier") != payload.get("new_tier"), \
                        f"Spurious broadcast: old_tier={payload.get('old_tier')} == new_tier={payload.get('new_tier')}"


class TestGetTrajectoryStatus:
    """Tests for get_trajectory_status function."""

    @pytest.mark.asyncio
    async def test_status_no_trajectory(self):
        """Agent without trajectory data should report has_genesis=False."""
        from src.trajectory_identity import get_trajectory_status

        with patch('src.db.get_db') as mock_db:
            mock_identity = MagicMock()
            mock_identity.metadata = {}  # No trajectory data

            mock_db_instance = AsyncMock()
            mock_db_instance.get_identity = AsyncMock(return_value=mock_identity)
            mock_db.return_value = mock_db_instance

            result = await get_trajectory_status("test-uuid")

            assert result["has_genesis"] is False
            assert result["has_current"] is False

    @pytest.mark.asyncio
    async def test_status_with_genesis_only(self):
        """Agent with genesis but no current should report appropriately."""
        from src.trajectory_identity import get_trajectory_status

        with patch('src.db.get_db') as mock_db:
            mock_identity = MagicMock()
            mock_identity.metadata = {
                "trajectory_genesis": {
                    "observation_count": 10,
                    "identity_confidence": 0.7,
                },
                "trajectory_genesis_at": "2026-02-04T10:00:00",
            }

            mock_db_instance = AsyncMock()
            mock_db_instance.get_identity = AsyncMock(return_value=mock_identity)
            mock_db.return_value = mock_db_instance

            result = await get_trajectory_status("test-uuid")

            assert result["has_genesis"] is True
            assert result["has_current"] is False
            assert result["genesis_observations"] == 10

    @pytest.mark.asyncio
    async def test_status_with_both(self):
        """Agent with genesis and current should include lineage comparison."""
        from src.trajectory_identity import get_trajectory_status

        with patch('src.db.get_db') as mock_db:
            mock_identity = MagicMock()
            mock_identity.metadata = {
                "trajectory_genesis": {
                    "attractor": {"center": [0.5, 0.5, 0.5, 0.5]},
                    "observation_count": 10,
                    "identity_confidence": 0.7,
                },
                "trajectory_current": {
                    "attractor": {"center": [0.5, 0.5, 0.5, 0.5]},
                    "observation_count": 50,
                    "identity_confidence": 0.85,
                },
            }

            mock_db_instance = AsyncMock()
            mock_db_instance.get_identity = AsyncMock(return_value=mock_identity)
            mock_db.return_value = mock_db_instance

            result = await get_trajectory_status("test-uuid")

            assert result["has_genesis"] is True
            assert result["has_current"] is True
            assert "lineage_similarity" in result


class TestVerifyTrajectoryIdentity:
    """Tests for two-tier identity verification."""

    @pytest.mark.asyncio
    async def test_verify_passes_both_tiers(self):
        """Signature similar to both current and genesis should pass."""
        from src.trajectory_identity import verify_trajectory_identity, TrajectorySignature

        test_sig = TrajectorySignature(
            attractor={"center": [0.5, 0.5, 0.5, 0.5]},
            observation_count=60,
        )

        with patch('src.db.get_db') as mock_db:
            mock_identity = MagicMock()
            mock_identity.metadata = {
                "trajectory_genesis": {
                    "attractor": {"center": [0.5, 0.5, 0.5, 0.5]},
                },
                "trajectory_current": {
                    "attractor": {"center": [0.5, 0.5, 0.5, 0.5]},
                },
            }

            mock_db_instance = AsyncMock()
            mock_db_instance.get_identity = AsyncMock(return_value=mock_identity)
            mock_db.return_value = mock_db_instance

            result = await verify_trajectory_identity("test-uuid", test_sig)

            assert result["verified"] is True

    @pytest.mark.asyncio
    async def test_verify_fails_lineage(self):
        """Signature very different from genesis should fail lineage tier."""
        from src.trajectory_identity import verify_trajectory_identity, TrajectorySignature

        # Very different from genesis
        test_sig = TrajectorySignature(
            attractor={"center": [0.9, 0.9, 0.9, 0.9]},
            beliefs={"values": [0.9, 0.9]},
            recovery={"tau_estimate": 100.0},
            stability_score=0.95,
        )

        with patch('src.db.get_db') as mock_db:
            mock_identity = MagicMock()
            mock_identity.metadata = {
                "trajectory_genesis": {
                    "attractor": {"center": [0.1, 0.1, 0.1, 0.1]},
                    "beliefs": {"values": [0.1, 0.1]},
                    "recovery": {"tau_estimate": 5.0},
                    "stability_score": 0.1,
                },
                "trajectory_current": {
                    "attractor": {"center": [0.9, 0.9, 0.9, 0.9]},  # Current matches
                },
            }

            mock_db_instance = AsyncMock()
            mock_db_instance.get_identity = AsyncMock(return_value=mock_identity)
            mock_db.return_value = mock_db_instance

            result = await verify_trajectory_identity("test-uuid", test_sig)

            assert result["verified"] is False
            assert "lineage" in result.get("failed_tiers", [])


class TestProcessAgentUpdateIntegration:
    """Integration tests for trajectory in process_agent_update."""

    def test_trajectory_signature_format(self):
        """Verify trajectory_signature format matches UNITARES expectations."""
        from src.trajectory_identity import TrajectorySignature

        # Create signature like anima-mcp would
        sig = TrajectorySignature(
            preferences={"vector": [0.5, 0.3, 0.7]},
            beliefs={"values": [0.8, 0.6]},
            attractor={"center": [0.5, 0.5, 0.5, 0.5], "variance": 0.1},
            recovery={"tau_estimate": 10.0},
            relational={"n_relationships": 2},
            observation_count=25,
            stability_score=0.8,
        )
        sig.identity_confidence = 0.75

        # Convert to dict (what would be sent in arguments)
        sig_dict = sig.to_dict()
        sig_dict["identity_confidence"] = sig.identity_confidence

        # Verify required fields for UNITARES
        assert "preferences" in sig_dict
        assert "beliefs" in sig_dict
        assert "attractor" in sig_dict
        assert "recovery" in sig_dict
        assert "observation_count" in sig_dict
        assert "identity_confidence" in sig_dict
        assert sig_dict["observation_count"] == 25
        assert sig_dict["identity_confidence"] == 0.75


class TestComputeTrustTier:
    """Tests for trust tier computation from trajectory metadata."""

    def test_tier_0_no_data(self):
        """No trajectory data = tier 0 (unknown)."""
        from src.trajectory_identity import compute_trust_tier
        result = compute_trust_tier({})
        assert result["tier"] == 0
        assert result["name"] == "unknown"
        assert result["observation_count"] == 0

    def test_tier_0_empty_genesis(self):
        """Empty metadata with no trajectory keys = tier 0."""
        from src.trajectory_identity import compute_trust_tier
        result = compute_trust_tier({"some_other_key": True})
        assert result["tier"] == 0
        assert result["name"] == "unknown"

    def test_tier_1_genesis_only_low_obs(self):
        """Genesis only, low observations = tier 1 (emerging)."""
        from src.trajectory_identity import compute_trust_tier
        metadata = {
            "trajectory_genesis": {
                "observation_count": 10,
                "identity_confidence": 0.3,
            }
        }
        result = compute_trust_tier(metadata)
        assert result["tier"] == 1
        assert result["name"] == "emerging"
        assert result["observation_count"] == 10

    def test_tier_1_with_current_low_confidence(self):
        """Current exists but low confidence = tier 1."""
        from src.trajectory_identity import compute_trust_tier
        metadata = {
            "trajectory_genesis": {
                "attractor": {"center": [0.5, 0.5, 0.5, 0.5]},
                "observation_count": 10,
                "identity_confidence": 0.3,
            },
            "trajectory_current": {
                "attractor": {"center": [0.52, 0.48, 0.51, 0.49]},
                "observation_count": 60,
                "identity_confidence": 0.4,
            }
        }
        result = compute_trust_tier(metadata)
        assert result["tier"] == 1
        assert result["name"] == "emerging"

    def test_tier_2_established(self):
        """50+ observations, confidence >= 0.5, similar attractors = tier 2."""
        from src.trajectory_identity import compute_trust_tier
        metadata = {
            "trajectory_genesis": {
                "attractor": {"center": [0.5, 0.5, 0.5, 0.5]},
                "observation_count": 10,
                "identity_confidence": 0.4,
            },
            "trajectory_current": {
                "attractor": {"center": [0.52, 0.48, 0.51, 0.49]},
                "observation_count": 75,
                "identity_confidence": 0.6,
            }
        }
        result = compute_trust_tier(metadata)
        assert result["tier"] == 2
        assert result["name"] == "established"
        assert result["lineage_similarity"] is not None
        assert result["lineage_similarity"] > 0.7

    def test_tier_2_genesis_only_high_obs(self):
        """Genesis only (no current), high obs and confidence = tier 2.
        Lineage is None since there's no current to compare."""
        from src.trajectory_identity import compute_trust_tier
        metadata = {
            "trajectory_genesis": {
                "observation_count": 100,
                "identity_confidence": 0.7,
            }
        }
        result = compute_trust_tier(metadata)
        # Uses genesis as sig_data, lineage is None (allowed for tier 2)
        assert result["tier"] == 2
        assert result["name"] == "established"
        assert result["lineage_similarity"] is None

    def test_tier_3_verified(self):
        """200+ observations, high confidence, nearly identical sigs = tier 3."""
        from src.trajectory_identity import compute_trust_tier
        metadata = {
            "trajectory_genesis": {
                "attractor": {"center": [0.5, 0.5, 0.5, 0.5]},
                "beliefs": {"values": [0.8, 0.6, 0.7]},
                "recovery": {"tau_estimate": 10.0},
                "stability_score": 0.8,
                "observation_count": 10,
                "identity_confidence": 0.5,
            },
            "trajectory_current": {
                "attractor": {"center": [0.5, 0.5, 0.5, 0.5]},
                "beliefs": {"values": [0.8, 0.6, 0.7]},
                "recovery": {"tau_estimate": 10.0},
                "stability_score": 0.8,
                "observation_count": 250,
                "identity_confidence": 0.85,
            }
        }
        result = compute_trust_tier(metadata)
        assert result["tier"] == 3
        assert result["name"] == "verified"
        assert result["lineage_similarity"] > 0.8

    def test_drift_prevents_promotion(self):
        """High obs and confidence but diverged attractors = low tier."""
        from src.trajectory_identity import compute_trust_tier
        metadata = {
            "trajectory_genesis": {
                "attractor": {"center": [0.1, 0.1, 0.1, 0.1]},
                "beliefs": {"values": [0.1, 0.1, 0.1]},
                "recovery": {"tau_estimate": 5.0},
                "stability_score": 0.2,
                "observation_count": 10,
                "identity_confidence": 0.4,
            },
            "trajectory_current": {
                "attractor": {"center": [0.9, 0.9, 0.9, 0.9]},
                "beliefs": {"values": [0.9, 0.9, 0.9]},
                "recovery": {"tau_estimate": 100.0},
                "stability_score": 0.95,
                "observation_count": 300,
                "identity_confidence": 0.9,
            }
        }
        result = compute_trust_tier(metadata)
        assert result["tier"] <= 1
        assert result["lineage_similarity"] is not None
        assert result["lineage_similarity"] < 0.7

    def test_established_identity_does_not_fall_back_to_emerging_on_lineage_drift(self):
        """Earned trust should not churn through tier 1 and trigger reseed cycles."""
        from src.trajectory_identity import compute_trust_tier
        metadata = {
            "trajectory_genesis": {
                "attractor": {"center": [0.1, 0.1, 0.1, 0.1]},
                "beliefs": {"values": [0.1, 0.1, 0.1]},
                "recovery": {"tau_estimate": 5.0},
                "stability_score": 0.2,
                "observation_count": 10,
                "identity_confidence": 0.4,
            },
            "trajectory_current": {
                "attractor": {"center": [0.9, 0.9, 0.9, 0.9]},
                "beliefs": {"values": [0.9, 0.9, 0.9]},
                "recovery": {"tau_estimate": 100.0},
                "stability_score": 0.95,
                "observation_count": 300,
                "identity_confidence": 0.9,
            },
            "trust_tier": {"tier": 2, "name": "established"},
        }
        result = compute_trust_tier(metadata)
        assert result["tier"] == 2
        assert result["name"] == "established"
        assert result["lineage_similarity"] is not None
        assert result["lineage_similarity"] < 0.7
        assert "Retaining established" in result["reason"]

    def test_result_has_all_fields(self):
        """Every tier result should include required fields."""
        from src.trajectory_identity import compute_trust_tier
        for metadata in [
            {},
            {"trajectory_genesis": {"observation_count": 5, "identity_confidence": 0.1}},
        ]:
            result = compute_trust_tier(metadata)
            assert "tier" in result
            assert "name" in result
            assert "observation_count" in result
            assert "identity_confidence" in result
            assert "lineage_similarity" in result
            assert "reason" in result
            assert isinstance(result["tier"], int)
            assert 0 <= result["tier"] <= 3


class TestDTWSimilarity:
    """DTW-based trajectory shape comparison."""

    def test_identical_trajectories(self):
        """Identical trajectory shapes should have similarity ~1.0."""
        from src.trajectory_identity import _dtw_similarity
        import math
        s1 = [0.5 + 0.1 * math.sin(i / 5) for i in range(50)]
        s2 = list(s1)
        assert _dtw_similarity(s1, s2) > 0.99

    def test_different_trajectories(self):
        """Different trajectory shapes should have lower similarity."""
        from src.trajectory_identity import _dtw_similarity
        import math
        s1 = [0.5 + 0.1 * math.sin(i / 5) for i in range(50)]
        s2 = [0.5 + 0.1 * math.cos(i / 3) for i in range(50)]
        sim = _dtw_similarity(s1, s2)
        assert sim < 0.95

    def test_time_shifted_similarity(self):
        """Time-shifted versions should still be similar (DTW benefit)."""
        from src.trajectory_identity import _dtw_similarity
        import math
        s1 = [0.5 + 0.2 * math.sin(i / 5) for i in range(50)]
        s2 = [0.5 + 0.2 * math.sin((i + 5) / 5) for i in range(50)]
        sim = _dtw_similarity(s1, s2)
        assert sim > 0.7  # DTW handles phase shift

    def test_empty_series(self):
        """Empty time series should return 0.0 similarity."""
        from src.trajectory_identity import _dtw_similarity
        assert _dtw_similarity([], [1, 2, 3]) == 0.0
        assert _dtw_similarity([1, 2, 3], []) == 0.0

    def test_dtw_via_trajectory_shape_similarity(self):
        """DTW is available via trajectory_shape_similarity() (not in main similarity)."""
        from src.trajectory_identity import TrajectorySignature
        import math

        traj_a = [0.5 + 0.1 * math.sin(i / 5) for i in range(50)]
        traj_b = [0.5 + 0.1 * math.cos(i / 3) for i in range(50)]

        sig1 = TrajectorySignature(
            attractor={
                "center": [0.5, 0.5, 0.5, 0.0],
                "E_trajectory": traj_a,
                "I_trajectory": traj_a,
                "S_trajectory": traj_a,
                "V_trajectory": traj_a,
            },
            beliefs={"values": [0.8, 0.6]},
        )
        sig_same = TrajectorySignature(
            attractor={
                "center": [0.5, 0.5, 0.5, 0.0],
                "E_trajectory": traj_a,
                "I_trajectory": traj_a,
                "S_trajectory": traj_a,
                "V_trajectory": traj_a,
            },
            beliefs={"values": [0.8, 0.6]},
        )
        sig_diff = TrajectorySignature(
            attractor={
                "center": [0.5, 0.5, 0.5, 0.0],
                "E_trajectory": traj_b,
                "I_trajectory": traj_b,
                "S_trajectory": traj_b,
                "V_trajectory": traj_b,
            },
            beliefs={"values": [0.8, 0.6]},
        )

        # DTW discriminates via the supplemental method
        dtw_same = sig1.trajectory_shape_similarity(sig_same)
        dtw_diff = sig1.trajectory_shape_similarity(sig_diff)
        assert dtw_same is not None
        assert dtw_diff is not None
        assert dtw_same > dtw_diff

        # Main similarity does NOT use DTW (same center+beliefs = same score)
        sim_same = sig1.similarity(sig_same)
        sim_diff = sig1.similarity(sig_diff)
        assert sim_same == sim_diff  # Without DTW, these are equal

    def test_graceful_degradation_no_trajectory(self):
        """Without trajectory data, similarity should still work (existing behavior)."""
        from src.trajectory_identity import TrajectorySignature

        sig1 = TrajectorySignature(
            attractor={"center": [0.5, 0.5, 0.5, 0.0]},
            beliefs={"values": [0.8, 0.6]},
            stability_score=0.8,
        )
        sig2 = TrajectorySignature(
            attractor={"center": [0.5, 0.5, 0.5, 0.0]},
            beliefs={"values": [0.8, 0.6]},
            stability_score=0.8,
        )
        sim = sig1.similarity(sig2)
        assert sim > 0.9  # Should be very similar without DTW


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
