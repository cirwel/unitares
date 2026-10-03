"""Revision hints name a route the caller's advertised surface can take.

update_finding is not in PROGRESSIVE_MODE_TOOLS (council rec 2026-10-03:
advertising it costs ~3.4 KB of tools/list against the down-only ratchet), so
progressive callers revise through use_tool; full mode keeps the bare call.
"""

from src import tool_modes
from src.mcp_handlers.middleware.envelope_step import build_experience_envelope
from src.tool_modes import PROGRESSIVE_MODE_TOOLS, progressive_aware_hint

BARE = "update_finding(discovery_id='d-1', status='resolved', resolution_notes='...')"


def test_update_finding_stays_unadvertised_in_progressive_mode():
    assert "update_finding" not in PROGRESSIVE_MODE_TOOLS


def test_progressive_hint_uses_use_tool():
    out = progressive_aware_hint(BARE, mode="progressive")
    assert out == (
        "use_tool(tool_name='update_finding', arguments={\"discovery_id\": \"d-1\", "
        "\"status\": \"resolved\", \"resolution_notes\": \"...\"})"
    )


def test_rewrite_is_independent_of_mode():
    # A per-request ?mode=progressive client on a server configured full must
    # not be told to call the unadvertised bare update_finding.
    assert progressive_aware_hint(BARE, mode="full") == progressive_aware_hint(
        BARE, mode="progressive"
    )


def test_unparseable_arguments_are_left_alone():
    odd = "update_finding(discovery_id=d_1)"
    assert progressive_aware_hint(odd, mode="progressive") == odd


def _store_envelope():
    payload = {
        "success": True,
        "message": "Discovery stored for agent 'agent-1'",
        "discovery_id": "d-9",
        "discovery": {"id": "d-9", "type": "note", "status": "open", "summary": "s"},
    }
    return build_experience_envelope("store_finding", "knowledge", payload, {"summary": "s"})


def test_store_finding_next_action_is_safe_in_every_process_mode(monkeypatch):
    for mode in ("progressive", "full"):
        monkeypatch.setattr(tool_modes, "TOOL_MODE", mode)
        action = _store_envelope()["next_action"]
        assert "use_tool(tool_name='update_finding'" in action
        assert "update_finding(discovery_id" not in action


def test_search_next_action_points_at_a_revision_route(monkeypatch):
    monkeypatch.setattr(tool_modes, "TOOL_MODE", "progressive")
    payload = {"success": True, "results": [{"id": "d-1", "summary": "x"}], "total_count": 1}
    env = build_experience_envelope(
        "search_shared_memory", "knowledge", payload, {"query": "x"}
    )
    assert "use_tool(tool_name='update_finding'" in env["next_action"]
