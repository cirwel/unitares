"""A metrics read for an id that names no agent must refuse, not seed.

`get_governance_metrics_data` reaches `get_or_create_monitor`, which builds a
fresh monitor on the default seed vector for any string it is handed. Every
field computed downstream — EISV, basin, verdict, guidance — is then a function
of that seed, returned under a `success` envelope. It is indistinguishable from
a reading.

That is not hypothetical. On 2026-08-10 the Discord governance HUD showed 50
agents at a constant E=0.70 I=0.80 S=0.20 V=0.00, because it fed the redacted
display handles from `list_agents` (`Claude_Code_<date>_<uuid8>`) back into this
tool. None resolved. Every one came back seeded.

Both transports are covered: PR #608 found the REST direct handler bypassing a
handler-only guard, so the refusal is defined once and invoked twice.
"""

from __future__ import annotations

import json

import pytest


UNKNOWN_ID = "Claude_Code_20260805_33fcecfd"  # a display handle, not a UUID
KNOWN_ID = "33fcecfd-291d-4407-b6c5-55b0dd040b72"


@pytest.fixture
def registry(monkeypatch):
    """Point the shared agent registry at exactly one known agent."""
    import src.mcp_handlers.core as core

    monkeypatch.setattr(core.mcp_server, "agent_metadata", {KNOWN_ID: object()})
    return core


@pytest.mark.asyncio
async def test_mcp_transport_refuses_unknown_agent(registry, monkeypatch):
    import src.services.runtime_queries as runtime_queries

    core = registry
    monkeypatch.setattr(core, "require_agent_id", lambda _a: (UNKNOWN_ID, None))

    called: list[str] = []

    async def fake_metrics(agent_id, arguments, server=None):
        called.append(agent_id)
        return {"status": "🟢 low"}

    monkeypatch.setattr(runtime_queries, "get_governance_metrics_data", fake_metrics)

    result = await core.handle_get_governance_metrics({"agent_id": UNKNOWN_ID})
    payload = json.loads(result[0].text)

    assert payload["success"] is False
    assert payload["error_type"] == "unknown_agent"
    # The seed never got built: the data layer was never reached.
    assert called == []


@pytest.mark.asyncio
async def test_rest_transport_refuses_unknown_agent(registry, monkeypatch):
    """The REST shortcut carries its own guard (PR #608 lesson)."""
    import src.services.http_tool_service as http_tool_service

    monkeypatch.setattr(
        http_tool_service, "require_agent_id", lambda _a: (UNKNOWN_ID, None)
    )

    called: list[str] = []

    async def fake_metrics(agent_id, arguments, server=None):
        called.append(agent_id)
        return {"status": "🟢 low"}

    monkeypatch.setattr(http_tool_service, "get_governance_metrics_data", fake_metrics)

    result = await http_tool_service._execute_http_get_governance_metrics(
        {"agent_id": UNKNOWN_ID}
    )
    payload = json.loads(result[0].text)

    assert payload["success"] is False
    assert payload["error_type"] == "unknown_agent"
    assert called == []


@pytest.mark.asyncio
async def test_known_agent_still_reads(registry, monkeypatch):
    """The guard must not refuse a real agent."""
    import src.services.runtime_queries as runtime_queries

    core = registry
    monkeypatch.setattr(core, "require_agent_id", lambda _a: (KNOWN_ID, None))

    async def fake_metrics(agent_id, arguments, server=None):
        return {"status": "🟢 low", "agent_id": agent_id}

    monkeypatch.setattr(runtime_queries, "get_governance_metrics_data", fake_metrics)

    result = await core.handle_get_governance_metrics({"agent_id": KNOWN_ID})
    payload = json.loads(result[0].text)

    assert payload["success"] is True
    assert payload["status"] == "🟢 low"


@pytest.mark.asyncio
async def test_zero_observation_agent_is_not_treated_as_unknown(registry, monkeypatch):
    """"Onboarded but never checked in" is a real state, distinct from "no such agent".

    The seed vector is an honest answer for an agent that exists and has no
    observations yet — that is what `is_uninitialized` in runtime_queries is
    for. The refusal is only about ids that resolve to nothing at all.
    """
    import src.services.runtime_queries as runtime_queries

    core = registry
    monkeypatch.setattr(core, "require_agent_id", lambda _a: (KNOWN_ID, None))

    async def fake_metrics(agent_id, arguments, server=None):
        return {"status": "⚪ uninitialized", "summary": "uninitialized | no observations yet"}

    monkeypatch.setattr(runtime_queries, "get_governance_metrics_data", fake_metrics)

    result = await core.handle_get_governance_metrics({"agent_id": KNOWN_ID})
    payload = json.loads(result[0].text)

    assert payload["success"] is True
    assert payload["status"] == "⚪ uninitialized"


@pytest.mark.asyncio
async def test_cold_registry_reloads_before_refusing(monkeypatch):
    """A cold-start read must not refuse a real agent just because the
    registry has not been hydrated yet."""
    import src.agent_state as agent_state
    import src.mcp_handlers.core as core

    registry_dict: dict = {}
    monkeypatch.setattr(core.mcp_server, "agent_metadata", registry_dict)

    loads: list[bool] = []

    async def fake_load():
        loads.append(True)
        registry_dict[KNOWN_ID] = object()

    monkeypatch.setattr(agent_state, "load_metadata_async", fake_load)

    assert await core.metrics_agent_is_known(KNOWN_ID) is True
    assert loads == [True]


@pytest.mark.asyncio
async def test_warm_registry_skips_reload(monkeypatch):
    """The hot path must not pay for a reload on every read."""
    import src.agent_state as agent_state
    import src.mcp_handlers.core as core

    monkeypatch.setattr(core.mcp_server, "agent_metadata", {KNOWN_ID: object()})

    loads: list[bool] = []

    async def fake_load():
        loads.append(True)

    monkeypatch.setattr(agent_state, "load_metadata_async", fake_load)

    assert await core.metrics_agent_is_known(KNOWN_ID) is True
    assert loads == []
