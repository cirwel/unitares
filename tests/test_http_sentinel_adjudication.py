"""Tests for the Sentinel adjudication endpoints (dashboard widget backend).

A minimal Starlette app mounts just the two routes; DB helpers and the inline
outcome recorder are patched so no live governance stack is needed. What's
under test: the operator write gate, input validation, idempotency, and that
a verdict produces exactly the outcome args the CLI path would (shared
builder semantics — fp dismissal is the only bad label).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from src.http_api import (
    http_sentinel_adjudicate,
    http_sentinel_adjudication_queue,
)

OP_TOKEN = "test-operator-token"
SENTINEL_UUID = "f92dcea8-4786-412a-a0eb-362c273382f5"
PROGRESS = {"outcomes": 25, "bad": 3, "days": 4, "bad_days": 1, "bad_days_target": 3}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("UNITARES_OPERATOR_TOKENS", OP_TOKEN)
    monkeypatch.delenv("UNITARES_HTTP_API_TOKEN", raising=False)
    app = Starlette(routes=[
        Route("/v1/sentinel/adjudication-queue", http_sentinel_adjudication_queue, methods=["GET"]),
        Route("/v1/sentinel/adjudicate", http_sentinel_adjudicate, methods=["POST"]),
    ])
    return TestClient(app)


def _op_headers():
    return {"X-Unitares-Operator": OP_TOKEN}


# ---------------------------------------------------------------------------
# POST /v1/sentinel/adjudicate
# ---------------------------------------------------------------------------

class TestAdjudicateGate:
    def test_no_operator_header_is_403(self, client):
        r = client.post("/v1/sentinel/adjudicate",
                        json={"fingerprint": "fp1", "status": "confirmed"})
        assert r.status_code == 403
        assert "operator" in r.json()["error"].lower()

    def test_wrong_operator_token_is_403(self, client):
        r = client.post("/v1/sentinel/adjudicate",
                        json={"fingerprint": "fp1", "status": "confirmed"},
                        headers={"X-Unitares-Operator": "not-the-token"})
        assert r.status_code == 403


class TestAdjudicateValidation:
    def test_missing_fingerprint_400(self, client):
        r = client.post("/v1/sentinel/adjudicate",
                        json={"status": "confirmed"}, headers=_op_headers())
        assert r.status_code == 400

    def test_bad_status_400(self, client):
        r = client.post("/v1/sentinel/adjudicate",
                        json={"fingerprint": "fp1", "status": "maybe"},
                        headers=_op_headers())
        assert r.status_code == 400

    def test_dismissal_without_reason_400(self, client):
        r = client.post("/v1/sentinel/adjudicate",
                        json={"fingerprint": "fp1", "status": "dismissed"},
                        headers=_op_headers())
        assert r.status_code == 400
        assert "reason" in r.json()["error"]


class TestAdjudicateRecording:
    def _patches(self, already=frozenset(), uuid=SENTINEL_UUID):
        return (
            patch("src.http_api._adjudicated_sentinel_fingerprints",
                  AsyncMock(return_value=set(already))),
            patch("src.http_api._sentinel_substrate_uuid",
                  AsyncMock(return_value=uuid)),
            patch("src.http_api._adjudication_progress",
                  AsyncMock(return_value=dict(PROGRESS))),
            patch("src.mcp_handlers.observability.outcome_events._record_outcome_event_inline",
                  AsyncMock(return_value={"success": True})),
        )

    def test_fp_dismissal_records_bad_label(self, client):
        p1, p2, p3, rec = self._patches()
        with p1, p2, p3, rec as recorder:
            r = client.post("/v1/sentinel/adjudicate",
                            json={"fingerprint": "fp1", "status": "dismissed", "reason": "fp"},
                            headers=_op_headers())
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is True
        assert body["outcome_type"] == "sentinel_finding_dismissed"
        assert body["is_bad"] is True
        assert body["progress"] == PROGRESS
        args = recorder.call_args[0][0]
        assert args["agent_id"] == SENTINEL_UUID
        assert args["verification_source"] == "external_signal"
        assert args["detail"]["fingerprint"] == "fp1"
        assert args["detail"]["adjudicated_via"] == "dashboard"

    def test_confirmation_is_good_label(self, client):
        p1, p2, p3, rec = self._patches()
        with p1, p2, p3, rec as recorder:
            r = client.post("/v1/sentinel/adjudicate",
                            json={"fingerprint": "fp2", "status": "confirmed"},
                            headers=_op_headers())
        assert r.status_code == 200
        assert r.json()["outcome_type"] == "sentinel_finding_confirmed"
        assert r.json()["is_bad"] is False
        assert recorder.call_args[0][0]["is_bad"] is False

    def test_non_fp_dismissal_is_not_bad(self, client):
        p1, p2, p3, rec = self._patches()
        with p1, p2, p3, rec:
            r = client.post("/v1/sentinel/adjudicate",
                            json={"fingerprint": "fp3", "status": "dismissed",
                                  "reason": "out_of_scope"},
                            headers=_op_headers())
        assert r.status_code == 200
        assert r.json()["is_bad"] is False

    def test_double_adjudication_409(self, client):
        p1, p2, p3, rec = self._patches(already={"fp1"})
        with p1, p2, p3, rec as recorder:
            r = client.post("/v1/sentinel/adjudicate",
                            json={"fingerprint": "fp1", "status": "confirmed"},
                            headers=_op_headers())
        assert r.status_code == 409
        recorder.assert_not_called()

    def test_missing_substrate_claim_503(self, client):
        p1, p2, p3, rec = self._patches(uuid=None)
        with p1, p2, p3, rec as recorder:
            r = client.post("/v1/sentinel/adjudicate",
                            json={"fingerprint": "fp1", "status": "confirmed"},
                            headers=_op_headers())
        assert r.status_code == 503
        recorder.assert_not_called()


# ---------------------------------------------------------------------------
# GET /v1/sentinel/adjudication-queue
# ---------------------------------------------------------------------------

def _event(fp, severity="high", msg="m", ts="2026-07-01T00:00:00+00:00"):
    return {"timestamp": ts, "agent_id": "sentinel", "event_id": fp + "-ev",
            "details": {"severity": severity, "finding_type": "ad_hoc",
                        "message": msg, "fingerprint": fp, "agent_name": "Sentinel"}}


class TestAdjudicationQueue:
    def test_filters_adjudicated_dedupes_and_counts(self, client):
        events = [
            _event("fp-a"),                      # pending
            _event("fp-a"),                      # duplicate fingerprint -> deduped
            _event("fp-done"),                   # already adjudicated -> excluded
            _event("fp-b", severity="critical"), # pending
            _event("fp-low", severity="low"),    # below default severities -> excluded
            _event("fp-x") | {"details": {"severity": "high", "message": "no fp"}},  # no fingerprint
        ]
        with patch("src.audit_db.query_audit_events_async",
                   AsyncMock(return_value=events)), \
             patch("src.http_api._adjudicated_sentinel_fingerprints",
                   AsyncMock(return_value={"fp-done"})), \
             patch("src.http_api._adjudication_progress",
                   AsyncMock(return_value=dict(PROGRESS))):
            r = client.get("/v1/sentinel/adjudication-queue?limit=5")
        assert r.status_code == 200
        body = r.json()
        fps = [q["fingerprint"] for q in body["queue"]]
        assert fps == ["fp-a", "fp-b"]
        assert body["pending_total"] == 2
        assert body["progress"] == PROGRESS
        assert "fp" in body["dismiss_reasons"]

    def test_limit_caps_queue_but_not_pending_total(self, client):
        events = [_event(f"fp-{i}") for i in range(8)]
        with patch("src.audit_db.query_audit_events_async",
                   AsyncMock(return_value=events)), \
             patch("src.http_api._adjudicated_sentinel_fingerprints",
                   AsyncMock(return_value=set())), \
             patch("src.http_api._adjudication_progress",
                   AsyncMock(return_value=dict(PROGRESS))):
            r = client.get("/v1/sentinel/adjudication-queue?limit=3")
        body = r.json()
        assert len(body["queue"]) == 3
        assert body["pending_total"] == 8


# ---------------------------------------------------------------------------
# Forced-release event-check enrichment (bridge-dispatch proposal §4, PR #1450)
# ---------------------------------------------------------------------------

from decimal import Decimal  # noqa: E402

from src.http_api import (  # noqa: E402
    _assess_forced_release_row,
    _finding_report_latency_s,
)

LEASE_ID = "17546f52-370d-4274-9d5b-0c233f09590c"
LEASE_ID_2 = "a2d89680-0f3a-48e5-b51a-50684f70bfd9"


def _lease_row(**over):
    row = {
        "lease_id": LEASE_ID,
        "surface_id": "resident:/sentinel_cycle",
        "release_reason": "forced",
        "holder_kind": "local_beam",
        "holder_pid_null": True,
        "original_ttl_s": 300,
        # asyncpg returns EXTRACT(epoch ...) as Decimal on the live driver —
        # mirror that so the float() coercion stays covered.
        "held_s": Decimal("4350.0"),  # 14.5x TTL
    }
    row.update(over)
    return row


class TestForcedReleaseAssessment:
    def test_missing_row_is_lease_plane_integrity_state(self):
        ev = _assess_forced_release_row(None, "resident:/sentinel_cycle")
        assert ev["assessment"] == "no_lease_row"

    def test_matching_row_is_event_recorded_with_facts(self):
        ev = _assess_forced_release_row(_lease_row(), "resident:/sentinel_cycle")
        assert ev["assessment"] == "event_recorded"
        assert ev["held_x_ttl"] == 14.5
        assert ev["holder_pid_null"] is True

    def test_surface_mismatch_is_lookup_mismatch_not_contradiction(self):
        ev = _assess_forced_release_row(_lease_row(), "resident:/steward")
        assert ev["assessment"] == "lookup_mismatch"
        assert ev["surface_match"] is False

    def test_unforced_release_reason_is_lookup_mismatch(self):
        ev = _assess_forced_release_row(
            _lease_row(release_reason="normal"), "resident:/sentinel_cycle")
        assert ev["assessment"] == "lookup_mismatch"

    def test_null_held_s_yields_no_ratio(self):
        ev = _assess_forced_release_row(
            _lease_row(held_s=None), "resident:/sentinel_cycle")
        assert ev["assessment"] == "event_recorded"
        assert ev["held_x_ttl"] is None


class TestReportLatency:
    def test_z_suffix_and_offset_formats_both_parse(self):
        s = _finding_report_latency_s(
            "2026-07-31T13:16:47.795293+00:00", "2026-07-31T05:53:46Z")
        assert s is not None and 26580 < s < 26582  # the documented 7.4h lag

    def test_unparseable_inputs_yield_none(self):
        assert _finding_report_latency_s(None, "2026-07-31T05:53:46Z") is None
        assert _finding_report_latency_s("not-a-ts", "also-not") is None

    def test_clock_skew_clamps_to_zero(self):
        assert _finding_report_latency_s(
            "2026-07-31T05:00:00Z", "2026-07-31T06:00:00Z") == 0.0


def _fr_event(fp, lease_id=LEASE_ID, surface="resident:/sentinel_cycle",
              event_ts="2026-07-31T21:08:05Z", ts="2026-07-31T21:08:31+00:00"):
    e = _event(fp, msg=f"forced release: {surface} (lease {lease_id})", ts=ts)
    e["details"].update({"lease_id": lease_id, "surface_id": surface, "ts": event_ts})
    return e


class TestQueueEvidenceEnrichment:
    def _get_queue(self, client, events, lease_rows):
        with patch("src.audit_db.query_audit_events_async",
                   AsyncMock(return_value=events)), \
             patch("src.http_api._adjudicated_sentinel_fingerprints",
                   AsyncMock(return_value=set())), \
             patch("src.http_api._adjudication_progress",
                   AsyncMock(return_value=dict(PROGRESS))), \
             patch("src.http_api._fetch_lease_rows", lease_rows):
            return client.get("/v1/sentinel/adjudication-queue?limit=5")

    def test_evidence_attached_only_to_forced_release_findings(self, client):
        events = [_fr_event("fp-fr"), _event("fp-plain")]
        r = self._get_queue(
            client, events, AsyncMock(return_value={LEASE_ID: _lease_row()}))
        assert r.status_code == 200
        q = r.json()["queue"]
        assert q[0]["evidence"]["assessment"] == "event_recorded"
        assert q[0]["evidence"]["report_latency_s"] == 26.0
        assert "evidence" not in q[1]

    def test_malformed_lease_id_cannot_poison_the_batch(self, client):
        # 36 hyphens satisfies a loose shape check but not the uuid cast; it
        # must degrade only its own finding, never the whole page.
        events = [_fr_event("fp-bad", lease_id="-" * 36),
                  _fr_event("fp-good", lease_id=LEASE_ID)]
        fetch = AsyncMock(return_value={LEASE_ID: _lease_row()})
        r = self._get_queue(client, events, fetch)
        q = r.json()["queue"]
        assert q[0]["evidence"]["assessment"] == "lookup_mismatch"
        assert q[1]["evidence"]["assessment"] == "event_recorded"
        (called_ids,) = fetch.call_args.args
        assert called_ids == [LEASE_ID]  # malformed id never reaches SQL

    def test_missing_lease_id_key_is_lookup_mismatch(self, client):
        e = _fr_event("fp-nolid")
        del e["details"]["lease_id"]
        r = self._get_queue(client, [e], AsyncMock(return_value={}))
        ev = r.json()["queue"][0]["evidence"]
        assert ev["assessment"] == "lookup_mismatch"
        assert "no lease id" in ev.get("note", "")

    def test_fetch_failure_is_explicit_check_error_not_silence(self, client):
        events = [_fr_event("fp-fr")]
        r = self._get_queue(
            client, events, AsyncMock(side_effect=RuntimeError("db down")))
        assert r.status_code == 200
        q = r.json()["queue"]
        assert len(q) == 1
        assert q[0]["evidence"]["assessment"] == "check_error"
