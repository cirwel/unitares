"""The subscription-CLI host adapters are an operator extension.

A default install runs no agent orchestrator, so the Codex, Claude and
Antigravity adapters can never serve there. These tests pin that the catalog
keeps listing them (reachability is a routing contract and does not flap with
configuration) while every surface an agent reads on such an install says the
extension is off and points at the lane that works, instead of listing flags as
if one were missing.
"""

import json
from unittest.mock import AsyncMock

import pytest

from src.mcp_handlers.support import consultation as co
from src.mcp_handlers.support import delegated_inference as di
from src.mcp_handlers.support import inference_registry as registry
from src.mcp_handlers.support import model_inference as mi
from src.mcp_handlers.support.host_adapter import (
    HOST_ADAPTER_EXTENSION,
    host_adapter_ids,
    host_adapter_lane_configured,
)

_ADAPTERS = {"codex:host-adapter", "claude:host-adapter", "antigravity:host-adapter"}


def _payload(result):
    return json.loads(result[0].text)


@pytest.fixture
def default_install(monkeypatch):
    """No orchestrator extension, and no local socket probe."""
    monkeypatch.delenv("UNITARES_HOST_ADAPTER_ENABLED", raising=False)
    monkeypatch.delenv("AGENT_ORCHESTRATOR_BEARER_TOKEN", raising=False)
    monkeypatch.setattr(registry, "_ollama_available", lambda: False)


def _hosts_by_id():
    return {host["host_id"]: host for host in registry.list_inference_hosts()}


def test_lane_needs_both_the_flag_and_the_bearer(monkeypatch):
    monkeypatch.delenv("UNITARES_HOST_ADAPTER_ENABLED", raising=False)
    monkeypatch.delenv("AGENT_ORCHESTRATOR_BEARER_TOKEN", raising=False)
    assert host_adapter_lane_configured() is False

    monkeypatch.setenv("UNITARES_HOST_ADAPTER_ENABLED", "1")
    assert host_adapter_lane_configured() is False

    monkeypatch.setenv("AGENT_ORCHESTRATOR_BEARER_TOKEN", "token")
    assert host_adapter_lane_configured() is True


def test_adapter_records_name_their_extension_and_local_hosts_do_not(default_install):
    hosts = _hosts_by_id()
    assert set(host_adapter_ids()) == _ADAPTERS
    for host_id in _ADAPTERS:
        assert hosts[host_id]["extension"] == HOST_ADAPTER_EXTENSION
        assert hosts[host_id]["configured"] is False
        # Still listed, still reachable by contract: routing does not flap.
        assert hosts[host_id]["accepts_host_id_from"] == ["delegate_inference"]
    assert hosts["ollama:local"]["extension"] is None
    assert hosts["hf:router"]["extension"] is None


def test_the_flag_alone_does_not_read_as_configured(default_install, monkeypatch):
    monkeypatch.setenv("UNITARES_HOST_ADAPTER_ENABLED", "1")
    hosts = _hosts_by_id()
    assert all(hosts[host_id]["configured"] is False for host_id in _ADAPTERS)


@pytest.mark.asyncio
async def test_listing_says_the_extension_is_off(default_install):
    parsed = _payload(await mi.handle_list_inference_hosts({}))
    extension = parsed["extensions"][HOST_ADAPTER_EXTENSION]
    assert extension["configured"] is False
    assert set(extension["host_ids"]) == _ADAPTERS
    assert "delegate_inference" in extension["serves"]
    assert "default install" in extension["note"]


@pytest.mark.asyncio
async def test_listing_says_the_extension_is_on_when_configured(default_install, monkeypatch):
    monkeypatch.setenv("UNITARES_HOST_ADAPTER_ENABLED", "1")
    monkeypatch.setenv("AGENT_ORCHESTRATOR_BEARER_TOKEN", "token")
    parsed = _payload(await mi.handle_list_inference_hosts({}))
    assert parsed["extensions"][HOST_ADAPTER_EXTENSION]["configured"] is True


@pytest.mark.asyncio
async def test_delegate_inference_names_the_missing_extension(default_install):
    parsed = _payload(await di.handle_delegate_inference({"prompt": "hello"}))
    assert parsed["success"] is False
    assert parsed["error_code"] == "INFERENCE_HOST_UNAVAILABLE"
    assert parsed["execution_started"] is False
    assert "agent orchestrator extension" in parsed["error"]
    action = parsed["recovery"]["action"]
    assert "consult(effort='standard')" in action
    assert "call_model" in action


@pytest.mark.asyncio
async def test_a_configured_lane_keeps_the_setup_recovery(default_install, monkeypatch):
    """With the extension set up, a missing CLI still names its variable."""
    monkeypatch.setenv("UNITARES_HOST_ADAPTER_ENABLED", "1")
    monkeypatch.setenv("AGENT_ORCHESTRATOR_BEARER_TOKEN", "token")
    monkeypatch.setenv("UNITARES_CLAUDE_CLI", "/nonexistent/claude")
    monkeypatch.setattr(
        "src.mcp_handlers.support.host_adapter.resolve_host_cli",
        lambda _host_id: None,
    )
    parsed = _payload(await di.handle_delegate_inference({"prompt": "hello"}))
    assert parsed["error_code"] == "INFERENCE_HOST_UNAVAILABLE"
    assert "agent orchestrator extension" not in parsed["error"]
    assert "UNITARES_CLAUDE_CLI" in parsed["recovery"]["action"]


@pytest.fixture
def bound_consult(monkeypatch):
    monkeypatch.setattr(co, "get_context_resolved_agent_id", lambda: "caller")


@pytest.mark.asyncio
async def test_thorough_consult_on_a_default_install_points_at_standard(
    default_install, bound_consult, monkeypatch
):
    standard = AsyncMock()
    monkeypatch.setattr(co, "run_model_inference", standard)

    parsed = _payload(await co.handle_consult({
        "brief": "Deep analysis",
        "effort": "thorough",
        "privacy": "cloud_allowed",
    }))

    assert parsed["success"] is False
    assert parsed["error_code"] == "INFERENCE_HOST_UNAVAILABLE"
    assert parsed["failure"]["upstream"]["reason"] == "extension_not_configured"
    assert "effort='standard'" in parsed["recovery"]["action"]
    assert "Restore" not in parsed["recovery"]["action"]
    standard.assert_not_awaited()


@pytest.mark.asyncio
async def test_thorough_local_refusal_does_not_send_a_default_install_to_the_cloud(
    default_install, bound_consult
):
    parsed = _payload(await co.handle_consult({
        "brief": "Deep analysis",
        "effort": "thorough",
        "privacy": "local",
    }))

    assert parsed["error_code"] == "CONSULT_POLICY_UNSATISFIED"
    action = parsed["recovery"]["action"]
    assert "effort='standard'" in action
    assert "cloud_allowed" not in action
