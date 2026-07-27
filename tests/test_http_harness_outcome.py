"""Tests for the harness outcome endpoint (POST /v1/harness/outcome, #1345).

A minimal Starlette app mounts just the route; the inline outcome recorder is
patched so no live governance stack is needed. What's under test: the operator
write gate, input validation, that attribution and provenance reach the
recorder exactly as given, and that the detail row is stamped with the
delivery-path breadcrumb.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from src.http_api import http_harness_outcome

OP_TOKEN = "test-operator-token"
AGENT_UUID = "3a7f2c91-5b64-4e08-9d13-8c2f6a4e7b50"

RECORDER_OK = {
    "outcome_id": 42,
    "outcome_type": "test_passed",
    "is_bad": False,
    "eisv_snapshot": {"primary_eisv": {}},
    "corroboration_grade": "externally_verified",
    "evidence_weight": 1.0,
}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("UNITARES_OPERATOR_TOKENS", OP_TOKEN)
    monkeypatch.delenv("UNITARES_HTTP_API_TOKEN", raising=False)
    app = Starlette(routes=[
        Route("/v1/harness/outcome", http_harness_outcome, methods=["POST"]),
    ])
    return TestClient(app)


def _op_headers():
    return {"X-Unitares-Operator": OP_TOKEN}


def _body(**overrides):
    body = {
        "agent_uuid": AGENT_UUID,
        "outcome_type": "test_passed",
        "verification_source": "external_signal",
        "detail": {"passed": 12, "failed": 0, "command": "./scripts/dev/test-cache.sh"},
    }
    body.update(overrides)
    return body


def _recorder(payload=None):
    return patch(
        "src.mcp_handlers.observability.outcome_events._record_outcome_event_inline",
        AsyncMock(return_value=dict(payload if payload is not None else RECORDER_OK)),
    )


class TestGate:
    def test_no_operator_header_is_403(self, client):
        r = client.post("/v1/harness/outcome", json=_body())
        assert r.status_code == 403
        assert "operator" in r.json()["error"].lower()

    def test_wrong_operator_token_is_403(self, client):
        r = client.post("/v1/harness/outcome", json=_body(),
                        headers={"X-Unitares-Operator": "not-the-token"})
        assert r.status_code == 403


class TestValidation:
    def test_invalid_json_400(self, client):
        r = client.post("/v1/harness/outcome", content=b"not json",
                        headers={**_op_headers(), "Content-Type": "application/json"})
        assert r.status_code == 400

    def test_missing_agent_uuid_400(self, client):
        r = client.post("/v1/harness/outcome",
                        json=_body(agent_uuid=""), headers=_op_headers())
        assert r.status_code == 400
        assert "agent_uuid" in r.json()["error"]

    def test_malformed_agent_uuid_400(self, client):
        r = client.post("/v1/harness/outcome",
                        json=_body(agent_uuid="agent-3a7f2c91"), headers=_op_headers())
        assert r.status_code == 400
        assert "UUID" in r.json()["error"]

    def test_unknown_outcome_type_400(self, client):
        r = client.post("/v1/harness/outcome",
                        json=_body(outcome_type="vibes_good"), headers=_op_headers())
        assert r.status_code == 400
        assert "outcome_type" in r.json()["error"]

    def test_unknown_verification_source_400(self, client):
        r = client.post("/v1/harness/outcome",
                        json=_body(verification_source="trust_me"), headers=_op_headers())
        assert r.status_code == 400
        assert "verification_source" in r.json()["error"]

    def test_non_object_detail_400(self, client):
        r = client.post("/v1/harness/outcome",
                        json=_body(detail=["not", "a", "dict"]), headers=_op_headers())
        assert r.status_code == 400
        assert "detail" in r.json()["error"]

    def test_confidence_out_of_range_400(self, client):
        r = client.post("/v1/harness/outcome",
                        json=_body(confidence=1.7), headers=_op_headers())
        assert r.status_code == 400
        assert "confidence" in r.json()["error"]

    def test_non_numeric_confidence_400(self, client):
        r = client.post("/v1/harness/outcome",
                        json=_body(confidence="high"), headers=_op_headers())
        assert r.status_code == 400


class TestRecording:
    def test_happy_path_passes_attribution_and_provenance(self, client):
        with _recorder() as recorder:
            r = client.post("/v1/harness/outcome", json=_body(), headers=_op_headers())
        assert r.status_code == 200
        payload = r.json()
        assert payload["success"] is True
        assert payload["outcome_id"] == 42
        assert payload["agent_uuid"] == AGENT_UUID
        assert payload["evidence_weight"] == 1.0
        assert payload["agent_state_found"] is True

        args = recorder.call_args.args[0]
        assert args["agent_id"] == AGENT_UUID
        assert args["outcome_type"] == "test_passed"
        assert args["verification_source"] == "external_signal"
        assert args["detail"]["recorded_via"] == "harness_outcome_endpoint"
        assert args["detail"]["passed"] == 12

    def test_optional_fields_forwarded(self, client):
        with _recorder() as recorder:
            r = client.post(
                "/v1/harness/outcome",
                json=_body(confidence=0.8, is_bad=False, outcome_score=1.0,
                           session_id="agent-3a7f2c91-5b6"),
                headers=_op_headers(),
            )
        assert r.status_code == 200
        args = recorder.call_args.args[0]
        assert args["confidence"] == 0.8
        assert args["is_bad"] is False
        assert args["outcome_score"] == 1.0
        assert args["session_id"] == "agent-3a7f2c91-5b6"

    def test_optional_fields_omitted_stay_absent(self, client):
        with _recorder() as recorder:
            client.post("/v1/harness/outcome", json=_body(), headers=_op_headers())
        args = recorder.call_args.args[0]
        for key in ("confidence", "is_bad", "outcome_score", "session_id"):
            assert key not in args

    def test_default_verification_source_is_schema_default(self, client):
        body = _body()
        del body["verification_source"]
        with _recorder() as recorder:
            r = client.post("/v1/harness/outcome", json=body, headers=_op_headers())
        assert r.status_code == 200
        assert recorder.call_args.args[0]["verification_source"] == "agent_reported_tool_result"

    def test_missing_agent_state_reported_not_fatal(self, client):
        payload = dict(RECORDER_OK, eisv_snapshot=None)
        with _recorder(payload):
            r = client.post("/v1/harness/outcome", json=_body(), headers=_op_headers())
        assert r.status_code == 200
        assert r.json()["agent_state_found"] is False

    def test_recorder_error_is_500(self, client):
        with _recorder({"error": "Failed to record outcome event (database error)"}):
            r = client.post("/v1/harness/outcome", json=_body(), headers=_op_headers())
        assert r.status_code == 500
        assert r.json()["success"] is False
