"""Pre-onboard read tools must not launder server-inferred identity.

A no-proof Hermes/MCP episode can share a transport session with a resident
binding. Read-only pre-onboard tools should report an unbound caller instead of
presenting that resident as the caller.
"""

from __future__ import annotations

import json

import pytest


@pytest.mark.asyncio
async def test_governance_metrics_server_inferred_binding_returns_unbound(monkeypatch):
    """Injected/sticky transport identity is not caller proof for read identity."""
    import src.mcp_handlers.context as context
    import src.mcp_handlers.core as core
    import src.services.runtime_queries as runtime_queries

    calls: list[str] = []

    monkeypatch.setattr(context, "get_context_agent_id", lambda: "chronicler-uuid")
    monkeypatch.setattr(context, "get_session_proof_origin", lambda: "server_inferred")
    monkeypatch.setattr(context, "get_session_resolution_source", lambda: "explicit_client_session_id")
    monkeypatch.setattr(core, "require_agent_id", lambda _arguments: ("chronicler-uuid", None))

    async def fake_metrics(agent_id, arguments, server=None):
        calls.append(agent_id)
        return {"status": "🟡 moderate", "agent_id": "Chronicler"}

    monkeypatch.setattr(runtime_queries, "get_governance_metrics_data", fake_metrics)

    result = await core.handle_get_governance_metrics({})
    payload = json.loads(result[0].text)

    assert payload["status"] == "⚪ unbound"
    assert calls == []


@pytest.mark.asyncio
async def test_governance_metrics_caller_asserted_binding_still_reads_agent(monkeypatch):
    """A caller-proven session binding keeps the convenient status read path."""
    import src.mcp_handlers.context as context
    import src.mcp_handlers.core as core
    import src.services.runtime_queries as runtime_queries

    calls: list[str] = []

    monkeypatch.setattr(context, "get_context_agent_id", lambda: "agent-uuid")
    monkeypatch.setattr(context, "get_session_proof_origin", lambda: "caller_asserted")
    monkeypatch.setattr(core, "require_agent_id", lambda _arguments: ("agent-uuid", None))

    async def fake_metrics(agent_id, arguments, server=None):
        calls.append(agent_id)
        return {"status": "🟢 low", "agent_id": "Caller"}

    monkeypatch.setattr(runtime_queries, "get_governance_metrics_data", fake_metrics)

    result = await core.handle_get_governance_metrics({})
    payload = json.loads(result[0].text)

    assert payload["status"] == "🟢 low"
    assert payload["agent_id"] == "Caller"
    assert calls == ["agent-uuid"]


def test_success_response_omits_signature_for_server_inferred_context(monkeypatch):
    """Generic read envelopes should not advertise a server-inferred sibling."""
    import src.mcp_handlers.context as context
    from src.mcp_handlers.response_base import success_response

    monkeypatch.setattr(context, "get_context_agent_id", lambda: "chronicler-uuid")
    monkeypatch.setattr(context, "get_session_proof_origin", lambda: "server_inferred")

    payload = json.loads(success_response({"results": []}, arguments={})[0].text)

    assert payload["agent_signature"] == {"uuid": None}


def test_knowledge_read_audit_omits_server_inferred_reader(monkeypatch):
    """Read-side KG telemetry should not attribute no-proof reads to a sibling."""
    import src.mcp_handlers.context as context
    from src.mcp_handlers.knowledge.handlers import _resolve_reader_agent_id

    monkeypatch.setattr(context, "get_context_agent_id", lambda: "chronicler-uuid")
    monkeypatch.setattr(context, "get_session_proof_origin", lambda: "server_inferred")

    assert _resolve_reader_agent_id({}) is None
