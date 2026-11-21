"""Past cold start with too little read, no verdict is drawn (hold only)."""

from __future__ import annotations

import pytest

from src.behavioral_state import BOOTSTRAP_UPDATES, STALE_AFTER_UPDATES, BehavioralEISV
from src.cold_start_risk_confirmation import (
    BEHAVIORAL_AUTHORITY_THRESHOLD,
    INSUFFICIENT_READING_DRIVER,
    classify_verdict_driver,
    reading_insufficient,
)

CHECK_IN = {"response_text": "Reviewed the change and ran the tests."}


@pytest.fixture(autouse=True)
def _hold_on(monkeypatch):
    """The rule exists only under the hold, so these tests switch it on."""
    monkeypatch.setenv("UNITARES_HOLD_UNREAD_AXES", "1")
    monkeypatch.setenv("UNITARES_SENSOR_DRIFT_AGENT_LOCAL", "1")


def _insufficient(**overrides) -> bool:
    arguments = dict(
        hold_unread=True,
        behavioral_confidence=0.0,
        checkin_confidence=1.0,
        behavioral_enabled=True,
        phi_telemetry=True,
    )
    arguments.update(overrides)
    return reading_insufficient(**arguments)


# --- the rule ---------------------------------------------------------------


def test_past_cold_start_with_nothing_read_is_insufficient():
    assert _insufficient() is True


def test_cold_start_by_checkin_count_stays_with_the_prior():
    below = BEHAVIORAL_AUTHORITY_THRESHOLD - 0.1
    assert _insufficient(checkin_confidence=below) is False
    assert _insufficient(checkin_confidence=BEHAVIORAL_AUTHORITY_THRESHOLD) is True


def test_a_state_with_authority_is_not_insufficient():
    assert _insufficient(behavioral_confidence=BEHAVIORAL_AUTHORITY_THRESHOLD) is False


def test_a_little_read_is_treated_like_nothing_read():
    # One or two readings must not be worse for the agent than none.
    assert _insufficient(behavioral_confidence=0.1) is True
    assert _insufficient(behavioral_confidence=0.2) is True


@pytest.mark.parametrize(
    "overrides",
    [
        {"hold_unread": False},
        {"behavioral_enabled": False},
        # Phi configured to floor the verdict is an authority the operator chose.
        {"phi_telemetry": False},
        {"behavioral_confidence": None},
        {"checkin_confidence": None},
        {"behavioral_confidence": float("nan")},
    ],
)
def test_the_rule_is_off_outside_its_scope(overrides):
    assert _insufficient(**overrides) is False


def test_checkin_confidence_skips_the_checkins_the_sensor_cannot_read():
    from src.behavioral_sensor import SENSOR_MIN_HISTORY

    state = BehavioralEISV()
    for _ in range(SENSOR_MIN_HISTORY):
        state.update(None, None, None)
    assert state.confidence == 0.0
    assert state.checkin_confidence == 0.0
    for _ in range(3):
        state.update(None, None, None)
    # Check-in 6: where an agent that supplied everything reaches authority.
    assert state.update_count == 6
    assert state.checkin_confidence == pytest.approx(BEHAVIORAL_AUTHORITY_THRESHOLD)
    for _ in range(BOOTSTRAP_UPDATES):
        state.update(None, None, None)
    assert state.checkin_confidence == 1.0


def test_the_sensor_reads_nothing_before_its_minimum_history():
    from src.behavioral_sensor import SENSOR_MIN_HISTORY, compute_behavioral_sensor_components

    def components(n):
        return compute_behavioral_sensor_components(
            decision_history=["proceed"] * n, coherence_history=[0.5] * n,
            regime_history=[], E_history=[], I_history=[], S_history=[], V_history=[],
        )

    assert components(SENSOR_MIN_HISTORY - 1) is None
    assert components(SENSOR_MIN_HISTORY) is not None


def test_a_supplying_agent_has_authority_exactly_where_cold_start_ends():
    # The boundary must not open a gap: on the first check-in at which the
    # rule could apply, an agent that read on every possible check-in already
    # holds authority, so the rule does not apply to it.
    from src.behavioral_sensor import SENSOR_MIN_HISTORY

    state = BehavioralEISV()
    for index in range(1, 9):
        if index <= SENSOR_MIN_HISTORY:
            state.update(None, None, None)
        else:
            state.update(0.7, 0.7, 0.2)
        assert not _insufficient(
            behavioral_confidence=state.confidence,
            checkin_confidence=state.checkin_confidence,
        ), index


