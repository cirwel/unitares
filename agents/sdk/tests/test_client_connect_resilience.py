"""Tests for GovernanceClient connect-handshake resilience.

Background (2026-06-02): Vigil's cron cycle was found timing out at
``session.initialize()`` — an unbounded MCP handshake that, under the
anyio-asyncio connect tax, hangs on the stream ``receive()`` (not covered by
httpx's timeout) until the caller's outer cycle timeout cancels the whole
cycle, skipping every health check. connect() now bounds the handshake with
``connect_timeout`` and retries transient failures ``connect_retries`` times.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from unitares_sdk.client import GovernanceClient


# --- config plumbing -------------------------------------------------------

def test_connect_defaults_from_env(monkeypatch):
    monkeypatch.setenv("UNITARES_CONNECT_TIMEOUT", "7")
    monkeypatch.setenv("UNITARES_CONNECT_RETRIES", "3")
    c = GovernanceClient()
    assert c.connect_timeout == 7.0
    assert c.connect_retries == 3


def test_explicit_args_override_env(monkeypatch):
    monkeypatch.setenv("UNITARES_CONNECT_TIMEOUT", "7")
    monkeypatch.setenv("UNITARES_CONNECT_RETRIES", "9")
    c = GovernanceClient(connect_timeout=2.0, connect_retries=0)
    assert c.connect_timeout == 2.0
    assert c.connect_retries == 0


def test_default_budget_fits_tightest_cycle():
    """Worst-case connect time must fit Sentinel's 45s cycle budget."""
    c = GovernanceClient()  # defaults
    worst = c.connect_timeout * (c.connect_retries + 1) + c.retry_delay * c.connect_retries
    assert worst < 45.0, f"worst-case connect {worst}s would blow Sentinel's 45s cycle"


# --- retry loop ------------------------------------------------------------

@pytest.mark.asyncio
async def test_transient_failure_retries_then_succeeds(monkeypatch):
    c = GovernanceClient(connect_retries=2, retry_delay=0)
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise asyncio.TimeoutError()
        # success on 2nd attempt

    disconnects = {"n": 0}

    async def fake_disconnect():
        disconnects["n"] += 1

    monkeypatch.setattr(c, "_open_session", flaky)
    monkeypatch.setattr(c, "disconnect", fake_disconnect)

    await c.connect()
    assert calls["n"] == 2
    assert disconnects["n"] == 1  # one unwind before the successful retry


@pytest.mark.asyncio
async def test_retries_exhausted_raises_and_unwinds_each_attempt(monkeypatch):
    c = GovernanceClient(connect_retries=1, retry_delay=0)

    async def always_timeout():
        raise asyncio.TimeoutError()

    disconnects = {"n": 0}

    async def fake_disconnect():
        disconnects["n"] += 1

    monkeypatch.setattr(c, "_open_session", always_timeout)
    monkeypatch.setattr(c, "disconnect", fake_disconnect)

    with pytest.raises(asyncio.TimeoutError):
        await c.connect()
    assert disconnects["n"] == 2  # attempts = retries + 1


@pytest.mark.asyncio
async def test_non_transient_error_not_retried(monkeypatch):
    """Auth/protocol errors are deterministic — surface at once, no retry."""
    c = GovernanceClient(connect_retries=5, retry_delay=0)
    calls = {"n": 0}

    async def auth_error():
        calls["n"] += 1
        raise ValueError("auth rejected")

    monkeypatch.setattr(c, "_open_session", auth_error)
    monkeypatch.setattr(c, "disconnect", AsyncMock())

    with pytest.raises(ValueError):
        await c.connect()
    assert calls["n"] == 1  # not retried despite connect_retries=5


@pytest.mark.asyncio
async def test_connect_error_is_transient(monkeypatch):
    c = GovernanceClient(connect_retries=1, retry_delay=0)
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise httpx.ConnectError("conn refused")

    monkeypatch.setattr(c, "_open_session", flaky)
    monkeypatch.setattr(c, "disconnect", AsyncMock())

    await c.connect()
    assert calls["n"] == 2


# --- the core safety property: initialize() is bounded ---------------------

@pytest.mark.asyncio
async def test_open_session_bounds_hanging_initialize(monkeypatch):
    """A hanging initialize() must raise TimeoutError within connect_timeout,
    not block indefinitely (the 2026-06-02 failure mode)."""
    c = GovernanceClient(connect_timeout=0.05)

    fake_cm = MagicMock()
    fake_cm.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock(), None))
    fake_cm.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "unitares_sdk.client.streamable_http_client", lambda *a, **k: fake_cm
    )

    hanging_session = MagicMock()

    async def hang():
        await asyncio.sleep(10)

    hanging_session.initialize = hang
    fake_session_cm = MagicMock()
    fake_session_cm.__aenter__ = AsyncMock(return_value=hanging_session)
    fake_session_cm.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "unitares_sdk.client.ClientSession", lambda *a, **k: fake_session_cm
    )

    with pytest.raises(asyncio.TimeoutError):
        await c._open_session()
    await c.disconnect()  # clean up the mock cms / httpx client
