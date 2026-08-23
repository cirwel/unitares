"""Both reviewer-reassignment producers must land on the (F) event stream.

The reassignment-rate half of §11 criterion 10 is computed from
``dialectic_reviewer_reassigned`` in ``audit.events``. Until 2026-08-22 only
the request-driven path emitted it: ``auto_resolve_stuck_sessions`` wrote
reviewer changes directly and called neither ``_apply_reviewer_reassignment``
nor ``check_reviewer_stuck`` nor ``check_timeout``, so every auto-path
reassignment was absent from the stream — while a comment in ``handlers.py``
described that emission as "the single chokepoint for both the explicit
`dialectic(reassign)` tool and the stuck-reviewer auto path."

These tests exist so that claim cannot silently become false again.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.mcp_handlers.dialectic import events


class TestEmitReviewerReassigned:
    @pytest.mark.asyncio
    async def test_payload_shape(self):
        """session_id must be top-level AND nested; source must be carried."""
        captured = {}

        async def fake_append(payload):
            captured.update(payload)

        with patch("src.audit_db.append_audit_event_async", side_effect=fake_append):
            await events.emit_reviewer_reassigned(
                session_id="sess-1",
                old_reviewer_id="old-agent",
                new_reviewer_id="new-agent",
                reason="reviewer_unresponsive",
                source="sweeper",
            )

        assert captured["event_type"] == "dialectic_reviewer_reassigned"
        assert captured["agent_id"] == "new-agent"
        # Top-level session_id populates the indexed audit.events column;
        # nested-only would land that column NULL.
        assert captured["session_id"] == "sess-1"
        assert captured["details"]["session_id"] == "sess-1"
        assert captured["details"]["old_reviewer_id"] == "old-agent"
        assert captured["details"]["new_reviewer_id"] == "new-agent"
        assert captured["details"]["source"] == "sweeper"

    @pytest.mark.asyncio
    async def test_is_fail_soft(self):
        """The reassignment has already committed; a failed emit must not raise."""
        with patch(
            "src.audit_db.append_audit_event_async",
            side_effect=RuntimeError("audit down"),
        ):
            await events.emit_reviewer_reassigned(
                session_id="sess-2",
                old_reviewer_id=None,
                new_reviewer_id="new-agent",
                reason="r",
                source="request",
            )  # must not raise

    @pytest.mark.asyncio
    async def test_open_reviewer_slot_is_representable(self):
        """old_reviewer_id is Optional — an open slot is not an error."""
        captured = {}

        async def fake_append(payload):
            captured.update(payload)

        with patch("src.audit_db.append_audit_event_async", side_effect=fake_append):
            await events.emit_reviewer_reassigned(
                session_id="sess-3",
                old_reviewer_id=None,
                new_reviewer_id="new-agent",
                reason="r",
                source="request",
            )

        assert captured["details"]["old_reviewer_id"] is None


class TestBothProducersEmit:
    """Structural: both call sites must route through the shared helper."""

    def test_sweeper_emits(self):
        from pathlib import Path

        src = Path(events.__file__).parent / "auto_resolve.py"
        text = src.read_text(encoding="utf-8")
        assert "emit_reviewer_reassigned(" in text, (
            "auto_resolve_stuck_sessions must emit the reassignment event — "
            "the (F) baseline is computed from this stream"
        )
        assert 'source="sweeper"' in text

    def test_request_path_emits(self):
        from pathlib import Path

        src = Path(events.__file__).parent / "handlers.py"
        text = src.read_text(encoding="utf-8")
        assert "emit_reviewer_reassigned(" in text
        assert 'source="request"' in text

    def test_event_shape_is_not_forked(self):
        """Neither producer may hand-build the payload — one shape, two callers."""
        from pathlib import Path

        pkg = Path(events.__file__).parent
        for name in ("auto_resolve.py", "handlers.py"):
            text = (pkg / name).read_text(encoding="utf-8")
            assert '"event_type": "dialectic_reviewer_reassigned"' not in text, (
                f"{name} builds the reassignment payload inline; it must call "
                "events.emit_reviewer_reassigned so the shape cannot drift"
            )
