"""Check-in/metrics tools hide *cosmetic* identity params, keep attribution keys.

A check-in should read as an ambient binding, not hand-threaded identifiers.
agent_id (auto-resolved structured handle) and agent_name (cosmetic; name-claim
resolution removed 2026-04-17) are stripped from the *advertised* schema while
remaining accepted by the handler as a same-process escape hatch.

client_session_id and continuity_token are deliberately KEPT advertised — a
claude.ai connector only sends advertised params, and these are the attribution
keys: client_session_id carries the unique agent-{uuid} session (vs the server
injecting a shared ip_ua_fingerprint when omitted), and continuity_token has no
injection fallback at all. See src/tool_schemas.py:_hide_auto_injected_identity
and tests/test_onboard_pin.py::TestToolSchemaClientSessionId (the cross-guard).
"""

import pytest

from src.tool_schemas import get_tool_definitions
from src.mcp_handlers.tool_stability import resolve_tool_alias
from src.mcp_handlers.schemas.core import ProcessAgentUpdateParams

HIDDEN = ("agent_id", "agent_name")
KEPT_ATTRIBUTION = ("client_session_id", "continuity_token")
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


@pytest.mark.parametrize("tool_name", CANONICAL)
def test_attribution_keys_stay_advertised(tool_defs, tool_name):
    # Regression lock paired with test_onboard_pin.py::TestToolSchemaClientSessionId.
    # claude.ai only sends advertised params; these two are the unique-attribution
    # keys (client_session_id) and resume proof (continuity_token). Stripping them
    # collapses connector attribution onto a shared fingerprint / breaks resume.
    props = tool_defs[tool_name].inputSchema.get("properties", {})
    missing = [p for p in KEPT_ATTRIBUTION if p not in props]
    assert not missing, f"{tool_name} dropped attribution keys {missing}"


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
