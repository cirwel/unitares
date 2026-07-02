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
