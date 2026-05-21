"""Tests for the onboard-side R1 score_trajectory_continuity background wrapper.

The primitive itself is covered by test_trajectory_continuity.py. This file
covers the fire-and-forget helper in mcp_handlers.identity.handlers that the
onboard flow schedules alongside the SPAWNED edge + seed_genesis tasks.

"Caller policy" + §v3.3-D:
- Onboard policy is `marks` — `inconclusive` verdict stamps
  `provisional_lineage=true`; `plausible` and `unsupported` are no-ops at
  this gate (orphan-archival handles `unsupported`).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.identity.trajectory_continuity import TrajectoryContinuityScore


def _make_score(verdict: str, score_id: str = "score-uuid-123") -> TrajectoryContinuityScore:
    """Synthesize a score with a chosen verdict — other fields stable."""
    return TrajectoryContinuityScore(
        score_id=score_id,
        plausibility=0.65,
        verdict=verdict,
        observations={"parent": {"E": 30, "I": 30, "S": 30, "V": 30},
                      "successor": {"E": 10, "I": 10, "S": 10, "V": 10}},
        components={"E": 0.65, "I": 0.65, "S": 0.65, "V": 0.65},
        reasons=[],
        parent_mature=True,
        calibration_status="seeded",
        n_dims_used=4,
    )


class TestScoreLineageContinuityBg:
    @pytest.mark.asyncio
    async def test_inconclusive_marks_provisional(self):
        """`marks` policy: inconclusive verdict triggers mark_lineage_provisional
        with the score_id as anchor."""
        mock_primitive = AsyncMock(
            return_value=_make_score("inconclusive", score_id="abcd-1234"),
        )
        mock_backend = MagicMock()
        mock_backend.mark_lineage_provisional = AsyncMock(return_value=True)
        with patch(
            "src.identity.trajectory_continuity.score_trajectory_continuity",
            mock_primitive,
        ), patch(
            "src.mcp_handlers.identity.handlers.get_db",
            return_value=mock_backend,
        ):
            from src.mcp_handlers.identity.handlers import _score_lineage_continuity_bg
            await _score_lineage_continuity_bg("child-uuid", "parent-uuid")

        mock_primitive.assert_awaited_once_with("parent-uuid", "child-uuid")
        mock_backend.mark_lineage_provisional.assert_awaited_once_with(
            "child-uuid", "abcd-1234",
        )

    @pytest.mark.asyncio
    async def test_plausible_does_not_mark(self):
        """Plausible verdict: lineage stays unflagged; no mark call."""
        mock_primitive = AsyncMock(return_value=_make_score("plausible"))
        mock_backend = MagicMock()
        mock_backend.mark_lineage_provisional = AsyncMock(return_value=True)
        with patch(
            "src.identity.trajectory_continuity.score_trajectory_continuity",
            mock_primitive,
        ), patch(
            "src.mcp_handlers.identity.handlers.get_db",
            return_value=mock_backend,
        ):
            from src.mcp_handlers.identity.handlers import _score_lineage_continuity_bg
            await _score_lineage_continuity_bg("child-uuid", "parent-uuid")

        mock_primitive.assert_awaited_once()
        mock_backend.mark_lineage_provisional.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unsupported_does_not_mark_at_onboard(self):
        """Unsupported verdict: NOT marked at onboard. Orphan-archival is the
        enforcement path for unsupported (spec §"Caller policy" line 256)."""
        mock_primitive = AsyncMock(return_value=_make_score("unsupported"))
        mock_backend = MagicMock()
        mock_backend.mark_lineage_provisional = AsyncMock(return_value=True)
        with patch(
            "src.identity.trajectory_continuity.score_trajectory_continuity",
            mock_primitive,
        ), patch(
            "src.mcp_handlers.identity.handlers.get_db",
            return_value=mock_backend,
        ):
            from src.mcp_handlers.identity.handlers import _score_lineage_continuity_bg
            await _score_lineage_continuity_bg("child-uuid", "parent-uuid")

        mock_primitive.assert_awaited_once()
        mock_backend.mark_lineage_provisional.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_score_exception_is_swallowed(self):
        """Score primitive raising (e.g. audit write fails — fail-loud
        contract) must be non-fatal — onboard path already committed the
        identity, scoring is best-effort."""
        mock_primitive = AsyncMock(
            side_effect=RuntimeError("audit write failed"),
        )
        with patch(
            "src.identity.trajectory_continuity.score_trajectory_continuity",
            mock_primitive,
        ):
            from src.mcp_handlers.identity.handlers import _score_lineage_continuity_bg
            # Must not raise
            await _score_lineage_continuity_bg("child-uuid", "parent-uuid")

        mock_primitive.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_mark_zero_rows_is_non_fatal(self):
        """mark_lineage_provisional returning False (0 rows matched — e.g.
        identity row missing) must not propagate. Logged at debug, no raise."""
        mock_primitive = AsyncMock(return_value=_make_score("inconclusive"))
        mock_backend = MagicMock()
        mock_backend.mark_lineage_provisional = AsyncMock(return_value=False)
        with patch(
            "src.identity.trajectory_continuity.score_trajectory_continuity",
            mock_primitive,
        ), patch(
            "src.mcp_handlers.identity.handlers.get_db",
            return_value=mock_backend,
        ):
            from src.mcp_handlers.identity.handlers import _score_lineage_continuity_bg
            # Must not raise
            await _score_lineage_continuity_bg("child-uuid", "parent-uuid")

        mock_backend.mark_lineage_provisional.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_mark_exception_is_swallowed(self):
        """mark_lineage_provisional raising (DB pool exhausted etc.) must
        not propagate. The score is recorded; mark is best-effort."""
        mock_primitive = AsyncMock(return_value=_make_score("inconclusive"))
        mock_backend = MagicMock()
        mock_backend.mark_lineage_provisional = AsyncMock(
            side_effect=RuntimeError("pool exhausted"),
        )
        with patch(
            "src.identity.trajectory_continuity.score_trajectory_continuity",
            mock_primitive,
        ), patch(
            "src.mcp_handlers.identity.handlers.get_db",
            return_value=mock_backend,
        ):
            from src.mcp_handlers.identity.handlers import _score_lineage_continuity_bg
            # Must not raise
            await _score_lineage_continuity_bg("child-uuid", "parent-uuid")

        mock_backend.mark_lineage_provisional.assert_awaited_once()
