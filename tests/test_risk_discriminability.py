"""F1(b) tests: risk_score must be flagged non-discriminative during bootstrap.

During behavioral bootstrap the phi-based risk keys on baseline-deviation terms
that sit near zero, so risk_score does not track absolute drift magnitude. The
payload must carry an explicit discriminability caveat rather than emitting a
confident margin-to-PAUSE in that window.
"""

from src.monitor_result import _build_risk_attribution


def _metrics():
    return {"risk_score": 0.26, "verdict": "safe"}


def test_no_baseline_status_omits_discriminability():
    # Back-compat: callers that pass no baseline_status get no new key.
    attr = _build_risk_attribution(_metrics(), None, None, None)
    assert "discriminability" not in attr


def test_bootstrap_flags_non_discriminative():
    status = {"is_baselined": False, "updates_completed": 4, "baseline_target": 30}
    attr = _build_risk_attribution(_metrics(), None, None, None, baseline_status=status)
    disc = attr["discriminability"]
    assert disc["baselined"] is False
    assert disc["non_discriminative"] is True
    assert disc["updates_until_baseline"] == 26
    assert disc["note"] is not None
    assert "non-discriminative" in disc["note"]


def test_baselined_clears_caveat():
    status = {"is_baselined": True, "updates_completed": 35, "baseline_target": 30}
    attr = _build_risk_attribution(_metrics(), None, None, None, baseline_status=status)
    disc = attr["discriminability"]
    assert disc["baselined"] is True
    assert disc["non_discriminative"] is False
    assert disc["updates_until_baseline"] == 0
    assert disc["note"] is None


# --- primary_driver accuracy (correction 2026-06-28) -------------------------
# The verdict driver must reflect the actual posture, not a hardcoded
# "self_reported". With Φ telemetry (default UNITARES_PHI_TELEMETRY_ONLY=1) a
# warm behavioral verdict is authoritative; cold-start falls back to the Φ prior.

class _StubAssessment:
    def __init__(self, verdict="safe", risk=0.1):
        self.verdict = verdict
        self.risk = risk


def test_cold_start_driver_is_phi_not_self_reported(monkeypatch):
    monkeypatch.setenv("UNITARES_PHI_TELEMETRY_ONLY", "1")
    # No behavioral assessment / sub-warmup confidence → Φ cold-start prior.
    attr = _build_risk_attribution(_metrics(), None, None, None, behavioral_confidence=0.1)
    assert attr["primary_driver"] == "phi_cold_start"
    # The legacy mislabel must be gone.
    assert attr["primary_driver"] != "self_reported"
    assert "self_reported" not in attr["sources"]
    assert "phi_drift" in attr["sources"]


def test_warm_driver_is_behavioral_under_phi_telemetry(monkeypatch):
    monkeypatch.setenv("UNITARES_PHI_TELEMETRY_ONLY", "1")
    attr = _build_risk_attribution(
        _metrics(), None, None, _StubAssessment(verdict="high-risk", risk=0.8),
        behavioral_confidence=0.5,
    )
    assert attr["primary_driver"] == "behavioral_assessment"


def test_warm_driver_is_phi_floor_when_telemetry_off(monkeypatch):
    monkeypatch.setenv("UNITARES_PHI_TELEMETRY_ONLY", "0")
    attr = _build_risk_attribution(
        _metrics(), None, None, _StubAssessment(verdict="caution", risk=0.5),
        behavioral_confidence=0.5,
    )
    assert attr["primary_driver"] == "phi_floor"