def test_checkin_confidence_equals_confidence_with_the_hold_off(monkeypatch):
    monkeypatch.setenv("UNITARES_HOLD_UNREAD_AXES", "0")
    state = BehavioralEISV()
    for _ in range(4):
        state.update(0.7, 0.7, 0.2)
        assert state.checkin_confidence == state.confidence


def test_an_agent_whose_readings_went_stale_becomes_insufficient():
    state = BehavioralEISV()
    for _ in range(12):
        state.update(0.7, 0.7, 0.2)
    assert not _insufficient(
        behavioral_confidence=state.confidence, checkin_confidence=state.checkin_confidence
    )
    for _ in range(STALE_AFTER_UPDATES):
        state.update(None, None, None)
    assert _insufficient(
        behavioral_confidence=state.confidence, checkin_confidence=state.checkin_confidence
    )


# --- the driver label -------------------------------------------------------


def _driver(**overrides) -> str:
    arguments = dict(
        behavioral_confidence=0.0,
        behavioral_verdict="safe",
        behavioral_enabled=True,
        phi_telemetry=True,
    )
    arguments.update(overrides)
    return classify_verdict_driver(**arguments)


def test_driver_names_insufficient_reading():
    assert _driver(insufficient_reading=True) == INSUFFICIENT_READING_DRIVER


def test_driver_defaults_to_the_cold_start_prior():
    assert _driver() == "phi_cold_start"


def test_a_warm_behavioral_state_keeps_its_label():
    assert _driver(behavioral_confidence=0.5, insufficient_reading=True) == "behavioral_assessment"


# --- the check-in path ------------------------------------------------------


def _high_risk_phi(monitor, monkeypatch):
    """Make the Phi path return a pause-grade verdict on every check-in."""
    real = monitor._compute_phi_and_risk

    def worst(*args, **kwargs):
        phi, _verdict, _risk, adjustment, original = real(*args, **kwargs)
        return phi, "high-risk", 0.9, adjustment, original

    monkeypatch.setattr(monitor, "_compute_phi_and_risk", worst)


def _silence_readings(monitor, monkeypatch):
    """Give the behavioral state no reading, as for an agent that supplies none."""
    real = monitor._behavioral_state.update
    monkeypatch.setattr(
        monitor._behavioral_state, "update", lambda *_a, **_k: real(None, None, None)
    )


def test_the_prior_owns_cold_start_and_no_verdict_is_drawn_after(monkeypatch):
    from src.governance_monitor import UNITARESMonitor

    monitor = UNITARESMonitor("insufficient-reading-path", load_state=False)
    _high_risk_phi(monitor, monkeypatch)
    _silence_readings(monitor, monkeypatch)

    results = [monitor.process_update(dict(CHECK_IN)) for _ in range(12)]

    # Check-ins 1 to 5: no behavioral reading could carry authority yet, so
    # the prior decides and an alarming start keeps its verdict.
    for result in results[:5]:
        assert result["risk_attribution"]["primary_driver"] == "phi_cold_start"
        assert result["metrics"]["verdict"] == "high-risk"
    for result in results[5:]:
        assert result["risk_attribution"]["primary_driver"] == INSUFFICIENT_READING_DRIVER
        assert result["metrics"]["verdict_source"] == INSUFFICIENT_READING_DRIVER
        assert result["metrics"]["verdict"] == "safe"
        assert result["metrics"]["risk_score"] == 0.0
        assert not result["decision"].get("latest_risk_fast_trip")
        assert set(result["behavioral"]["assessment"]["unscored_axes"]) == set("EISV")
        assert "No verdict was drawn" in result["risk_attribution"]["note"]
    assert monitor._behavioral_state.confidence == 0.0


def test_readings_hand_the_verdict_to_the_behavioral_assessment():
    from src.governance_monitor import UNITARESMonitor

    monitor = UNITARESMonitor("insufficient-reading-warms", load_state=False)
    drivers = [
        monitor.process_update(dict(CHECK_IN))["risk_attribution"]["primary_driver"]
        for _ in range(8)
    ]
    # An agent that supplies readings goes from the prior straight to the
    # behavioral assessment; it never passes through "insufficient reading".
    assert drivers[:5] == ["phi_cold_start"] * 5
    assert drivers[5:] == ["behavioral_assessment"] * 3


def test_phi_configured_as_a_floor_keeps_its_authority(monkeypatch):
    from src.governance_monitor import UNITARESMonitor

    monkeypatch.setenv("UNITARES_PHI_TELEMETRY_ONLY", "0")
    monitor = UNITARESMonitor("insufficient-reading-phi-floor", load_state=False)
    _high_risk_phi(monitor, monkeypatch)
    _silence_readings(monitor, monkeypatch)

    result = None
    for _ in range(8):
        result = monitor.process_update(dict(CHECK_IN))
    assert result["risk_attribution"]["primary_driver"] == "phi_cold_start"
    assert result["metrics"]["verdict"] == "high-risk"


