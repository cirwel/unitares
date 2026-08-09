import json
from unittest.mock import patch

from mcp.types import TextContent

from src.services.update_response_service import (
    build_process_update_response_data,
    serialize_process_update_response,
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
