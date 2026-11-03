import json
import pytest
from unittest.mock import patch

from mcp.types import TextContent

from src.services.update_response_service import (
    build_process_update_response_data,
    serialize_process_update_response,
)
from src.state_persistence import (
    StatePersistenceReceipt,
    STATE_PERSISTENCE_UNCONFIRMED_WARNING,
)


def test_build_process_update_response_data_sets_agent_fields():
    payload = build_process_update_response_data(
        result={"status": "ok"},
        agent_id="agent-123",
        identity_assurance={"tier": "strong"},
    )
    assert payload["status"] == "ok"
    assert payload["agent_id"] == "agent-123"
    assert payload["identity_assurance"] == {"tier": "strong"}
    assert payload["eisv_labels"]["V"]["label"] == "Valence"
    assert "positive" in payload["eisv_labels"]["V"]["description"].lower()


def test_serialize_process_update_response_falls_back_on_serialization_error():
    with patch(
        "src.services.update_response_service.json.dumps",
        side_effect=RuntimeError("boom"),
    ):
        result = serialize_process_update_response(
            response_data={"status": "ok"},
            agent_uuid="uuid-123",
            arguments={"lite_response": True},
            fallback_result={
                "status": "ok",
                "decision": {"action": "proceed"},
                "metrics": {"E": 0.1, "I": 0.2, "S": 0.3, "V": 0.4, "coherence": 0.5, "risk_score": 0.6},
            },
        )

    assert isinstance(result[0], TextContent)
    payload = json.loads(result[0].text)
    assert payload["_warning"] == "Response serialization had issues - some fields may be missing"
    assert payload["decision"]["action"] == "proceed"


@pytest.mark.parametrize("state_id", [41, None])
def test_build_uses_typed_acknowledgement_over_mutable_result(state_id):
    receipt = StatePersistenceReceipt(state_id)
    payload = build_process_update_response_data(
        result={"state_persistence": {"status": "recorded", "state_id": 999}},
        agent_id="agent-123", identity_assurance={}, state_persistence=receipt,
        ctx_warnings=["prior warning"],
    )
    assert payload["state_persistence"] == receipt.to_dict()
    assert payload["warnings"][0] == "prior warning"
    assert (STATE_PERSISTENCE_UNCONFIRMED_WARNING in payload["warnings"]) == (state_id is None)


def _serialize(receipt, *, serializer=None, response_data=None, fallback_result=None):
    result = serialize_process_update_response(
        response_data=response_data or {"status": "ok"}, agent_uuid="uuid-123",
        arguments={"lite_response": True},
        fallback_result=fallback_result or {"status": "ok", "decision": {"action": "proceed"}},
        serializer=serializer, state_persistence=receipt,
    )
    assert isinstance(result[0], TextContent)
    return json.loads(result[0].text)


@pytest.mark.parametrize("state_id", [41, None])
def test_production_serializer_cannot_trim_a_late_receipt_or_its_warning(state_id):
    from src.mcp_handlers.response_base import success_response

    receipt = StatePersistenceReceipt(state_id)
    source = {f"field_{i}": i for i in range(105)}
    source["state_persistence"] = StatePersistenceReceipt(999).to_dict()
    payload = _serialize(receipt, serializer=success_response, response_data=source)
    assert any("more keys" in key for key in payload), "Exercise real 100-key truncation"
    assert payload["state_persistence"] == receipt.to_dict()
    assert payload["success"] is True
    if state_id is None:
        assert payload["warnings"] == [STATE_PERSISTENCE_UNCONFIRMED_WARNING]


@pytest.mark.parametrize("state_id", [41, None])
def test_nonraising_production_serializer_error_preserves_error_and_receipt(state_id):
    from src.mcp_handlers.response_base import success_response

    receipt = StatePersistenceReceipt(state_id)
    with patch("src.mcp_handlers.serialization._make_json_serializable", side_effect=ValueError("broken")):
        payload = _serialize(receipt, serializer=success_response)
    assert payload["success"] is False
    assert payload["error"] == "Response serialization failed"
    assert payload["recovery"] == {"action": "Check server logs for details"}
    assert payload["state_persistence"] == receipt.to_dict()
    if state_id is None:
        assert payload["warnings"] == [STATE_PERSISTENCE_UNCONFIRMED_WARNING]


@pytest.mark.parametrize("state_id", [41, None])
@pytest.mark.parametrize("metrics", [{"E": 0.7}, {"E": object()}, None])
def test_raising_serializer_preserves_receipt_even_with_malformed_fallback_metrics(state_id, metrics):
    def broken_serializer(*args, **kwargs):
        raise RuntimeError("broken serializer")

    receipt = StatePersistenceReceipt(state_id)
    payload = _serialize(receipt, serializer=broken_serializer, fallback_result={"metrics": metrics})
    assert payload["success"] is True
    assert payload["state_persistence"] == receipt.to_dict()
    assert "serialization" in payload["_warning"]
    if state_id is None:
        assert payload["warnings"] == [STATE_PERSISTENCE_UNCONFIRMED_WARNING]


@pytest.mark.parametrize("state_id", [41, None])
def test_encoder_independent_fallback_retains_receipt_and_returned_error_status(state_id):
    def error_serializer(*args, **kwargs):
        return [TextContent(type="text", text='{"success":false,"error":"serialization failed"}')]

    receipt = StatePersistenceReceipt(state_id)
    with patch("src.services.update_response_service.json.JSONEncoder", side_effect=RuntimeError("broken encoder")):
        payload = _serialize(receipt, serializer=error_serializer, fallback_result={"metrics": None})
    assert payload["success"] is False
    assert payload["error"] == "Internal serialization error"
    assert payload["state_persistence"] == receipt.to_dict()
    if state_id is None:
        assert payload["warnings"] == [STATE_PERSISTENCE_UNCONFIRMED_WARNING]


def test_no_attempt_does_not_invent_receipt_from_mutable_response():
    source = {"state_persistence": StatePersistenceReceipt(999).to_dict()}
    built = build_process_update_response_data(result=source, agent_id="agent-123", identity_assurance={})
    assert "state_persistence" not in built
    assert "state_persistence" not in _serialize(None, response_data=source)
