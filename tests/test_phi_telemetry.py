"""Φ→telemetry: resolve_verdict_risk (governance_monitor) + the flag.

Default = Φ floors verdict/risk (more_severe / max). UNITARES_PHI_TELEMETRY_ONLY
makes the behavioral assessment authoritative when usable — which can only
*de-escalate* (never adds a pause), and is byte-identical to the floor when off
or when behavioral is unavailable.
"""
import pytest

from src.governance_monitor import resolve_verdict_risk
from config.governance_config import phi_telemetry_only


def test_flag_off_floors_with_phi():
    # behavioral safer than Φ → floor keeps the worse Φ verdict/risk
    v, r = resolve_verdict_risk("caution", 0.5, "safe", 0.2, phi_telemetry=False)
    assert v == "caution" and r == 0.5


def test_flag_on_behavioral_authoritative_deescalates():
    v, r = resolve_verdict_risk("caution", 0.5, "safe", 0.2, phi_telemetry=True)
    assert v == "safe" and r == 0.2  # Φ no longer floors → de-escalates


def test_flag_on_never_escalates_beyond_behavioral():
    # behavioral worse than Φ → both paths pick the worse (no difference)
    off = resolve_verdict_risk("safe", 0.2, "high-risk", 0.9, phi_telemetry=False)
    on = resolve_verdict_risk("safe", 0.2, "high-risk", 0.9, phi_telemetry=True)
    assert on == off == ("high-risk", 0.9)


def test_flag_on_missing_behavioral_falls_back_to_phi():
    # cold-start prior: no behavioral verdict → Φ floor still applies
    v, r = resolve_verdict_risk("caution", 0.5, None, 0.0, phi_telemetry=True)
    assert v == "caution" and r == 0.5


@pytest.mark.parametrize("pv,pr,bv,br", [
    ("safe", 0.2, "safe", 0.25),
    ("caution", 0.5, "safe", 0.2),
    ("high-risk", 0.9, "caution", 0.4),
    ("safe", 0.1, None, 0.0),
])
def test_off_is_byte_identical_to_historical_floor(pv, pr, bv, br):
    from src.governance_monitor import _more_severe_verdict
    v, r = resolve_verdict_risk(pv, pr, bv, br, phi_telemetry=False)
    assert v == _more_severe_verdict(pv, bv)
    assert r == max(pr, br)


def test_flag_default_on(monkeypatch):
    # Default ON (live-proven maths posture) when the env is unset.
    monkeypatch.delenv("UNITARES_PHI_TELEMETRY_ONLY", raising=False)
    assert phi_telemetry_only() is True


def test_flag_reads_env(monkeypatch):
    monkeypatch.setenv("UNITARES_PHI_TELEMETRY_ONLY", "1")
    assert phi_telemetry_only() is True
    # Explicit off override restores the legacy Φ-floor.
    monkeypatch.setenv("UNITARES_PHI_TELEMETRY_ONLY", "0")
    assert phi_telemetry_only() is False
