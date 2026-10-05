"""export reads only the caller's own history.

``handle_get_system_history`` and ``handle_export_to_file`` take ``agent_id``
as given: neither checks it against the caller. What keeps a bound caller from
exporting another agent's full trajectory is the dispatch step before them,
``inject_identity``, which refuses an ``agent_id`` that is not the session's own
(``identity_mismatch``) unless the tool is in ``_OPERATOR_TARGET_CALLS``. An
unbound caller is refused earlier, by strict identity (``identity_required``).

So the guard lives in one place, and adding ``export`` to the operator-target
set, or a route that skips ``inject_identity``, would open the read with no
handler-level check behind it. These tests drive the canonical ``export``
tool through ``dispatch_tool`` and its real middleware, with the export
handlers' server and storage replaced. Cross-agent observation stays available
by design through ``observe(action='agent', target_agent_id=...)``.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.mcp_handlers.middleware.params_step import _OPERATOR_TARGET_CALLS
from tests.test_dispatch_tool_integration import (  # noqa: F401  (fixtures)
    clean_registry,
    integration_mocks,
    mock_db,
    mock_derive_session_key,
    mock_identity,
    mock_onboard_pin,
    mock_pattern_tracker,
    mock_rate_limiter,
    mock_tool_alias,
    mock_validators,
)

#: The session uuid the integration fixtures bind every call to.
CALLER = "test-uuid-0000-1111-2222"
OTHER = "2222abcd-2222-4222-8222-22222222abcd"

ACTIONS = ["history", "file"]


@pytest.fixture
def export_server(tmp_path):
    """The export handlers' server, recording which agent they were asked for.

    project_root is a temporary directory, so an export that should have been
    refused but ran writes there and not into the working tree.
    """
    server = MagicMock()
    server.project_root = str(tmp_path)
    monitor = MagicMock()
    monitor.state.E_history = []
    monitor.state.timestamp_history = []
    server.get_or_create_monitor.return_value = monitor
    server.agent_metadata = {}
    with patch("src.mcp_handlers.introspection.export.mcp_server", server), \
         patch("src.agent_monitor_state.ensure_hydrated", AsyncMock()):
        yield server


async def _export(arguments: dict) -> dict:
    from src.mcp_handlers import dispatch_tool

    result = await dispatch_tool("export", arguments)
    assert result, "dispatch_tool returned nothing"
    return json.loads(result[0].text)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ACTIONS)
async def test_another_agents_id_is_refused_before_the_handler(
    integration_mocks, export_server, action,
):
    body = await _export({"action": action, "agent_id": OTHER})

    assert body.get("success") is False, body
    assert body.get("error_type") == "identity_mismatch", body
    assert body.get("requested_agent_id") == OTHER
    export_server.get_or_create_monitor.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("own", [{"agent_id": CALLER}, {}], ids=["own-id", "no-id"])
async def test_own_history_reaches_the_handler_as_the_caller(
    integration_mocks, export_server, own,
):
    body = await _export({"action": "history", **own})

    assert body.get("success") is True, body
    assert body.get("agent_id") == CALLER
    export_server.get_or_create_monitor.assert_called_once_with(CALLER)


@pytest.mark.parametrize("action", ACTIONS)
def test_export_is_not_an_operator_target_call(action):
    assert not _OPERATOR_TARGET_CALLS.matches("export", {"action": action})