def test_flag_off_never_names_insufficient_reading(monkeypatch):
    from src.governance_monitor import UNITARESMonitor

    monkeypatch.setenv("UNITARES_HOLD_UNREAD_AXES", "0")
    monitor = UNITARESMonitor("insufficient-reading-flag-off", load_state=False)
    drivers = {
        monitor.process_update(dict(CHECK_IN))["risk_attribution"]["primary_driver"]
        for _ in range(8)
    }
    assert INSUFFICIENT_READING_DRIVER not in drivers


def test_the_verification_floor_still_raises_the_verdict(monkeypatch):
    from config.governance_config import GovernanceConfig
    from governance_core import verification
    from src.governance_monitor import UNITARESMonitor

    monkeypatch.setattr(GovernanceConfig, "VERIFICATION_FLOOR_ENABLED", True)
    signal = verification.score_harm_confession("")
    raised = type(signal)(**{**signal.__dict__, "verdict": "high-risk", "score": 0.9})
    monkeypatch.setattr(verification, "score_harm_confession", lambda _text: raised)

    monitor = UNITARESMonitor("insufficient-reading-floor", load_state=False)
    _silence_readings(monitor, monkeypatch)
    result = None
    for _ in range(7):
        result = monitor.process_update(dict(CHECK_IN))
    assert result["metrics"]["verdict"] == "high-risk"
    assert result["risk_attribution"]["primary_driver"] == "independent_verification_floor"


# --- what the agent is told -------------------------------------------------


def _payload() -> dict:
    return {
        "risk_attribution": {"primary_driver": INSUFFICIENT_READING_DRIVER},
        "metrics": {"primary_eisv_source": "ode_fallback"},
    }


def test_insufficient_reading_is_not_reported_as_cold_start():
    from src.mcp_handlers.middleware.envelope_helpers import _is_cold_start

    assert _is_cold_start(_payload()) is False


def test_insufficient_reading_is_provisional_and_named():
    from src.mcp_handlers.middleware.envelope_helpers import _verdict_assurance

    assert _verdict_assurance(_payload()) == ("provisional", INSUFFICIENT_READING_DRIVER)


# --- response formats that drop risk_attribution (review finding) -----------


def _insufficient_result(monkeypatch) -> dict:
    from src.governance_monitor import UNITARESMonitor

    monitor = UNITARESMonitor("insufficient-reading-formats", load_state=False)
    _silence_readings(monitor, monkeypatch)
    result = None
    for _ in range(7):
        result = monitor.process_update(dict(CHECK_IN))
    assert result["metrics"]["verdict_source"] == INSUFFICIENT_READING_DRIVER
    return result


def _formatted(result: dict, mode: str) -> dict:
    from src.mcp_handlers import response_formatter as formatter

    if mode == "standard":
        return formatter._format_standard(dict(result), "mixed")
    if mode == "minimal":
        return formatter._format_minimal(dict(result), False, None)
    return formatter._format_compact(dict(result), False, None)


def _wrapped_verdict(formatted: dict) -> dict:
    verdict = formatted.get("verdict")
    if not isinstance(verdict, dict):
        verdict = (formatted.get("metrics") or {}).get("verdict")
    assert isinstance(verdict, dict), formatted.keys()
    return verdict


@pytest.mark.parametrize("mode", ["standard", "minimal", "compact"])
def test_formats_without_risk_attribution_still_name_the_driver(monkeypatch, mode):
    from src.mcp_handlers.middleware.envelope_helpers import _is_cold_start, _verdict_assurance

    formatted = _formatted(_insufficient_result(monkeypatch), mode)
    verdict = _wrapped_verdict(formatted)

    assert verdict["evidence"]["basis"] == INSUFFICIENT_READING_DRIVER
    assert "no verdict was drawn" in verdict["meaning"]
    assert "cold-start prior" not in verdict["meaning"]
    # The envelope reads the formatted payload, with or without attribution.
    formatted.pop("risk_attribution", None)
    assert _is_cold_start(formatted) is False
    assert _verdict_assurance(formatted) == ("provisional", INSUFFICIENT_READING_DRIVER)


def test_explain_verdict_is_unchanged_without_the_driver():
    from src.governance_glossary import explain_verdict

    assert explain_verdict("safe", evidence_source="ode_fallback") == explain_verdict(
        "safe", evidence_source="ode_fallback", verdict_driver="phi_cold_start"
    )
    assert explain_verdict("safe")["value"] == "safe"
    assert "evidence" not in explain_verdict("safe", verdict_driver="behavioral_assessment")
