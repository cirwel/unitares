"""The served admin telemetry contract must match its caller-bound dispatch.

Exercise the public description and real alias/identity/validation/router path.
Only the telemetry I/O and process performance snapshot are substituted; the
scope, defaults, and measurement-context response come from production code.
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.helpers import parse_result

pytestmark = pytest.mark.usefixtures("first_party_tool_surface")

CALLER = "12345678-1234-4234-8234-123456789abc"


@pytest.fixture
def bound_caller():
    from src.mcp_handlers.context import (
        reset_session_context,
        set_session_context,
        update_context_agent_id,
    )

    token = set_session_context()
    update_context_agent_id(CALLER)
    try:
        yield CALLER
    finally:
        reset_session_context(token)


@pytest.fixture
def telemetry(monkeypatch):
    collector = SimpleNamespace(
        get_skip_rate_metrics=Mock(return_value={"total_skips": 1, "total_updates": 9}),
        get_confidence_distribution=Mock(return_value={"count": 9, "mean": 0.7}),
        detect_suspicious_patterns=Mock(return_value=[]),
        get_calibration_metrics=Mock(return_value={"bins": {"high": {"count": 20}}}),
        audit_logger=SimpleNamespace(log_file=SimpleNamespace(exists=lambda: True)),
    )
    monkeypatch.setattr("src.telemetry.TelemetryCollector", lambda: collector)
    monkeypatch.setattr("src.perf_monitor.snapshot", lambda: {})
    return collector


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["admin", "get_telemetry_metrics"])
@pytest.mark.parametrize("window_hours", [None, 48])
@pytest.mark.parametrize("include_calibration", [None, False, True])
async def test_described_admin_telemetry_matches_bound_call(
    tool_name, window_hours, include_calibration, bound_caller, telemetry,
):
    from src.mcp_handlers.introspection.tool_introspection import handle_describe_tool
    from src.mcp_handlers.middleware.params_step import (
        inject_identity,
        resolve_alias,
        validate_params,
    )
    from src.services.tool_dispatch_service import run_tool_dispatch_pipeline

    described = parse_result(await handle_describe_tool({"tool_name": "admin", "lite": True}))
    arguments = {"action": "telemetry"} if tool_name == "admin" else {}
    if window_hours is not None:
        arguments["window_hours"] = window_hours
    if include_calibration is not None:
        arguments["include_calibration"] = include_calibration
    # The caller supplies no agent_id: real injection must scope this read.
    result = await run_tool_dispatch_pipeline(
        name=tool_name,
        arguments=arguments,
        pre_steps=[resolve_alias, inject_identity, validate_params],
        post_steps=[],
    )
    data = parse_result(result)
    assert data["success"] is True
    expected_hours = 24 if window_hours is None else window_hours
    assert data["agent_id"] == bound_caller
    assert data["window_hours"] == expected_hours
    components = data["measurement_context"]["components"]
    for key, method in (
        ("skip_rate_metrics", telemetry.get_skip_rate_metrics),
        ("confidence_distribution", telemetry.get_confidence_distribution),
        ("suspicious_patterns", telemetry.detect_suspicious_patterns),
    ):
        method.assert_called_once_with(bound_caller, expected_hours)
        assert data[key] == method.return_value
        assert components[key]["subject"] == {"kind": "agent", "agent_id": bound_caller}
        assert components[key]["window"]["kind"] == "lookback"
        assert components[key]["window"]["requested_hours"] == expected_hours

    calibration = components["calibration"]
    assert calibration["subject"] == {"kind": "fleet"}
    assert calibration["window"] == {
        "kind": "cumulative", "requested_lookback_applied": False,
    }
    assert calibration["included"] is (include_calibration is True)
    if include_calibration:
        # No caller or lookback is passed to this cumulative fleet collector.
        telemetry.get_calibration_metrics.assert_called_once_with()
        assert data["calibration"] == telemetry.get_calibration_metrics.return_value
        assert calibration["coverage"]["status"] == "observed"
    else:
        telemetry.get_calibration_metrics.assert_not_called()
        assert "excluded" in data["calibration"]["note"]
        assert "bins" not in data["calibration"]
        assert calibration["coverage"]["status"] == "not_requested"

    # The description clients actually receive must teach the scopes above.
    description = described["description"]
    assert "bound caller's skip rate and confidence" in description
    assert "calibration is excluded unless include_calibration=true" in description
    assert "fleet-wide cumulative state, not a window_hours slice" in description
