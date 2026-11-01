"""Unit tests for the extracted _fingerprint_hijack_check helper.

The end-to-end PATH 1 behavior is covered by test_identity_path1_fingerprint.py;
these pin the helper's own contract so PATH 2 can reuse it safely:
returns True ONLY on a strict-mode violation (B1: a bool, never mutates the
caller), fires identity_hijack_suspected on any violation, and honors the
global / per-path (prefix) mode resolution.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.mcp_handlers.identity import resolution

_AU = "11111111-1111-4111-8111-111111111111"


def _ctx(*, global_mode="off", prefix_mode="off", current_fp=None, broadcaster=None):
    """Patch the helper's lazily-imported deps."""
    sig = SimpleNamespace(ip_ua_fingerprint=current_fp)
    return [
        patch("config.governance_config.session_fingerprint_check_mode", return_value=global_mode),
        patch("config.governance_config.prefix_bind_fingerprint_mode", return_value=prefix_mode),
        patch("src.mcp_handlers.context.get_session_signals", return_value=sig),
        patch("src.mcp_handlers.identity.handlers._broadcaster", return_value=broadcaster),
    ]


async def _call(session_key, bound_fp, **ctx_kwargs):
    bcast = MagicMock()
    bcast.broadcast_event = AsyncMock()
    ctx_kwargs.setdefault("broadcaster", bcast)
    patches = _ctx(**ctx_kwargs)
    for p in patches:
        p.start()
    try:
        result = await resolution._fingerprint_hijack_check(session_key, bound_fp, _AU)
    finally:
        for p in patches:
            p.stop()
    return result, ctx_kwargs["broadcaster"]


@pytest.mark.asyncio
async def test_off_mode_returns_false_no_event():
    result, b = await _call("agent-abc", "bound-fp", global_mode="off", prefix_mode="off", current_fp="other")
    assert result is False
    b.broadcast_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_log_mode_mismatch_fires_event_but_does_not_block():
    result, b = await _call("1.2.3.4:xx", "bound-fp", global_mode="log", current_fp="DIFFERENT")
    assert result is False  # log mode never blocks
    b.broadcast_event.assert_awaited_once()
    assert b.broadcast_event.call_args.kwargs["payload"]["reason"] == "fingerprint_mismatch"


@pytest.mark.asyncio
async def test_strict_mode_mismatch_blocks_and_fires_event():
    result, b = await _call("1.2.3.4:xx", "bound-fp", global_mode="strict", current_fp="DIFFERENT")
    assert result is True  # strict -> caller sets resume=False
    b.broadcast_event.assert_awaited_once()
    kw = b.broadcast_event.call_args.kwargs
    assert kw["event_type"] == "identity_hijack_suspected"
    assert kw["agent_id"] == _AU
    assert kw["payload"]["mode"] == "strict"


@pytest.mark.asyncio
async def test_matching_fingerprints_no_violation():
    result, b = await _call("1.2.3.4:xx", "same-fp", global_mode="strict", current_fp="same-fp")
    assert result is False
    b.broadcast_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_prefix_strict_absent_bound_fp_blocks():
    """agent- key under prefix-strict: a missing binding-time fp is non-authorizing."""
    result, b = await _call("agent-abc", None, prefix_mode="strict", current_fp="curr")
    assert result is True
    assert b.broadcast_event.call_args.kwargs["payload"]["reason"] == "no_bind_fingerprint"


@pytest.mark.asyncio
async def test_global_mode_never_penalizes_missing_fp_on_nonprefix():
    """Legacy-cache contract: global mode must not fire on a missing fp for a
    non-prefix key (only the per-path prefix mode does)."""
    result, b = await _call("1.2.3.4:xx", None, global_mode="strict", current_fp=None)
    assert result is False
    b.broadcast_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_keyed_session_same_host_ua_shift_is_not_a_violation_in_log_mode(monkeypatch):
    """In log mode, a verified keyed session id shared between an IDE/CLI MCP
    client and a local hook helper (urllib) on loopback does not emit a hijack
    alert."""
    from src.mcp_handlers.identity.shared import make_client_session_id
    monkeypatch.setenv("UNITARES_CONTINUITY_TOKEN_SECRET", "keyed-session-test-secret")
    keyed_key = make_client_session_id(_AU)
    result, b = await _call(
        keyed_key,
        "127.0.0.1:f6f44a",
        global_mode="log",
        current_fp="127.0.0.1:020f1c",
    )
    assert result is False
    b.broadcast_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_forged_keyed_session_same_host_ua_shift_still_fires_in_log_mode(monkeypatch):
    """A forged keyed id whose HMAC tag does not verify for agent_uuid is not
    exempted from the loopback UA-shift check in log mode."""
    monkeypatch.setenv("UNITARES_CONTINUITY_TOKEN_SECRET", "keyed-session-test-secret")
    forged_key = "agent-11111111-111-abcdefghijklmnopqrst"
    result, b = await _call(
        forged_key,
        "127.0.0.1:f6f44a",
        global_mode="log",
        current_fp="127.0.0.1:020f1c",
    )
    assert result is False
    b.broadcast_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_keyed_session_same_host_ua_shift_still_blocked_in_strict_mode(monkeypatch):
    """Strict mode keeps the full fingerprint guard on loopback so a headerless
    local forwarder (SSH tunnel / socat) cannot bypass strict enforcement."""
    from src.mcp_handlers.identity.shared import make_client_session_id
    monkeypatch.setenv("UNITARES_CONTINUITY_TOKEN_SECRET", "keyed-session-test-secret")
    keyed_key = make_client_session_id(_AU)
    result, b = await _call(
        keyed_key,
        "127.0.0.1:f6f44a",
        global_mode="strict",
        current_fp="127.0.0.1:020f1c",
    )
    assert result is True
    b.broadcast_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_nonkeyed_session_same_host_ua_shift_still_fires():
    """Unkeyed/legacy session keys still treat a UA change on the same IP as a mismatch."""
    result, b = await _call(
        "agent-11111111-111",
        "127.0.0.1:f6f44a",
        global_mode="strict",
        current_fp="127.0.0.1:020f1c",
    )
    assert result is True
    b.broadcast_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_keyed_session_nonloopback_same_ip_ua_shift_still_fires(monkeypatch):
    """A shared non-loopback address (e.g. proxy/NAT) is not exempted on keyed sessions."""
    from src.mcp_handlers.identity.shared import make_client_session_id
    monkeypatch.setenv("UNITARES_CONTINUITY_TOKEN_SECRET", "keyed-session-test-secret")
    keyed_key = make_client_session_id(_AU)
    result, b = await _call(
        keyed_key,
        "10.0.0.1:f6f44a",
        global_mode="strict",
        current_fp="10.0.0.1:020f1c",
    )
    assert result is True
    b.broadcast_event.assert_awaited_once()
