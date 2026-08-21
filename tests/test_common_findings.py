"""Tests for agents/common/findings.py — the shared finding-post seam.

Covers the doctor layer's shared governance identity: one identity across the
doctor producers so their findings become adjudicatable, while each producer
keeps its own event_type so per-detector precision stays separable.
"""

import json




class TestDoctorLayerAgentId:
    """One identity for the doctor layer; the slug is the degradation path.

    Slug rows are unadjudicatable — http_sentinel_adjudicate 422s rather than
    book an outcome against the wrong resident — so every doctor finding was a
    refutable claim that could never become an anchor (272 in 30 days).
    """

    def test_returns_the_anchored_uuid(self, tmp_path, monkeypatch):
        from agents.common import findings
        anchor = tmp_path / "doctor.json"
        anchor.write_text(json.dumps(
            {"agent_uuid": "dddddddd-1111-2222-3333-444444444444"}))
        monkeypatch.setattr(findings, "DOCTOR_ANCHOR", str(anchor))
        assert findings.doctor_layer_agent_id("deploy-drift-doctor") == \
            "dddddddd-1111-2222-3333-444444444444"

    def test_missing_anchor_degrades_to_the_slug(self, tmp_path, monkeypatch):
        """Unprovisioned must behave exactly as today, not raise or drop."""
        from agents.common import findings
        monkeypatch.setattr(findings, "DOCTOR_ANCHOR", str(tmp_path / "nope.json"))
        assert findings.doctor_layer_agent_id("lumen-checkin-doctor") == \
            "lumen-checkin-doctor"

    def test_malformed_anchor_degrades_to_the_slug(self, tmp_path, monkeypatch):
        from agents.common import findings
        anchor = tmp_path / "doctor.json"
        anchor.write_text("{not json")
        monkeypatch.setattr(findings, "DOCTOR_ANCHOR", str(anchor))
        assert findings.doctor_layer_agent_id("doctor-findings") == "doctor-findings"

    def test_anchor_without_uuid_degrades_to_the_slug(self, tmp_path, monkeypatch):
        from agents.common import findings
        anchor = tmp_path / "doctor.json"
        anchor.write_text(json.dumps({"display_name": "Doctor"}))
        monkeypatch.setattr(findings, "DOCTOR_ANCHOR", str(anchor))
        assert findings.doctor_layer_agent_id("bridge-liveness-watchdog") == \
            "bridge-liveness-watchdog"

    def test_never_calls_governance(self, tmp_path, monkeypatch):
        """These run on cron ticks and a watchdog path — a round-trip here is a
        hazard, not a cost. Guard the constraint, not just the return value."""
        from agents.common import findings
        import httpx

        def _boom(*a, **k):
            raise AssertionError("doctor_layer_agent_id must not do network I/O")

        monkeypatch.setattr(httpx, "post", _boom, raising=False)
        monkeypatch.setattr(httpx, "Client", _boom, raising=False)
        monkeypatch.setattr(findings, "DOCTOR_ANCHOR", str(tmp_path / "nope.json"))
        assert findings.doctor_layer_agent_id("doctor-findings") == "doctor-findings"


class TestDoctorOutcomeLabelStaysPerFamily:
    """⛔The identity consolidates; the event_type must NOT.

    These detectors have very different precision and very different volume
    (doctor_check 136, bridge 61, drift 24, lumen_checkin 15 per 30d), so one
    shared label would move with the volume mix rather than any detector's
    quality — the confound that made the pooled dialectic-reviewer number
    describe neither instrument. It is also what lets a structurally-broken
    check surface as one bad detector instead of dragging the layer down.
    """

    def test_each_producer_keeps_its_own_event_type(self):
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[1]
        expected = {
            "scripts/ops/doctor_findings.py": "doctor_check_finding",
            "scripts/ops/deploy_drift_doctor.py": "deploy_drift_finding",
            "scripts/ops/lumen_checkin_doctor.py": "lumen_checkin_finding",
            "scripts/ops/bridge_liveness_watchdog.sh": "bridge_liveness_finding",
        }
        for rel, event_type in expected.items():
            text = (root / rel).read_text()
            assert event_type in text, (
                f"{rel} no longer names {event_type} — if the doctor families "
                "were collapsed to one label, per-detector precision is gone"
            )
