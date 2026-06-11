"""Wave 3 §3.2 consumer retry-on-503 — §14 prereq PR #10.

The SDK is the consumer-side surface for the resident agents (Sentinel via
the async client, Watcher via the sync REST client). Contract: honor the
``Retry-After`` header OR the typed body's ``retry_after_seconds``, retry
once, then raise ``GovernanceUnavailableError`` carrying the server delay.
"""

from __future__ import annotations

import io
import json
import urllib.error
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from unitares_sdk.client import GovernanceClient
from unitares_sdk.errors import (
    DEFAULT_RETRY_AFTER_SECONDS,
    MAX_RETRY_AFTER_SECONDS,
    GovernanceUnavailableError,
    extract_retry_after_seconds,
    parse_retry_after_header,
)
from unitares_sdk.sync_client import SyncGovernanceClient


UNAVAILABLE_BODY = {
    "ok": False,
    "error": "governance_temporarily_unavailable",
    "reason": "handler_dispatch_unavailable",
    "retry_after_seconds": 5,
}


# --- Detection helpers ---


class TestExtractRetryAfterSeconds:
    def test_typed_body(self):
        assert extract_retry_after_seconds(UNAVAILABLE_BODY) == 5.0

    def test_requires_the_pinned_error_literal(self):
        # retry_after_seconds alone is NOT the §3.2 shape — the deep-health
        # warming-up body also carries it and must not trigger tool retry.
        assert extract_retry_after_seconds(
            {"error": "Health snapshot not yet populated", "retry_after_seconds": 5}
        ) is None
        assert extract_retry_after_seconds({"retry_after_seconds": 5}) is None

    def test_non_dict(self):
        assert extract_retry_after_seconds(None) is None
        assert extract_retry_after_seconds("503") is None

    def test_missing_delay_defaults(self):
        body = {"ok": False, "error": "governance_temporarily_unavailable"}
        assert extract_retry_after_seconds(body) == DEFAULT_RETRY_AFTER_SECONDS

    def test_delay_is_capped(self):
        body = dict(UNAVAILABLE_BODY, retry_after_seconds=86400)
        assert extract_retry_after_seconds(body) == MAX_RETRY_AFTER_SECONDS

    def test_negative_delay_defaults(self):
        body = dict(UNAVAILABLE_BODY, retry_after_seconds=-1)
        assert extract_retry_after_seconds(body) == DEFAULT_RETRY_AFTER_SECONDS


class TestParseRetryAfterHeader:
    def test_delta_seconds(self):
        assert parse_retry_after_header("5") == 5.0
        assert parse_retry_after_header(" 2 ") == 2.0

    def test_absent_or_unparseable(self):
        assert parse_retry_after_header(None) is None
        assert parse_retry_after_header("Wed, 21 Oct 2026 07:28:00 GMT") is None

    def test_capped(self):
        assert parse_retry_after_header("3600") == MAX_RETRY_AFTER_SECONDS


# --- Async client (Sentinel path) ---


def _make_mcp_result(data: dict):
    content = MagicMock()
    content.text = json.dumps(data)
    result = MagicMock()
    result.isError = False
    result.content = [content]
    return result


def _make_async_client(session: AsyncMock) -> GovernanceClient:
    client = GovernanceClient(timeout=5.0, retry_delay=0.01)
    client._session = session
    return client


