"""Tests for `src/identity/substrate.py` — R4 operational check.

Exercises the synthetic test cases called out in the ontology appendix
(`` — "Appendix: Pattern — Substrate-Earned
Identity"):

  - Lumen-like: embodied + sustained + declared → earned=true
  - Claude Code tab with hardcoded UUID but no substrate → earned=false
  - Shared-label resident (persistent but no anchor) → earned=false
  - Fresh hardware deployment (would-be substrate, N=0) → earned=false

Tests use the pure `evaluate_substrate_earned` entrypoint and a tmp
anchors directory fixture — no DB dependency.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.identity.substrate import (  # noqa: E402
    DEFAULT_RESTART_THRESHOLD,
    evaluate_substrate_earned,
    verify_substrate_earned,
)


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def anchors_dir(tmp_path):
    """A tmp anchors directory that tests populate as needed."""
    d = tmp_path / "anchors"
    d.mkdir()
    return d


def _write_anchor(anchors_dir: Path, role: str, agent_uuid: str) -> Path:
    path = anchors_dir / f"{role}.json"
    payload = {
        "client_session_id": f"agent-{agent_uuid[:8]}",
        "agent_uuid": agent_uuid,
    }
    path.write_text(json.dumps(payload))
    return path


def _metadata_with_observations(n: int) -> dict:
    """Fake identity metadata carrying a trajectory with N observations."""
    return {
        "trajectory_current": {
            "observation_count": n,
            "identity_confidence": 0.7,
        },
        "trajectory_genesis": {
            "observation_count": max(1, n // 2),
            "identity_confidence": 0.5,
        },
    }


# ── Canonical test cases (appendix) ──────────────────────────────────────


class TestAppendixCases:
    """The four synthetic cases from the ontology appendix."""

    def test_lumen_like_passes(self, anchors_dir):
        """Embodied + sustained + declared role → earned=true."""
        uuid = "b2d5c0e0-1111-2222-3333-444444444444"
        _write_anchor(anchors_dir, "lumen", uuid)

        result = evaluate_substrate_earned(
            agent_uuid=uuid,
            label="Lumen",
            tags=["embodied", "resident"],
            metadata=_metadata_with_observations(42),
            anchors_dir=anchors_dir,
        )

        assert result["earned"] is True
        c = result["conditions"]
        assert c["dedicated_substrate"] is True
        assert c["sustained_behavior"] is True
        assert c["declared_role"] is True
        # Evidence trail references the signals used
        assert result["evidence"]["substrate_class_tag"] == "embodied"
        assert result["evidence"]["observation_count"] == 42

    def test_claude_code_tab_with_hardcoded_uuid_fails(self, anchors_dir):
        """Hardcoded UUID but no dedicated substrate: no-substrate reason."""
        uuid = "11111111-2222-3333-4444-555555555555"
        # Tab has an anchor-like session file but no substrate-class tag
        # (anchor alone is a pinned UUID, not substrate).
        _write_anchor(anchors_dir, "claude_code_tab", uuid)

        result = evaluate_substrate_earned(
            agent_uuid=uuid,
            label="Claude_Opus_4_7_20260419",  # cosmetic harness label
            tags=[],
            metadata=_metadata_with_observations(3),
            anchors_dir=anchors_dir,
        )

        assert result["earned"] is False
        assert result["conditions"]["dedicated_substrate"] is False
        assert result["conditions"]["declared_role"] is False
        # Reason cites the no-substrate gap
        joined = " | ".join(result["reasons"])
        assert "dedicated_substrate=false" in joined
        assert (
            "no substrate-class tag" in joined
            or "pinned UUID" in joined
        )

    def test_shared_label_resident_without_anchor_fails(self, anchors_dir):
        """Vigil-like: `persistent` tag but no anchor pairing → earned=false."""
        uuid = "22222222-3333-4444-5555-666666666666"
        # No anchor file written for this agent's UUID.

        result = evaluate_substrate_earned(
            agent_uuid=uuid,
            label="Vigil",
            tags=["persistent", "resident"],
            metadata=_metadata_with_observations(20),
            anchors_dir=anchors_dir,
        )

        assert result["earned"] is False
        assert result["conditions"]["dedicated_substrate"] is False
        joined = " | ".join(result["reasons"])
        assert "no anchor file" in joined or "anchor pairing" in joined

    def test_shared_label_resident_with_anchor_passes(self, anchors_dir):
        """Vigil-like: `persistent` PAIRED with an anchor → earned=true."""
        uuid = "33333333-4444-5555-6666-777777777777"
        _write_anchor(anchors_dir, "vigil", uuid)

        result = evaluate_substrate_earned(
            agent_uuid=uuid,
            label="Vigil",
            tags=["persistent", "resident"],
            metadata=_metadata_with_observations(20),
            anchors_dir=anchors_dir,
        )

        assert result["earned"] is True
        assert result["conditions"]["dedicated_substrate"] is True
        assert result["evidence"]["anchor_file"] == "vigil.json"

    def test_fresh_hardware_deployment_fails(self, anchors_dir):
        """New embodied agent on day 1: substrate + role OK, tenure N=0 → earned=false."""
        uuid = "44444444-5555-6666-7777-888888888888"
        _write_anchor(anchors_dir, "lumen2", uuid)

        result = evaluate_substrate_earned(
            agent_uuid=uuid,
            label="Lumen2",
            tags=["embodied", "resident"],
            metadata={},  # no trajectory yet
            anchors_dir=anchors_dir,
        )

        assert result["earned"] is False
        assert result["conditions"]["dedicated_substrate"] is True
        assert result["conditions"]["declared_role"] is True
        assert result["conditions"]["sustained_behavior"] is False
        joined = " | ".join(result["reasons"])
        assert "sustained_behavior=false" in joined
        assert "tenure" in joined.lower() or "observation_count=0" in joined


# ── Edge cases ───────────────────────────────────────────────────────────


class TestEdgeCases:

    def test_ephemeral_tag_disqualifies_substrate(self, anchors_dir):
        """An `ephemeral`-tagged agent fails condition 1 even with an anchor."""
        uuid = "55555555-6666-7777-8888-999999999999"
        _write_anchor(anchors_dir, "ghost", uuid)

        result = evaluate_substrate_earned(
            agent_uuid=uuid,
            label="Ghost",
            tags=["ephemeral"],
            metadata=_metadata_with_observations(100),
            anchors_dir=anchors_dir,
        )

        assert result["earned"] is False
        assert result["conditions"]["dedicated_substrate"] is False
        joined = " | ".join(result["reasons"])
        assert "ephemeral" in joined

    def test_missing_label_fails_declared_role(self, anchors_dir):
        """Empty label → declared_role=false."""
        uuid = "66666666-7777-8888-9999-aaaaaaaaaaaa"
        _write_anchor(anchors_dir, "lumen3", uuid)

        result = evaluate_substrate_earned(
            agent_uuid=uuid,
            label=None,
            tags=["embodied"],
            metadata=_metadata_with_observations(10),
            anchors_dir=anchors_dir,
        )

        assert result["earned"] is False
        assert result["conditions"]["declared_role"] is False

    def test_other_label_fails_declared_role(self, anchors_dir):
        """label='other' is the placeholder default; fails declared_role."""
        uuid = "77777777-8888-9999-aaaa-bbbbbbbbbbbb"
        _write_anchor(anchors_dir, "other", uuid)

        result = evaluate_substrate_earned(
            agent_uuid=uuid,
            label="other",
            tags=["embodied"],
            metadata=_metadata_with_observations(10),
            anchors_dir=anchors_dir,
        )

        assert result["earned"] is False
        assert result["conditions"]["declared_role"] is False

    def test_cosmetic_model_label_fails(self, anchors_dir):
        """`Claude_Opus_*` is a harness cosmetic label — not a declared role."""
        uuid = "88888888-9999-aaaa-bbbb-cccccccccccc"

        result = evaluate_substrate_earned(
            agent_uuid=uuid,
            label="Claude_Opus_4_7_20260419",
            tags=["embodied"],
            metadata=_metadata_with_observations(10),
            anchors_dir=anchors_dir,
        )

        assert result["conditions"]["declared_role"] is False

    def test_declared_role_requires_class_tag(self, anchors_dir):
        """Non-cosmetic label without class tag → declared_role=false.

        Paper §4 class taxonomy is the commitment; label alone is
        insufficient.
        """
        uuid = "99999999-aaaa-bbbb-cccc-dddddddddddd"

        result = evaluate_substrate_earned(
            agent_uuid=uuid,
            label="MyCustomAgent",
            tags=["some_arbitrary_tag"],
            metadata=_metadata_with_observations(10),
            anchors_dir=anchors_dir,
        )

        assert result["conditions"]["declared_role"] is False

    def test_configurable_restart_threshold(self, anchors_dir):
        """`restart_threshold=1` should let low-N substrates pass."""
        uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        _write_anchor(anchors_dir, "lumen4", uuid)

        result = evaluate_substrate_earned(
            agent_uuid=uuid,
            label="Lumen4",
            tags=["embodied"],
            metadata=_metadata_with_observations(1),
            anchors_dir=anchors_dir,
            restart_threshold=1,
        )

        assert result["earned"] is True
        assert result["evidence"]["restart_threshold"] == 1

    def test_default_threshold_exposed(self):
        assert DEFAULT_RESTART_THRESHOLD == 5

    def test_anchor_accepts_agent_id_key(self, anchors_dir):
        """Anchors with `agent_id` instead of `agent_uuid` also match."""
        uuid = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
        path = anchors_dir / "alt.json"
        path.write_text(json.dumps({"agent_id": uuid}))

        result = evaluate_substrate_earned(
            agent_uuid=uuid,
            label="Alt",
            tags=["persistent"],
            metadata=_metadata_with_observations(10),
            anchors_dir=anchors_dir,
        )

        assert result["conditions"]["dedicated_substrate"] is True
        assert result["evidence"]["anchor_file"] == "alt.json"

    def test_anchors_dir_missing_is_conservative(self, tmp_path):
        """Non-existent anchors directory: no anchor match; persistent fails."""
        missing = tmp_path / "does_not_exist"
        uuid = "cccccccc-dddd-eeee-ffff-000000000000"

        result = evaluate_substrate_earned(
            agent_uuid=uuid,
            label="Vigil",
            tags=["persistent"],
            metadata=_metadata_with_observations(10),
            anchors_dir=missing,
        )

        assert result["conditions"]["dedicated_substrate"] is False
        assert result["evidence"]["anchor_file"] is None

    def test_result_shape_is_stable(self, anchors_dir):
        """Result dict always has the four top-level keys."""
        result = evaluate_substrate_earned(
            agent_uuid="deadbeef-0000-0000-0000-000000000000",
            label=None,
            tags=[],
            metadata={},
            anchors_dir=anchors_dir,
        )
        assert set(result.keys()) == {"earned", "conditions", "reasons", "evidence"}
        assert set(result["conditions"].keys()) == {
            "dedicated_substrate",
            "sustained_behavior",
            "declared_role",
        }
        assert isinstance(result["reasons"], list)
        assert isinstance(result["evidence"], dict)


# ── Async wrapper ────────────────────────────────────────────────────────


class TestVerifySubstrateEarnedAsync:

    @pytest.mark.asyncio
    async def test_agent_not_found_is_conservative(self, anchors_dir):
        """Missing agent row → earned=false with cannot-verify reason."""
        uuid = "dddddddd-eeee-ffff-0000-111111111111"

        mock_db = MagicMock()
        mock_db.get_agent = AsyncMock(return_value=None)
        mock_db.get_identity = AsyncMock(return_value=None)

        with patch("src.db.get_db", return_value=mock_db):
            result = await verify_substrate_earned(
                uuid, anchors_dir=anchors_dir
            )

        assert result["earned"] is False
        joined = " | ".join(result["reasons"])
        assert "not found" in joined
        assert "cannot verify" in joined

    @pytest.mark.asyncio
    async def test_db_exception_is_conservative(self, anchors_dir):
        """DB raises → earned=false; no confidence fabricated."""
        uuid = "eeeeeeee-ffff-0000-1111-222222222222"

        with patch("src.db.get_db", side_effect=RuntimeError("db down")):
            result = await verify_substrate_earned(
                uuid, anchors_dir=anchors_dir
            )

        assert result["earned"] is False
        joined = " | ".join(result["reasons"])
        assert "DB lookup failed" in joined

    @pytest.mark.asyncio
    async def test_happy_path_delegates_to_pure_evaluator(self, anchors_dir):
        """Async path composes DB fetch + pure evaluator correctly."""
        uuid = "ffffffff-0000-1111-2222-333333333333"
        _write_anchor(anchors_dir, "lumen5", uuid)

        identity_stub = MagicMock()
        identity_stub.metadata = _metadata_with_observations(30)

        mock_db = MagicMock()
        mock_db.get_agent = AsyncMock(
            return_value={"label": "Lumen5", "tags": ["embodied"]}
        )
        mock_db.get_identity = AsyncMock(return_value=identity_stub)

        with patch("src.db.get_db", return_value=mock_db):
            result = await verify_substrate_earned(
                uuid, anchors_dir=anchors_dir
            )

        assert result["earned"] is True
        assert result["conditions"]["dedicated_substrate"] is True
        assert result["conditions"]["sustained_behavior"] is True
        assert result["conditions"]["declared_role"] is True
