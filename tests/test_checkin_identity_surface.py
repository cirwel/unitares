"""Check-in/metrics tools hide session-auto-injected identity params.

A check-in should read as one ambient binding, not four hand-threaded
identifiers. The four AgentIdentityMixin params (+ agent_name) are populated
by session injection (TOOLS_NEEDING_SESSION_INJECTION), so they are stripped
from the *advertised* schema while remaining accepted by the handler as a
same-process escape hatch. See src/tool_schemas.py:_hide_auto_injected_identity.
"""

import pytest

from src.tool_schemas import get_tool_definitions
from src.mcp_handlers.tool_stability import resolve_tool_alias
from src.mcp_handlers.schemas.core import ProcessAgentUpdateParams

HIDDEN = ("agent_id", "agent_name", "client_session_id", "continuity_token")
CANONICAL = ("process_agent_update", "get_governance_metrics")
ALIASES = ("sync_state", "check_working_state")


@pytest.fixture(scope="module")
def tool_defs():
    return {t.name: t for t in get_tool_definitions()}


@pytest.mark.parametrize("tool_name", CANONICAL)
def test_canonical_surface_hides_identity_params(tool_defs, tool_name):
    props = tool_defs[tool_name].inputSchema.get("properties", {})
    leaked = [p for p in HIDDEN if p in props]
    assert not leaked, f"{tool_name} still advertises {leaked}"


@pytest.mark.parametrize("alias", ALIASES)
def test_alias_inherits_stripped_surface(tool_defs, alias):
    actual, info = resolve_tool_alias(alias)
    assert info is not None, f"{alias} should resolve to a canonical tool"
    props = tool_defs[actual].inputSchema.get("properties", {})
    leaked = [p for p in HIDDEN if p in props]
    assert not leaked, f"{alias} -> {actual} still advertises {leaked}"


def test_core_params_survive(tool_defs):
    props = tool_defs["process_agent_update"].inputSchema.get("properties", {})
    for keep in ("response_text", "complexity", "confidence"):
        assert keep in props, f"process_agent_update lost {keep}"


def test_onboard_keeps_identity_params(tool_defs):
    # onboard is a real entry point for identity — it must NOT be stripped.
    props = tool_defs["onboard"].inputSchema.get("properties", {})
    assert "agent_id" in props
    assert "continuity_token" in props


def test_escape_hatch_handler_still_accepts_hidden_params():
    # Hidden from the surface, but the model still parses them when passed.
    m = ProcessAgentUpdateParams(
        response_text="x", agent_id="manual-123", client_session_id="s1"
    )
    assert m.agent_id == "manual-123"
    assert m.client_session_id == "s1"
