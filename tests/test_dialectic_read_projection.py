"""Read projection must preserve protocol meaning and cached source records."""

import copy

import pytest
from pydantic import ValidationError

from src.mcp_handlers.dialectic.read_projection import compact_session_read
from src.mcp_handlers.schemas.dialectic import (
    DialecticParams,
    GetDialecticSessionParams,
    ListDialecticSessionsParams,
)


@pytest.mark.parametrize("model", [DialecticParams, GetDialecticSessionParams, ListDialecticSessionsParams])
def test_projection_is_validated_and_advertised(model):
    assert model.model_validate({"fields": "compact"}).fields == "compact"
    assert "fields" in model.model_json_schema()["properties"]
    with pytest.raises(ValidationError):
        model.model_validate({"fields": "typo"})
    assert "fields" in DialecticParams.ACTION_FIELDS["get"]
    assert "fields" in DialecticParams.ACTION_FIELDS["list"]


def test_timeout_envelope_keeps_failure_and_post_transition_state():
    payload = {
        "success": False,
        "error": "Session timeout",
        "recovery": {"action": "Read the timeout reason."},
        "session": {
            "session_id": "timed-out-session",
            "phase": "failed",
            "awaiting_facilitation": False,
            "transcript_revision": 7,
            "transcript": [{"reasoning": "A long historical message."}],
            "resolution": {"reason": "Session timeout", "closed_by": "get_timeout_check"},
        },
    }
    original = copy.deepcopy(payload)
    compact = compact_session_read(payload)
    assert compact["success"] is False
    assert compact["error"] == payload["error"]
    assert compact["recovery"] == payload["recovery"]
    assert compact["session"]["phase"] == "failed"
    assert compact["session"]["resolution"] == payload["session"]["resolution"]
    assert compact["session"]["transcript_revision"] == 7
    assert "transcript" not in compact["session"]
    assert payload == original


def test_legacy_row_keeps_null_verdict_and_previews_unicode_reason():
    payload = {"session_id": "legacy", "resolution": None, "reason": "理由" * 500}
    compact = compact_session_read(payload)
    assert compact["resolution"] is None
    assert compact["reason_truncated"] is True
    assert compact["reason_chars"] == 1000
    assert compact["reason"] == payload["reason"][:280] + "…"
    assert compact["omitted_fields"] == []
