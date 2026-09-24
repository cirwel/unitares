"""Tests for POST /v1/sentinel/model-adjudicate and its effect on the queue.

A model verdict is telemetry, never an operator label. The load-bearing
assertions are the isolation ones: nothing reaches outcome_events, the event
type cannot be read back as a finding or as an adjudication outcome, and an
operator verdict always outranks a model's.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

import src.http_routes.sentinel as sentinel_routes
from src.http_api import (
    http_sentinel_adjudication_queue,
    http_sentinel_model_adjudicate,
)

READ_TOKEN = "test-dashboard-read-token"
PROGRESS = {"outcomes": 0, "bad": 0, "days": 0, "bad_days": 0, "bad_days_target": 3}
MODEL = {"backend": "codex", "host_id": "codex:host-adapter", "model": "m-fast", "tier": "fast"}


ADJ_TOKEN = "test-adjudicator-token"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("UNITARES_HTTP_API_TOKEN", READ_TOKEN)
    monkeypatch.setenv("UNITARES_MODEL_ADJUDICATOR_TOKEN", ADJ_TOKEN)
    app = Starlette(routes=[
        Route("/v1/sentinel/adjudication-queue", http_sentinel_adjudication_queue, methods=["GET"]),
        Route("/v1/sentinel/model-adjudicate", http_sentinel_model_adjudicate, methods=["POST"]),
    ])
    return TestClient(app, headers={"Authorization": f"Bearer {READ_TOKEN}"})


def _db(appended):
    return type("DB", (), {"append_audit_event": appended})()


def _patches(already=frozenset(), event_type="doctor_check_finding"):
    return (
        patch("src.http_routes.sentinel._adjudicated_sentinel_fingerprints",
              AsyncMock(return_value=set(already))),
        patch("src.http_routes.sentinel._finding_producer_uuid",
              AsyncMock(return_value=(None, "doctor-findings", event_type))),
        patch("src.mcp_handlers.observability.outcome_events._record_outcome_event_inline",
              AsyncMock(return_value={"success": True})),
    )


def _post(client, headers=None, **body):
    payload = {"fingerprint": "fp1", "verdict": "confirmed", "model": MODEL}
    payload.update(body)
    return client.post("/v1/sentinel/model-adjudicate", json=payload,
                       headers={"X-Unitares-Adjudicator": ADJ_TOKEN} if headers is None
                       else headers)


class TestModelAdjudicateIsolation:
    def test_writes_no_outcome_event(self, client):
        adjudicated, producer, recorder = _patches()
        appended = AsyncMock(return_value=True)
        with adjudicated, producer, recorder as rec, \
                patch("src.db.get_db", return_value=_db(appended)):
            r = _post(client, verdict="dismissed", reason="fp")
        assert r.status_code == 200
        assert r.json()["recorded_outcome"] is False
        rec.assert_not_awaited()

    def test_records_a_labelled_telemetry_event_naming_the_model(self, client):
        adjudicated, producer, recorder = _patches()
        appended = AsyncMock(return_value=True)
        with adjudicated, producer, recorder, \
                patch("src.db.get_db", return_value=_db(appended)):
            _post(client, rationale="checked the log", confidence=0.9)
        event = appended.call_args[0][0]
        assert event.event_type == sentinel_routes._MODEL_ADJUDICATION_EVENT_TYPE
        assert event.payload["verdict"] == "confirmed"
        assert event.payload["model"] == MODEL
        assert event.payload["finding_event_type"] == "doctor_check_finding"
        assert "NOT an operator adjudication" in event.payload["note"]

    def test_event_type_cannot_reach_the_queue_or_the_anchor_channel(self):
        t = sentinel_routes._MODEL_ADJUDICATION_EVENT_TYPE
        assert t not in sentinel_routes._SENTINEL_FINDING_EVENT_TYPES
        assert t not in sentinel_routes._SENTINEL_ADJUDICATION_OUTCOME_TYPES
        assert not t.endswith(("_confirmed", "_dismissed", "_finding"))

    def test_operator_verdict_outranks_the_model(self, client):
        adjudicated, producer, recorder = _patches(already={"fp1"})
        appended = AsyncMock(return_value=True)
        with adjudicated, producer, recorder, \
                patch("src.db.get_db", return_value=_db(appended)):
            r = _post(client)
        assert r.status_code == 409
        appended.assert_not_awaited()

    def test_non_queue_fingerprint_is_refused(self, client):
        adjudicated, producer, recorder = _patches(event_type="watcher_finding")
        appended = AsyncMock(return_value=True)
        with adjudicated, producer, recorder, \
                patch("src.db.get_db", return_value=_db(appended)):
            r = _post(client)
        assert r.status_code == 404
        appended.assert_not_awaited()


    def test_a_failed_insert_is_not_reported_as_success(self, client):
        """append_audit_event swallows DB errors and returns False."""
        adjudicated, producer, recorder = _patches()
        with adjudicated, producer, recorder, \
                patch("src.db.get_db", return_value=_db(AsyncMock(return_value=False))):
            r = _post(client)
        assert r.status_code == 500
        assert r.json()["success"] is False


class TestModelAdjudicateValidation:
    def test_needs_bearer_auth(self, client):
        r = client.post("/v1/sentinel/model-adjudicate",
                        json={"fingerprint": "fp1", "verdict": "confirmed", "model": MODEL},
                        headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401

    def test_unknown_verdict_400(self, client):
        assert _post(client, verdict="maybe").status_code == 400

    def test_dismissal_needs_a_reason(self, client):
        r = _post(client, verdict="dismissed")
        assert r.status_code == 400
        assert "reason" in r.json()["error"]

    def test_unattributed_model_verdict_is_refused(self, client):
        assert _post(client, model={"model": "x"}).status_code == 400
        assert _post(client, model=None).status_code == 400

    def test_non_numeric_confidence_400(self, client):
        assert _post(client, confidence="high").status_code == 400

    @pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity"])
    def test_non_finite_confidence_400(self, client, raw):
        """json accepts these and a clamp would turn them into 1.0."""
        r = client.post(
            "/v1/sentinel/model-adjudicate",
            content=('{"fingerprint": "fp1", "verdict": "confirmed", '
                     '"model": {"backend": "claude"}, "confidence": %s}' % raw),
            headers={"X-Unitares-Adjudicator": ADJ_TOKEN,
                     "Content-Type": "application/json"},
        )
        assert r.status_code == 400
        assert "finite" in r.json()["error"]


class TestAdjudicatorCredential:
    """A model verdict hides a finding from the operator queue, so generic
    client auth is not enough to write one."""

    def test_missing_adjudicator_header_is_403(self, client):
        adjudicated, producer, recorder = _patches()
        appended = AsyncMock(return_value=True)
        with adjudicated, producer, recorder, \
                patch("src.db.get_db", return_value=_db(appended)):
            r = _post(client, headers={})
        assert r.status_code == 403
        appended.assert_not_awaited()

    def test_wrong_adjudicator_token_is_403(self, client):
        r = _post(client, headers={"X-Unitares-Adjudicator": "guess"})
        assert r.status_code == 403

    def test_route_is_off_when_the_server_has_no_token(self, client, monkeypatch):
        monkeypatch.delenv("UNITARES_MODEL_ADJUDICATOR_TOKEN")
        r = _post(client)
        assert r.status_code == 503
        assert "UNITARES_MODEL_ADJUDICATOR_TOKEN" in r.json()["error"]

    def test_abstain_needs_no_reason_and_suppresses_nothing(self, client):
        adjudicated, producer, recorder = _patches()
        appended = AsyncMock(return_value=True)
        with adjudicated, producer, recorder, \
                patch("src.db.get_db", return_value=_db(appended)):
            r = _post(client, verdict="abstain")
        assert r.status_code == 200
        assert r.json()["suppressed_for_hours"] == 0


def _event(fp, severity="warning"):
    return {"timestamp": "2026-09-01T00:00:00+00:00", "agent_id": "doctor-findings",
            "event_type": "doctor_check_finding", "event_id": fp + "-ev",
            "details": {"severity": severity, "message": "m", "fingerprint": fp}}


class TestQueueSuppression:
    def _get(self, client, model_verdicts, query=""):
        events = [_event("fp-open"), _event("fp-confirmed"), _event("fp-dismissed"),
                  _event("fp-abstained")]
        with patch("src.audit_db.query_audit_events_async", AsyncMock(return_value=events)), \
             patch("src.http_routes.sentinel._adjudicated_sentinel_fingerprints",
                   AsyncMock(return_value=set())), \
             patch("src.http_routes.sentinel._abstained_sentinel_fingerprints",
                   AsyncMock(return_value=set())), \
             patch("src.http_routes.sentinel._model_adjudicated_fingerprints",
                   AsyncMock(return_value=model_verdicts)), \
             patch("src.http_routes.sentinel._adjudication_progress",
                   AsyncMock(return_value=dict(PROGRESS))):
            return client.get("/v1/sentinel/adjudication-queue?limit=10" + query).json()

    VERDICTS = {"fp-confirmed": "confirmed", "fp-dismissed": "dismissed",
                "fp-abstained": "abstain"}

    def test_queue_items_carry_structured_provenance(self, client):
        """So consumers never infer the producer or check from message prose."""
        body = self._get(client, {})
        item = body["queue"][0]
        assert item["event_type"] == "doctor_check_finding"
        assert "check" in item

    def test_model_verdicts_leave_the_queue_and_are_counted(self, client):
        body = self._get(client, self.VERDICTS)
        fps = [q["fingerprint"] for q in body["queue"]]
        # A model abstention stays visible to whoever else might judge it.
        assert fps == ["fp-open", "fp-abstained"]
        assert body["model_adjudicated_suppressed"] == 2

    def test_the_adjudicator_can_skip_what_it_already_declined(self, client):
        body = self._get(client, self.VERDICTS, "&exclude_model_abstained=1")
        assert [q["fingerprint"] for q in body["queue"]] == ["fp-open"]
        assert body["model_adjudicated_suppressed"] == 3


def test_dashboard_pane_surfaces_hidden_items():
    """The queue's only consumer must show what model verdicts hid, or an
    emptied queue reads "Queue clear" while findings are merely out of view."""
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent
          / "dashboard" / "redesign" / "sections" / "adjudication.js").read_text()
    assert "model_adjudicated_suppressed" in js
    assert "abstained_suppressed" in js
    assert "the\n          queue is not clear" in js or "queue is not clear" in js
