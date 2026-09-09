"""#2134: the typed identity refusal predicate is single-sourced.

``strict_identity_refusal_payload`` is deliberately "a structured success-shape,
not an error", so a refusal carries ``success: true`` with no ``error`` key.
Every consumer that branches on success/error misses it. Two now ask the same
question — the usage recorder (so a refusal is not counted as a successful
call) and the experience envelope (so a refusal is not rebuilt into an ordinary
check-in) — so the predicate lives beside the builder it tests for.
"""

from __future__ import annotations

import pytest

from src.mcp_handlers.identity_bootstrap import (
    IDENTITY_REFUSAL_MARKER,
    identity_refusal_status,
    strict_identity_refusal_payload,
)


class TestPredicate:
    def test_recognizes_a_real_refusal(self):
        payload = strict_identity_refusal_payload("process_agent_update")
        assert identity_refusal_status(payload) == "identity_required"

    def test_carries_the_emission_points_own_status(self):
        payload = strict_identity_refusal_payload(
            "onboard", status="lineage_declaration_required"
        )
        assert identity_refusal_status(payload) == "lineage_declaration_required"

    @pytest.mark.parametrize("payload", [
        {"success": True, "status": "healthy"},
        {"success": False, "error": "nope"},
        {"status": "identity_required"},          # status without the marker
        {"rollout_flag": "SOMETHING_ELSE"},
        None,
        "not a dict",
        [],
    ])
    def test_rejects_everything_else(self, payload):
        assert identity_refusal_status(payload) is None

    def test_the_marker_is_written_by_the_builder_and_nothing_else(self):
        """The predicate is only precise while `rollout_flag` has one writer.
        If a second place starts writing it, this predicate starts producing
        false positives and both consumers silently change behavior.
        """
        import pathlib
        import re

        src = pathlib.Path("src")
        writers = set()
        pattern = re.compile(r'"rollout_flag"\s*:')
        for path in src.rglob("*.py"):
            for i, line in enumerate(path.read_text().splitlines(), 1):
                if pattern.search(line):
                    writers.add(f"{path}:{i}")
        assert len(writers) == 1, f"expected one writer, found {sorted(writers)}"
        assert "identity_bootstrap.py" in next(iter(writers))


class TestSingleSource:
    def test_the_usage_recorder_uses_the_same_predicate(self):
        """Re-exported under its original private names, so the recorder's
        own callers and tests are unaffected by the move."""
        from src.services import tool_usage_recorder as rec

        assert rec._identity_refusal_status is identity_refusal_status
        assert rec._IDENTITY_REFUSAL_MARKER == IDENTITY_REFUSAL_MARKER

    def test_the_recorder_still_classifies_a_refusal_as_not_a_success(self):
        import json

        from src.mcp_handlers.response_base import success_response
        from src.services.tool_usage_recorder import classify_tool_result

        raw = success_response(strict_identity_refusal_payload("process_agent_update"))
        assert json.loads(raw[0].text)["success"] is True  # premise guard
        success, error_type = classify_tool_result(raw)
        assert success is False
        assert error_type == "identity_required"
