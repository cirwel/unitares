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
