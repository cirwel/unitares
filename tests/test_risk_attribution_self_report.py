"""Self-reported ethical_drift is telemetry only, and risk_attribution says so.

An earlier capped 30% blend ran only for a list/tuple report, so the MCP
check-in path (an ndarray) never blended while direct Python callers did. The
blend is removed (operator-delegated council decision 2026-10-11): no report,
of any type, changes the drift vector, and the attribution text states that.
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


def _drift_run(ethical_drift, warm_updates=3):
    """Run identical check-ins differing only in the self-report; return the
    final drift vector, the result, and the monitor."""
    monitor = UNITARESMonitor(f"test-drift-self-report-{uuid.uuid4().hex[:12]}", load_state=False)
    result = None
    for _ in range(warm_updates + 1):
        result = monitor.process_update(
            {"parameters": np.array([]), "ethical_drift": ethical_drift,
             "response_text": TEXT, "complexity": 0.5},
            confidence=0.7,
        )
    return monitor._last_drift_vector.to_list(), result, monitor


def test_self_report_has_no_effect_on_drift_whatever_its_type():
    # Operator-delegated council decision 2026-10-11: self-reported
    # ethical_drift is telemetry only. A list, a tuple and an ndarray report,
    # maxed or zero, must give the identical governance-computed vector and
    # verdict; before this change a list blended in at 30% and an ndarray
    # did not, purely by type.
    reference_drift, reference, _ = _drift_run(np.array([0.0, 0.0, 0.0]))
    for report in ([1.0, 1.0, 1.0], (1.0, 1.0, 1.0), np.array([1.0, 1.0, 1.0]), [0.0, 0.0, 0.0]):
        drift, result, _ = _drift_run(report)
        assert drift == reference_drift, report
        assert result["metrics"]["verdict"] == reference["metrics"]["verdict"]
        assert result["metrics"]["risk_score"] == reference["metrics"]["risk_score"]
        assert result["decision"]["action"] == reference["decision"]["action"]


def test_self_report_norm_is_kept_as_telemetry_for_list_and_ndarray():
    from src.monitor_drift import compute_drift_vector

    for report in ([0.6, 0.8, 0.0], np.array([0.6, 0.8, 0.0])):
        _, _, monitor = _drift_run([0.0, 0.0, 0.0], warm_updates=0)
        grounded = {"ethical_drift": None, "complexity": 0.5}
        _, norm = compute_drift_vector(
            monitor, grounded, {"ethical_drift": report}, 0.7, "mixed",
            monitor._last_continuity_metrics,
        )
        assert abs(norm - 1.0) < 1e-9
        # The vector written for the dynamics is the computed one, not the report.
        assert grounded["ethical_drift"] == monitor._last_drift_vector.to_list()


def test_self_report_norm_tolerates_missing_or_malformed_reports():
    from src.monitor_drift import _self_report_norm

    assert _self_report_norm(None) == 0.0
    assert _self_report_norm([]) == 0.0
    assert _self_report_norm("abc") == 0.0
    assert _self_report_norm([float("nan"), 1.0]) == 0.0
    assert _self_report_norm(np.array([3.0, 4.0])) == 5.0


def test_agent_profile_still_records_the_self_report_magnitude():
    # The MCP check-in hands the raw report to the per-agent profile
    # (phases.py, record_checkin); that telemetry sink is unchanged.
    from src.agent_profile import AgentProfile

    profile = AgentProfile()
    profile.record_checkin(complexity=0.5, confidence=0.7, ethical_drift=[0.6, 0.8, 0.0])
    assert profile._drift_count == 1
    assert abs(profile._drift_max_seen - 1.0) < 1e-9


def test_report_is_described_as_not_blended_for_list_and_ndarray():
    for report in ([1.0, 1.0, 1.0], np.array([1.0, 1.0, 1.0]), [0.0, 0.0, 0.0]):
        text = _texts(_attribution(report))
        assert "is not blended into this vector" in text
        assert "does not enter it" in text
        assert "recorded as telemetry" not in text
        assert "was blended" not in text
        assert "capped 30%" not in text


def test_warm_report_keeps_independent_wording_for_list_and_ndarray():
    for report in ([1.0, 1.0, 1.0], np.array([1.0, 1.0, 1.0])):
        attribution = _attribution(report, warm_updates=5)
        assert attribution["primary_driver"] == "behavioral_assessment"
        assert "independent behavioral assessment" in attribution["note"]
        assert "not independent of your report" not in attribution["note"]


def test_blend_flags_are_gone_from_the_monitor():
    _, _, monitor = _drift_run([1.0, 1.0, 1.0])
    assert not hasattr(monitor, "_last_self_report_blended")
    assert not hasattr(monitor, "_self_report_ever_blended")


def test_drift_vector_provenance_is_described_as_mixed():
    # Even though the ethical_drift self-report never enters the vector,
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


def test_threat_model_states_the_exclusion_as_a_rule():
    from pathlib import Path

    doc = (Path(__file__).resolve().parents[1] / "docs" / "SCOPE_AND_THREAT_MODEL.md").read_text()
    flat = " ".join(doc.split())
    assert "do get the blend" not in flat
    assert "in any phase, on any path: it is never an input" in flat
    # The telemetry claim is scoped to the path that actually records it.
    assert "a direct Python call to `process_update()` does not record it" in flat


def test_omitted_confidence_and_complexity_defaults_are_disclosed():
    # With no confidence, compute_drift_vector substitutes 0.6; with no
    # complexity, the MCP schema fills 0.5 and a direct call gets a fixed 0.2
    # divergence. The description must not present those as agent reports.
    description = _attribution(np.array([0.0, 0.0, 0.0]))["sources"]["phi_drift"]["description"]
    assert "a fixed 0.6 when it reports none" in description
    assert "omits it is read as 0.5" in description
    assert "fixed divergence of 0.2" in description
