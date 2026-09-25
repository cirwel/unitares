"""The margin's scope reaches compact and standard check-ins.

A compact sync_state used to carry only margin and nearest_edge, so an agent
whose coherence edge was never assessed (every agent today) read
margin="comfortable" with nothing saying which edges it covered. The envelope
already lifts margin_scope and unmeasurable_edges beside the margin; the
formatter dropped them before they got there.
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from src.mcp_handlers.middleware.envelope_step import build_experience_envelope
from src.mcp_handlers.response_formatter import format_response


def _source(**decision_overrides) -> dict:
    decision = {
        "action": "proceed",
        "sub_action": "approve",
        "reason": "Low risk (27.0%) - healthy operating range",
        "margin": "comfortable",
        "nearest_edge": None,
        "unmeasurable_edges": ["coherence"],
        "margin_scope": "measured_edges_only",
    }
    decision.update(decision_overrides)
    return {
        "success": True,
        "status": "healthy",
        "health_status": "healthy",
        "decision": decision,
        "metrics": {
            "E": 0.72, "I": 0.79, "S": 0.21, "V": -0.02,
            "coherence": 0.48, "risk_score": 0.27, "verdict": "safe",
            "health_status": "healthy",
        },
    }


@pytest.mark.parametrize("mode", ["compact", "standard"])
def test_unassessed_edges_reach_the_formatted_response(mode):
    formatted = format_response(deepcopy(_source()), {"response_mode": mode})
    decision = formatted["decision"] if isinstance(formatted.get("decision"), dict) else formatted
    assert decision["margin"] == "comfortable"
    assert decision["unmeasurable_edges"] == ["coherence"]
    assert decision["margin_scope"] == "measured_edges_only"


def test_unassessed_edges_reach_the_sync_state_envelope():
    formatted = format_response(deepcopy(_source()), {"response_mode": "auto"})
    env = build_experience_envelope(
        "sync_state", "process_agent_update", formatted, {"response_mode": "auto"}
    )
    state = env["state_summary"]
    assert state["margin"] == "comfortable"
    assert state["unmeasurable_edges"] == ["coherence"]
    assert state["margin_scope"] == "measured_edges_only"


@pytest.mark.parametrize("mode", ["compact", "standard"])
def test_fully_assessed_margin_adds_nothing(mode):
    formatted = format_response(
        deepcopy(_source(unmeasurable_edges=[], margin_scope="all_edges")),
        {"response_mode": mode},
    )
    decision = formatted["decision"] if isinstance(formatted.get("decision"), dict) else formatted
    assert "unmeasurable_edges" not in decision
    assert "margin_scope" not in decision


def test_minimal_mode_carries_the_scope_beside_its_margin():
    formatted = format_response(deepcopy(_source()), {"response_mode": "minimal"})
    assert formatted["margin"] == "comfortable"
    assert formatted["unmeasurable_edges"] == ["coherence"]
    assert formatted["margin_scope"] == "measured_edges_only"


def test_mirror_mode_carries_the_scope_on_an_actionable_margin():
    # Mirror shows a margin only when it is actionable; tight is the other
    # level where an edge can be unassessed.
    formatted = format_response(
        deepcopy(_source(margin="tight", nearest_edge="risk")),
        {"response_mode": "mirror"},
    )
    assert formatted["margin"] == "tight"
    assert formatted["unmeasurable_edges"] == ["coherence"]
    assert formatted["margin_scope"] == "measured_edges_only"


def test_mirror_mode_still_hides_a_comfortable_margin():
    formatted = format_response(deepcopy(_source()), {"response_mode": "mirror"})
    assert "margin" not in formatted
    assert "unmeasurable_edges" not in formatted
