"""Sentinel must compare coordinated coherence movement within one producer."""

from unittest.mock import patch

from agents.sentinel.agent import AgentSnapshot, FleetState


def _event(agent_id: str, coherence: float, source: str, role: str) -> dict:
    return {
        "type": "eisv_update",
        "agent_id": agent_id,
        "agent_name": agent_id,
        "eisv": {"E": 0.7, "I": 0.7, "S": 0.2, "V": 0.0},
        "coherence": coherence,
        "metrics": {"coherence_source": source, "coherence_role": role},
        "decision": {"action": "proceed"},
    }


def test_mixed_producers_are_not_compared_within_an_agent():
    snapshot = AgentSnapshot("agent-a")
    with patch("agents.sentinel.agent.time.time", side_effect=[100.0, 101.0, 101.0, 101.0]):
        snapshot.record(_event("agent-a", 0.9, "legacy_tanh_v", "ode_control_feedback"))
        snapshot.record(_event("agent-a", 0.6, "manifold", "eis_structural_measurement"))
        assert snapshot.coherence_drop() == 0.0
        assert snapshot.coherence_provenance() is None


def test_coordinated_finding_is_stratified_and_source_tagged():
    fleet = FleetState()
    with patch("agents.sentinel.agent.time.time", return_value=100.0):
        for agent_id in ("agent-a", "agent-b"):
            fleet.ingest(_event(agent_id, 0.9, "legacy_tanh_v", "ode_control_feedback"))
    with patch("agents.sentinel.agent.time.time", return_value=101.0):
        for agent_id in ("agent-a", "agent-b"):
            fleet.ingest(_event(agent_id, 0.6, "legacy_tanh_v", "ode_control_feedback"))
        findings = fleet.analyze()

    finding = next(item for item in findings if item["type"] == "coordinated_degradation")
    assert finding["coherence_source"] == "legacy_tanh_v"
    assert finding["coherence_role"] == "ode_control_feedback"
    assert "not a health diagnosis" in finding["summary"]


def test_different_producers_do_not_form_one_coordinated_finding():
    fleet = FleetState()
    producers = {
        "agent-a": ("legacy_tanh_v", "ode_control_feedback"),
        "agent-b": ("manifold", "eis_structural_measurement"),
    }
    with patch("agents.sentinel.agent.time.time", return_value=100.0):
        for agent_id, (source, role) in producers.items():
            fleet.ingest(_event(agent_id, 0.9, source, role))
    with patch("agents.sentinel.agent.time.time", return_value=101.0):
        for agent_id, (source, role) in producers.items():
            fleet.ingest(_event(agent_id, 0.6, source, role))
        findings = fleet.analyze()

    assert not any(item["type"] == "coordinated_degradation" for item in findings)