class TestAsyncClientRetry503:
    @pytest.mark.asyncio
    async def test_retries_once_then_succeeds(self):
        session = AsyncMock()
        session.call_tool.side_effect = [
            _make_mcp_result(UNAVAILABLE_BODY),
            _make_mcp_result({"success": True, "data": "ok"}),
        ]
        client = _make_async_client(session)
        with patch("unitares_sdk.client.asyncio.sleep", new=AsyncMock()) as sleep:
            raw = await client.call_tool("knowledge", {"action": "search"})
        assert raw["data"] == "ok"
        assert session.call_tool.await_count == 2
        # Slept the server-suggested delay, not the generic retry_delay.
        sleep.assert_awaited_once_with(5.0)

    @pytest.mark.asyncio
    async def test_raises_typed_after_budget(self):
        session = AsyncMock()
        session.call_tool.return_value = _make_mcp_result(UNAVAILABLE_BODY)
        client = _make_async_client(session)
        with patch("unitares_sdk.client.asyncio.sleep", new=AsyncMock()):
            with pytest.raises(GovernanceUnavailableError) as exc_info:
                await client.call_tool("process_agent_update", {"response_text": "x"})
        assert exc_info.value.retry_after_seconds == 5.0
        assert session.call_tool.await_count == 2

    @pytest.mark.asyncio
    async def test_plain_failure_payload_untouched(self):
        # Ordinary tool failures keep the pre-existing single-attempt raise.
        session = AsyncMock()
        session.call_tool.return_value = _make_mcp_result(
            {"success": False, "error": "Tool not found"}
        )
        client = _make_async_client(session)
        from unitares_sdk.errors import GovernanceConnectionError

        with pytest.raises(GovernanceConnectionError, match="Tool not found"):
            await client.call_tool("bad_tool", {})
        assert session.call_tool.await_count == 1


# --- Sync REST client (Watcher path) ---


def _http_503(
    body: dict | None = None, retry_after_header: str | None = None
) -> urllib.error.HTTPError:
    headers = {}
    if retry_after_header is not None:
        headers["Retry-After"] = retry_after_header
    raw = json.dumps(body if body is not None else UNAVAILABLE_BODY).encode()
    return urllib.error.HTTPError(
        url="http://127.0.0.1:8767/v1/tools/call",
        code=503,
        msg="Service Unavailable",
        hdrs=headers,  # HTTPError exposes .headers with a dict-like .get
        fp=io.BytesIO(raw),
    )


def _ok_response(data: dict):
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(data).encode()
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


class TestSyncClientRetry503:
    @patch("unitares_sdk.sync_client.time.sleep")
    @patch("unitares_sdk.sync_client.urllib.request.urlopen")
    def test_retries_once_then_succeeds(self, mock_open, mock_sleep):
        mock_open.side_effect = [
            _http_503(retry_after_header="2"),
            _ok_response({"success": True, "result": {"success": True, "data": "ok"}}),
        ]
        client = SyncGovernanceClient(transport="rest")
        raw = client.call_tool("knowledge", {"action": "search"})
        assert raw["data"] == "ok"
        assert mock_open.call_count == 2
        mock_sleep.assert_called_once_with(2.0)

    @patch("unitares_sdk.sync_client.time.sleep")
    @patch("unitares_sdk.sync_client.urllib.request.urlopen")
    def test_raises_typed_after_budget(self, mock_open, mock_sleep):
        mock_open.side_effect = [_http_503(), _http_503()]
        client = SyncGovernanceClient(transport="rest")
        with pytest.raises(GovernanceUnavailableError) as exc_info:
            client.call_tool("knowledge", {"action": "search"})
        assert exc_info.value.retry_after_seconds == 5.0
        assert mock_open.call_count == 2

    @patch("unitares_sdk.sync_client.time.sleep")
    @patch("unitares_sdk.sync_client.urllib.request.urlopen")
    def test_body_delay_used_when_header_absent(self, mock_open, mock_sleep):
        mock_open.side_effect = [
            _http_503(body=dict(UNAVAILABLE_BODY, retry_after_seconds=3)),
            _ok_response({"success": True, "result": {"success": True}}),
        ]
        client = SyncGovernanceClient(transport="rest")
        client.call_tool("knowledge", {"action": "search"})
        mock_sleep.assert_called_once_with(3.0)

    @patch("unitares_sdk.sync_client.urllib.request.urlopen")
    def test_non_503_http_error_unchanged(self, mock_open):
        mock_open.side_effect = urllib.error.HTTPError(
            url="http://x", code=500, msg="boom", hdrs={}, fp=io.BytesIO(b"")
        )
        from unitares_sdk.errors import GovernanceConnectionError

        client = SyncGovernanceClient(transport="rest")
        with pytest.raises(GovernanceConnectionError):
            client.call_tool("knowledge", {"action": "search"})
        assert mock_open.call_count == 1
