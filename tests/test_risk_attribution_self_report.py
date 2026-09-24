"""risk_attribution must describe what happened to the self-reported drift.

The MCP check-in handler passes ethical_drift as an ndarray, which the drift
blend's list/tuple test rejects, so on that path the report never enters the
vector. Direct Python callers passing a list do get the capped blend. The
attribution text has to be true on both paths.
"""

import uuid

import numpy as np

from src.governance_monitor import UNITARESMonitor

TEXT = "Implemented the requested change and ran the focused tests; they pass."


def _attribution(ethical_drift, warm_updates=0, sensor_eisv=None):
    monitor = UNITARESMonitor(f"test-attr-self-report-{uuid.uuid4().hex[:12]}", load_state=False)
    for _ in range(warm_updates):
        monitor.process_update(
            {"parameters": np.array([]), "ethical_drift": [0.0, 0.0, 0.0],
             "response_text": TEXT, "complexity": 0.5},
            confidence=0.7,
        )
    result = monitor.process_update(
        {
            "parameters": np.array([]),
            "ethical_drift": ethical_drift,
            "response_text": TEXT,
            "complexity": 0.5,
            **({"sensor_eisv": sensor_eisv, "sensor_eisv_source": "behavioral"} if sensor_eisv else {}),
        },
        confidence=0.7,
    )
    return result["risk_attribution"]


def _texts(attribution):
    return attribution["sources"]["phi_drift"]["description"] + " " + attribution.get("note", "")


def test_mcp_path_ndarray_report_is_described_as_not_entering():
    text = _texts(_attribution(np.array([1.0, 1.0, 1.0])))
    assert "did not enter" in text
    assert "was blended" not in text


def test_direct_list_report_is_described_as_blended():
    text = _texts(_attribution([1.0, 1.0, 1.0]))
    assert "was blended in at a capped 30%" in text
    assert "did not enter" not in text


def test_zero_report_is_described_as_not_entering():
    text = _texts(_attribution([0.0, 0.0, 0.0]))
    assert "did not enter" in text


def test_warm_blended_report_is_not_called_independent():
    attribution = _attribution([1.0, 1.0, 1.0], warm_updates=5)
    assert attribution["primary_driver"] == "behavioral_assessment"
    assert "not independent of your report" in attribution["note"]


def test_warm_mcp_path_report_keeps_independent_wording():
    attribution = _attribution(np.array([1.0, 1.0, 1.0]), warm_updates=5)
    assert attribution["primary_driver"] == "behavioral_assessment"
    assert "independent behavioral assessment" in attribution["note"]


def test_warm_blended_report_with_supplied_sensor_is_not_called_independent():
    # A supplied sensor_eisv bypasses the behavioral sensor, but the blended
    # vector still reaches the assessment via ODE-derived inputs.
    attribution = _attribution(
        [1.0, 1.0, 1.0], warm_updates=5,
        sensor_eisv={"E": 0.7, "I": 0.7, "S": 0.3, "V": 0.0},
    )
    assert attribution["primary_driver"] == "behavioral_assessment"
    assert "not independent of your report" in attribution["note"]


def test_zero_report_after_a_blended_report_is_not_called_independent():
    monitor = UNITARESMonitor(f"test-attr-carry-{uuid.uuid4().hex[:12]}", load_state=False)
    def update(drift):
        return monitor.process_update(
            {"parameters": np.array([]), "ethical_drift": drift,
             "response_text": TEXT, "complexity": 0.5},
            confidence=0.7,
        )["risk_attribution"]
    for _ in range(5):
        update([0.0, 0.0, 0.0])
    assert "independent behavioral assessment" in update([0.0, 0.0, 0.0])["note"]
    assert "not independent of your report" in update([1.0, 1.0, 1.0])["note"]
    assert "not independent of your report" in update([0.0, 0.0, 0.0])["note"]


def test_simulated_blended_update_does_not_mark_the_monitor():
    monitor = UNITARESMonitor(f"test-attr-sim-{uuid.uuid4().hex[:12]}", load_state=False)
    base = {"parameters": np.array([]), "response_text": TEXT, "complexity": 0.5}
    for _ in range(5):
        monitor.process_update({**base, "ethical_drift": [0.0, 0.0, 0.0]}, confidence=0.7)
    preview = monitor.simulate_update({**base, "ethical_drift": [1.0, 1.0, 1.0]}, confidence=0.7)
    # The preview itself blended, so it must not be called independent...
    assert "not independent of your report" in preview["risk_attribution"]["note"]
    # ...but the dry run leaves no mark on the monitor.
    assert not getattr(monitor, "_self_report_ever_blended", False)
    note = monitor.process_update(
        {**base, "ethical_drift": np.array([0.0, 0.0, 0.0])}, confidence=0.7
    )["risk_attribution"]["note"]
    assert "independent behavioral assessment" in note


def test_drift_vector_provenance_is_described_as_mixed():
    # Even when the ethical_drift self-report is rejected (MCP ndarray path),
    # complexity divergence depends on the caller's reported complexity, so
    # the vector must not be labelled purely server-derived.
    attribution = _attribution(np.array([1.0, 1.0, 1.0]))
    description = attribution["sources"]["phi_drift"]["description"]
    assert "mixed provenance" in description
    assert "the complexity the agent reports" in description
    assert "It is server-derived" not in description
    assert "server-derived signals" not in attribution["note"]
    assert "depends on the complexity you report" in attribution["note"]


def test_calibration_source_is_not_attributed_to_the_agent_alone():
    # Once the tactical record is populated, calibration error comes from the
    # process-wide calibration checker (all agents), not this caller's
    # confidence; the fallback compares this agent's confidence with its own
    # baseline. The description has to name both.
    description = _attribution(np.array([0.0, 0.0, 0.0]))["sources"]["phi_drift"]["description"]
    assert "server-wide record" in description
    assert "not this one alone" in description
    assert "its own running baseline" in description


def test_cold_start_blended_note_does_not_call_behavioral_independent():
    # A blended list report reaches the behavioral inputs via the ODE during
    # check-ins 1-2 too, so the cold-start note must not call it independent.
    attribution = _attribution([1.0, 1.0, 1.0])
    assert attribution["primary_driver"] != "behavioral_assessment"
    assert "was blended in at a capped 30%" in attribution["note"]
    assert "independent behavioral signal" not in attribution["note"]
