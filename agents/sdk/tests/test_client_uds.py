"""S19 PR5: GovernanceClient UDS transport selection.

Tests cover:
- Default (no uds_path, no env): HTTP transport, no UDS configuration.
- Explicit ``uds_path=`` parameter: UDS transport selected.
- ``UNITARES_UDS_SOCKET`` env var: UDS transport selected.
- Explicit ``uds_path=`` overrides the env var (so tests / config can pin).
- Connect path actually constructs ``httpx.AsyncHTTPTransport(uds=...)``.

Connection-establishment paths are not exercised end-to-end here (would
require a running governance MCP). The focus is the transport-selection
logic that PR5 introduces; the actual UDS connection round-trip was
verified end-to-end by ``tests/test_uds_listener.py`` in PR3c.
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from unitares_sdk.client import GovernanceClient


# =============================================================================
# Construction-time transport selection
# =============================================================================


def test_default_no_uds_path_when_no_arg_no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UNITARES_UDS_SOCKET", raising=False)
    client = GovernanceClient()
    assert client.uds_path is None


def test_explicit_uds_path_argument_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UNITARES_UDS_SOCKET", raising=False)
    client = GovernanceClient(uds_path="/tmp/explicit.sock")
    assert client.uds_path == "/tmp/explicit.sock"


def test_env_var_picked_up_when_no_explicit_arg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UNITARES_UDS_SOCKET", "/home/agent/.unitares/governance.sock")
    client = GovernanceClient()
    assert client.uds_path == "/home/agent/.unitares/governance.sock"


def test_explicit_arg_overrides_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests / per-call overrides must beat the env-var default."""
    monkeypatch.setenv("UNITARES_UDS_SOCKET", "/from/env.sock")
    client = GovernanceClient(uds_path="/from/explicit.sock")
    assert client.uds_path == "/from/explicit.sock"


def test_empty_env_var_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty string env var (e.g. ``UNITARES_UDS_SOCKET=``) is treated as
    unset, not as the literal empty string."""
    monkeypatch.setenv("UNITARES_UDS_SOCKET", "")
    client = GovernanceClient()
    assert client.uds_path is None


# =============================================================================
# Connect path constructs httpx.AsyncHTTPTransport(uds=...)
# =============================================================================


@pytest.mark.asyncio
async def test_connect_builds_uds_transport_when_uds_path_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When uds_path is set, connect() constructs httpx with UDS transport."""
    captured: dict[str, object] = {}

    real_async_client = httpx.AsyncClient

    def capturing_client(*args, **kwargs):
        captured["transport"] = kwargs.get("transport")
        captured["http2"] = kwargs.get("http2")
        return real_async_client(*args, **kwargs)

    # Stub out the MCP transport setup; we only care that the httpx config
    # was right at construction time. The streamable_http_client returns
    # a context manager whose __aenter__ returns (read, write, _).
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock(), None))
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    mock_session_cm = AsyncMock()
    mock_session = AsyncMock()
    mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_cm.__aexit__ = AsyncMock(return_value=False)

    with patch("unitares_sdk.client.httpx.AsyncClient", side_effect=capturing_client), \
         patch("unitares_sdk.client.streamable_http_client", return_value=mock_cm), \
         patch("unitares_sdk.client.ClientSession", return_value=mock_session_cm):
        client = GovernanceClient(uds_path="/tmp/test-s19.sock")
        await client.connect()
        try:
            transport = captured.get("transport")
            assert isinstance(transport, httpx.AsyncHTTPTransport), (
                f"expected AsyncHTTPTransport for UDS, got {type(transport).__name__}"
            )
        finally:
            await client.disconnect()


@pytest.mark.asyncio
async def test_connect_skips_uds_transport_when_no_uds_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When uds_path is None, connect() builds the default httpx client
    without the explicit transport= kwarg (matches pre-PR5 behavior)."""
    monkeypatch.delenv("UNITARES_UDS_SOCKET", raising=False)
    captured: dict[str, object] = {"transport_set": False}

    real_async_client = httpx.AsyncClient

    def capturing_client(*args, **kwargs):
        captured["transport_set"] = "transport" in kwargs and kwargs["transport"] is not None
        return real_async_client(*args, **kwargs)

    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock(), None))
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    mock_session_cm = AsyncMock()
    mock_session = AsyncMock()
    mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_cm.__aexit__ = AsyncMock(return_value=False)

    with patch("unitares_sdk.client.httpx.AsyncClient", side_effect=capturing_client), \
         patch("unitares_sdk.client.streamable_http_client", return_value=mock_cm), \
         patch("unitares_sdk.client.ClientSession", return_value=mock_session_cm):
        client = GovernanceClient()
        await client.connect()
        try:
            assert captured["transport_set"] is False
        finally:
            await client.disconnect()
