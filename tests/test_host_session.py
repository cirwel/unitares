"""The declared vendor-session record: validation and its served form."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.identity.host_session import (
    HOST_SESSION_SCHEMA,
    host_session_summary,
    normalize_host_session,
)

NOW = datetime(2026, 10, 26, 6, 0, tzinfo=timezone.utc)


def test_full_declaration_is_stored_with_schema_time_and_source():
    record = normalize_host_session(
        {"harness": "codex", "session_id": "0199a-b2", "workspace": "/srv/work"},
        now=NOW,
    )
    assert record == {
        "schema": HOST_SESSION_SCHEMA,
        "harness": "codex",
        "session_id": "0199a-b2",
        "workspace": "/srv/work",
        "recorded_at": NOW.isoformat(),
        "source": "caller_declared",
    }


def test_workspace_is_optional_and_windows_paths_are_absolute():
    assert "workspace" not in normalize_host_session({"harness": "claude-code", "session_id": "s1"})
    record = normalize_host_session(
        {"harness": "claude-code", "session_id": "s1", "workspace": "C:\\work\\repo"}
    )
    assert record["workspace"] == "C:\\work\\repo"


def test_nothing_declared_records_nothing():
    assert normalize_host_session(None) is None


@pytest.mark.parametrize(
    "declared",
    [
        "claude-code:s1",
        {"harness": "claude-code"},
        {"session_id": "s1"},
        {"harness": "claude code", "session_id": "s1"},
        {"harness": "claude-code", "session_id": "s1; rm -rf /"},
        {"harness": "claude-code", "session_id": "s" * 129},
        {"harness": "claude-code", "session_id": "s1", "workspace": "relative/path"},
        {"harness": "claude-code", "session_id": "s1", "workspace": "/srv/a\nb"},
        {"harness": "claude-code", "session_id": "s1", "workspace": "/" + "a" * 1024},
        {"harness": "claude-code", "session_id": "s1", "token": "x"},
    ],
)
def test_malformed_declarations_are_rejected(declared):
    with pytest.raises(ValueError):
        normalize_host_session(declared)


def test_summary_serves_no_workspace_and_reports_resumable():
    assert host_session_summary("codex", "s1", True) == {
        "harness": "codex", "session_id": "s1", "resumable": True,
    }
    assert host_session_summary("codex", "s1", False)["resumable"] is False
    assert host_session_summary(None, None, False) is None


@pytest.mark.asyncio
async def test_record_helper_never_raises():
    from src.mcp_handlers.identity.handlers import _record_host_session

    db = MagicMock()
    db.update_identity_metadata = AsyncMock(return_value=True)
    with patch("src.mcp_handlers.identity.handlers.get_db", return_value=db):
        assert await _record_host_session("a" * 36, {"harness": "codex", "session_id": "s1"}) is True
        assert await _record_host_session("a" * 36, {"harness": "codex"}) is False
        db.update_identity_metadata.side_effect = RuntimeError("db down")
        assert await _record_host_session("a" * 36, {"harness": "codex", "session_id": "s1"}) is False
    stored = db.update_identity_metadata.await_args_list[0]
    assert stored.args[1]["host_session"]["session_id"] == "s1"
    assert stored.kwargs == {"merge": True}
