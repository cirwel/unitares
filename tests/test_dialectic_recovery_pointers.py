"""Guard: dialectic recovery payloads point at the current tool surface.

The dialectic handlers were consolidated (`get_dialectic_session`,
`list_dialectic_sessions`, `submit_antithesis`, `list_agents`,
`get_agent_api_key`, ... → `dialectic` / `agent` / `identity`). Recovery
payloads must not hand an agent a `related_tools` pointer it cannot see in its
tool list, or the error's own remediation is a dead end. This test pins the
specific stale names that drifted, so a future regression is caught here.
"""

from __future__ import annotations

import pytest

from src.mcp_handlers.dialectic import responses

# Consolidated old names that are no longer exposed as standalone tools.
# `request_dialectic_review` is deliberately excluded — it was restored as an
# active tool (tool_stability: BETA), so it is a valid pointer.
STALE_TOOL_NAMES = {
    "get_dialectic_session",
    "list_dialectic_sessions",
    "submit_thesis",
    "submit_antithesis",
    "list_agents",
    "get_agent_api_key",
}

# Recovery builders that emit a `related_tools` list, with call args.
RECOVERY_BUILDERS = [
    lambda: responses.missing_session_id_recovery(),
    lambda: responses.session_not_found_recovery(),
    lambda: responses.get_reviewer_reassigned_recovery(None, "reviewer-x"),
    lambda: responses.get_awaiting_facilitation_recovery("sess-123"),
    lambda: responses.get_agent_not_found_recovery(),
    lambda: responses.no_sessions_found_recovery(),
    lambda: responses.missing_session_or_agent_recovery(),
    lambda: responses.get_session_exception_recovery(),
    lambda: responses.get_session_timeout_recovery("timed out"),
    lambda: responses.get_reviewer_stuck_recovery("reviewer-x"),
    lambda: responses.llm_unavailable_recovery(),
    lambda: responses.llm_failed_recovery(),
]


@pytest.mark.parametrize("builder", RECOVERY_BUILDERS)
def test_recovery_related_tools_are_current_surface(builder):
    payload = builder()
    related = payload.get("related_tools", [])
    stale = STALE_TOOL_NAMES.intersection(related)
    assert not stale, (
        f"recovery payload points at consolidated tool name(s) {stale}; "
        f"use the current surface (dialectic / agent / identity)"
    )
