"""#1351 hardening: ephemeral-write gate + token-invalid warning on successful calls.

Follow-up to #1319 (fixed by #1325). Two defenses:

1. The post-execution ``apply_identity_warnings`` step surfaces a
   ``continuity_token_invalid`` warning on ANY successful response when the
   request presented a token that failed verification but succeeded via a
   fallback proof — previously the caller learned nothing until the fallback
   rotated.

2. ``outcome_event`` consults the middleware's ``ephemeral/persisted`` stamp:
   strict identity refuses the durable write fail-closed (that route should
   not exist); non-strict stamps the row ``ephemeral_writer=true`` and warns.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from mcp.types import TextContent

from src.mcp_handlers.context import (
    clear_continuity_token_invalid,
    get_continuity_token_invalid,
    mark_continuity_token_invalid,
    set_session_context,
    set_session_resolution_source,
)
from src.mcp_handlers.middleware.identity_warning_step import apply_identity_warnings


@pytest.fixture(autouse=True)
def _clean_flag():
    clear_continuity_token_invalid()
    yield
    clear_continuity_token_invalid()


def _result(payload) -> list:
    return [TextContent(type="text", text=json.dumps(payload))]


def _ctx():
    return SimpleNamespace(original_name=None)


class TestContinuityTokenInvalidFlag:
    def test_mark_and_clear(self):
        assert get_continuity_token_invalid() is False
        mark_continuity_token_invalid()
        assert get_continuity_token_invalid() is True
        clear_continuity_token_invalid()
        assert get_continuity_token_invalid() is False

    @pytest.mark.asyncio
    async def test_derive_session_key_marks_flag_on_invalid_token(self):
        """The invalid-token branch in derive_session_key sets the sticky flag
        even when a fallback proof wins afterwards."""
        from src.mcp_handlers.identity.session import derive_session_key

        with patch(
            "src.mcp_handlers.identity.session.resolve_continuity_token",
            return_value=None,
        ):
            resolved = await derive_session_key(
                signals=None,
                arguments={
                    "continuity_token": "v1.garbage",
                    "client_session_id": "agent-00000000-feed-4bad-8888-000000000001",
                },
            )
        # Fallback proof won (explicit client_session_id)...
        assert resolved == "agent-00000000-feed-4bad-8888-000000000001"
        # ...but the invalid-token fact survived.
        assert get_continuity_token_invalid() is True


class TestIdentityWarningStep:
    @pytest.mark.asyncio
    async def test_appends_warning_on_success_when_flag_set(self):
        mark_continuity_token_invalid()
        set_session_resolution_source("explicit_client_session_id")
        result = await apply_identity_warnings(
            "get_state", {}, _ctx(), _result({"success": True, "data": 1})
        )
        payload = json.loads(result[0].text)
        (warning,) = payload["identity_warnings"]
        assert warning["code"] == "continuity_token_invalid"
        assert warning["resolved_via"] == "explicit_client_session_id"
        assert payload["data"] == 1  # payload otherwise untouched

    @pytest.mark.asyncio
    async def test_no_warning_when_flag_clear(self):
        result = await apply_identity_warnings(
            "get_state", {}, _ctx(), _result({"success": True})
        )
        assert "identity_warnings" not in json.loads(result[0].text)

    @pytest.mark.asyncio
    async def test_error_responses_pass_through_untouched(self):
        mark_continuity_token_invalid()
        original = _result({"success": False, "error": "nope"})
        result = await apply_identity_warnings("get_state", {}, _ctx(), original)
        assert result is original

    @pytest.mark.asyncio
    async def test_non_json_result_passes_through(self):
        mark_continuity_token_invalid()
        original = [TextContent(type="text", text="not json {")]
        result = await apply_identity_warnings("get_state", {}, _ctx(), original)
        assert result is original

    @pytest.mark.asyncio
    async def test_does_not_duplicate_existing_warning(self):
        mark_continuity_token_invalid()
        payload = {
            "success": True,
            "identity_warnings": [{"code": "continuity_token_invalid"}],
        }
        result = await apply_identity_warnings("get_state", {}, _ctx(), _result(payload))
        assert len(json.loads(result[0].text)["identity_warnings"]) == 1

    def test_registered_after_envelope_step(self):
        from src.mcp_handlers.middleware import POST_EXECUTION_STEPS
        from src.mcp_handlers.middleware.envelope_step import apply_experience_envelope

        names = [s.__name__ for s in POST_EXECUTION_STEPS]
        assert names.index("apply_identity_warnings") > names.index(
            apply_experience_envelope.__name__
        )


class TestEphemeralWriteGate:
    """outcome_event consults the middleware ephemeral/persisted stamp."""

    def _set_ephemeral_context(self):
        return set_session_context(
            session_key="sk-test",
            agent_id="00000000-dead-4bee-8888-000000000002",
            identity_result={"ephemeral": True, "persisted": False, "created": True},
        )

    @pytest.mark.asyncio
    async def test_strict_refuses_ephemeral_write(self):
        from src.mcp_handlers.observability.outcome_events import handle_outcome_event

        self._set_ephemeral_context()
        with patch(
            "src.mcp_handlers.identity_bootstrap.is_strict_identity_required",
            return_value=True,
        ):
            result = await handle_outcome_event({"outcome_type": "test_passed"})
        payload = json.loads(result[0].text)
        assert payload.get("success") is False or "error" in payload
        assert payload.get("error_code") == "EPHEMERAL_IDENTITY_WRITE_REFUSED"

    @pytest.mark.asyncio
    async def test_non_strict_stamps_and_warns(self):
        from src.mcp_handlers.observability import outcome_events

        self._set_ephemeral_context()
        captured = {}

        async def _fake_inline(args):
            captured.update(args)
            return {"recorded": True}

        with patch(
            "src.mcp_handlers.identity_bootstrap.is_strict_identity_required",
            return_value=False,
        ), patch.object(
            outcome_events, "_record_outcome_event_inline", _fake_inline
        ):
            result = await outcome_events.handle_outcome_event(
                {"outcome_type": "test_passed"}
            )
        assert captured["detail"]["ephemeral_writer"] is True
        payload = json.loads(result[0].text)
        codes = [w["code"] for w in payload.get("identity_warnings", [])]
        assert "ephemeral_writer" in codes

    @pytest.mark.asyncio
    async def test_persisted_identity_writes_unaffected(self):
        from src.mcp_handlers.observability import outcome_events

        set_session_context(
            session_key="sk-test",
            agent_id="00000000-dead-4bee-8888-000000000003",
            identity_result={"persisted": True},
        )
        captured = {}

        async def _fake_inline(args):
            captured.update(args)
            return {"recorded": True}

        with patch.object(
            outcome_events, "_record_outcome_event_inline", _fake_inline
        ):
            result = await outcome_events.handle_outcome_event(
                {"outcome_type": "test_passed"}
            )
        assert "ephemeral_writer" not in (captured.get("detail") or {})
        payload = json.loads(result[0].text)
        assert "identity_warnings" not in payload
