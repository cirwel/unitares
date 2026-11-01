"""Offline wire-contract tests, not proof of an installed hosted integration."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from unitares_sdk.client import GovernanceClient
from unitares_sdk.errors import GovernanceConnectionError, GovernanceTimeoutError

_spec = importlib.util.spec_from_file_location(
    "hosted_execution_example",
    Path(__file__).parents[1] / "examples" / "hosted_execution.py",
)
example = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(example)


def reply(**data):
    return SimpleNamespace(content=[SimpleNamespace(text=json.dumps(data))])


def client_with_responses(*responses):
    client = GovernanceClient(retry_delay=0)
    client._session = SimpleNamespace(call_tool=AsyncMock(side_effect=responses))
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    return client


async def reports(*values):
    for value in values:
        yield value


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", [False, None, "room-123", 1])
async def test_unknown_boundary_makes_zero_calls(monkeypatch, boundary):
    factory = Mock()
    monkeypatch.setattr(example, "GovernanceClient", factory)
    with pytest.raises(RuntimeError, match="boundary unknown"):
        await example.report_known_execution(
            reports("finished review"), display_name="ExampleAssistant",
            mcp_url="http://unused.invalid", host_start_verified=boundary,
        )
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_verified_execution_threads_binding_without_lineage_or_idle_reports(monkeypatch):
    client = client_with_responses(
        reply(success=True, uuid="execution-a", client_session_id="session-a"),
        reply(success=True, agent_uuid="execution-a"),
        reply(success=True, verdict="proceed"),
        reply(success=True, verdict="proceed"),
    )
    monkeypatch.setattr(example, "GovernanceClient", Mock(return_value=client))
    result = await example.report_known_execution(
        reports("reviewed the patch", "  ", "tests passed"),
        display_name="ExampleAssistant", mcp_url="http://unused.invalid",
        host_start_verified=True,
    )
    calls = client._session.call_tool.call_args_list
    assert result == "execution-a"
    assert [call.args[0] for call in calls] == [
        "onboard", "check_working_state", "sync_state", "sync_state",
    ]
    assert calls[0].args[1]["force_new"] is True
    for key in ("retry", "parent_agent_id", "spawn_reason", "client_session_id", "tags"):
        assert key not in calls[0].args[1]
    for call in calls[1:]:
        assert call.args[1]["client_session_id"] == "session-a"
        assert "continuity_token" not in call.args[1]
    for call in calls[2:]:
        assert call.args[1]["epistemic_class"] == "agent_report"
        assert "confidence" not in call.args[1]
    client.disconnect.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,error", [
    (TimeoutError(), GovernanceTimeoutError),
    (ConnectionError("response lost"), GovernanceConnectionError),
])
async def test_uncertain_mint_stops_without_retry_or_report(monkeypatch, failure, error):
    client = client_with_responses(failure)
    monkeypatch.setattr(example, "GovernanceClient", Mock(return_value=client))
    with pytest.raises(error):
        await example.report_known_execution(
            reports("must not write"), display_name="ExampleAssistant",
            mcp_url="http://unused.invalid", host_start_verified=True,
        )
    assert client._session.call_tool.await_count == 1
    assert client.agent_uuid is None
    client.disconnect.assert_awaited_once()
    # There is no example retry loop or fallback mint after the failed attempt.


@pytest.mark.asyncio
@pytest.mark.parametrize("bound_uuid", [None, "different-execution"])
async def test_unconfirmed_binding_never_writes_or_remints(monkeypatch, bound_uuid):
    client = client_with_responses(
        reply(success=True, uuid="execution-a", client_session_id="session-a"),
        reply(success=True, agent_uuid=bound_uuid),
    )
    monkeypatch.setattr(example, "GovernanceClient", Mock(return_value=client))
    with pytest.raises(RuntimeError, match="Binding not confirmed"):
        await example.report_known_execution(
            reports("must not write"), display_name="ExampleAssistant",
            mcp_url="http://unused.invalid", host_start_verified=True,
        )
    assert [call.args[0] for call in client._session.call_tool.call_args_list] == [
        "onboard", "check_working_state",
    ]


@pytest.mark.asyncio
async def test_onboard_default_retry_stays_compatible():
    client = client_with_responses(
        TimeoutError(),
        reply(success=True, uuid="execution-a", client_session_id="session-a"),
    )
    await client.onboard("ExampleAssistant", force_new=True)
    assert client._session.call_tool.await_count == 2
    assert "retry" not in client._session.call_tool.call_args.args[1]


@pytest.mark.asyncio
async def test_fresh_clients_do_not_reuse_predecessor_proof():
    first = client_with_responses(reply(
        success=True, uuid="execution-a", client_session_id="session-a",
        continuity_token="proof-a",
    ))
    second = client_with_responses(reply(
        success=True, uuid="execution-b", client_session_id="session-b",
    ))
    await first.onboard("ExampleAssistant", force_new=True, retry=False)
    await second.onboard("ExampleAssistant", force_new=True, retry=False)
    assert second.agent_uuid == "execution-b"
    assert second.client_session_id == "session-b"
    assert second.continuity_token is None
    args = second._session.call_tool.call_args.args[1]
    assert not {"parent_agent_id", "client_session_id", "continuity_token"} & args.keys()


@pytest.mark.asyncio
async def test_roster_label_collision_cannot_emit_resident_observation(monkeypatch):
    import unitares_sdk._substrate as substrate

    monkeypatch.setattr(substrate, "KNOWN_RESIDENT_NAMES", frozenset({"ExampleAssistant"}))
    emit = Mock()
    monkeypatch.setattr(substrate, "emit_substrate_observation", emit)
    client = client_with_responses(
        reply(success=True, uuid="execution-a", client_session_id="session-a"),
        reply(success=True, agent_uuid="execution-a"),
        reply(success=True, verdict="proceed", metrics={"E": 0.5}),
    )
    monkeypatch.setattr(example, "GovernanceClient", Mock(return_value=client))
    await example.report_known_execution(
        reports("completed review"), display_name="ExampleAssistant",
        mcp_url="http://unused.invalid", host_start_verified=True,
    )
    assert client.resident_name == "ExampleAssistant"  # SDK captures display label
    emit.assert_not_called()  # ordinary reports never become resident observations


@pytest.mark.asyncio
async def test_identity_refusal_stops_reports_without_token_recovery(monkeypatch):
    from unitares_sdk.errors import IdentityRefusedError

    client = client_with_responses(
        reply(success=True, uuid="execution-a", client_session_id="session-a",
              continuity_token="proof-a"),
        reply(success=True, agent_uuid="execution-a"),
        reply(success=True, rollout_flag="STRICT_IDENTITY_REQUIRED",
              status="identity_required", tool="sync_state"),
    )
    monkeypatch.setattr(example, "GovernanceClient", Mock(return_value=client))
    with pytest.raises(IdentityRefusedError):
        await example.report_known_execution(
            reports("first report", "must not send"), display_name="ExampleAssistant",
            mcp_url="http://unused.invalid", host_start_verified=True,
        )
    assert client._session.call_tool.await_count == 3
    assert "continuity_token" not in client._session.call_tool.call_args.args[1]
