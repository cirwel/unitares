"""Bearer gate for the /mcp endpoint (src/mcp_listen_config).

This is the one primitive a hosted deployment needs and that was absent: a way
to require an Authorization: Bearer token on every /mcp request, while staying
byte-identical to current behavior when no token is configured (localhost dev /
self-host). These tests pin both halves — the off-by-default posture and the
on-path accept/reject rules — plus the deliberate no-trusted-bypass design.
"""

from __future__ import annotations

import importlib

import pytest

import src.mcp_listen_config as cfg


@pytest.fixture(autouse=True)
def _clear_bearer_env(monkeypatch):
    monkeypatch.delenv("UNITARES_MCP_BEARER_TOKENS", raising=False)
    importlib.reload(cfg)
    yield


def test_gate_off_by_default():
    # No env configured -> gate OFF, everything allowed, even no header.
    assert cfg.mcp_bearer_required() is False
    assert cfg.check_mcp_bearer(None) is True
    assert cfg.check_mcp_bearer("anything") is True


def test_gate_on_when_tokens_configured(monkeypatch):
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", "s3cret")
    assert cfg.mcp_bearer_required() is True
    assert cfg.mcp_bearer_tokens() == ["s3cret"]


def test_valid_bearer_accepted(monkeypatch):
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", "s3cret")
    assert cfg.check_mcp_bearer("Bearer s3cret") is True


@pytest.mark.parametrize(
    "header",
    [
        None,                       # missing header
        "",                         # empty header
        "s3cret",                   # raw token, no "Bearer " scheme
        "Bearer ",                  # scheme but empty token
        "Bearer wrong",             # wrong token
        "Basic s3cret",             # wrong scheme
        "bearer s3cret",            # case-sensitive scheme (RFC says token68 scheme is case-insensitive,
                                    # but we require the canonical "Bearer " prefix the clients send)
    ],
)
def test_invalid_bearer_rejected_when_gate_on(monkeypatch, header):
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", "s3cret")
    assert cfg.check_mcp_bearer(header) is False


def test_token_rotation_accepts_any_listed(monkeypatch):
    # CSV allowlist lets an operator rotate without dropping the old token mid-flight.
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", "old-token, new-token")
    assert cfg.mcp_bearer_tokens() == ["old-token", "new-token"]
    assert cfg.check_mcp_bearer("Bearer old-token") is True
    assert cfg.check_mcp_bearer("Bearer new-token") is True
    assert cfg.check_mcp_bearer("Bearer retired-token") is False


def test_whitespace_only_env_is_gate_off(monkeypatch):
    # "   ,  ," parses to zero tokens -> still OFF (no accidental empty-token allow).
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", "   ,  ,")
    assert cfg.mcp_bearer_required() is False
    assert cfg.check_mcp_bearer(None) is True


def test_read_fresh_each_call_supports_runtime_rotation(monkeypatch):
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", "first")
    assert cfg.check_mcp_bearer("Bearer first") is True
    # Rotate in-process; no restart, no reload — read-fresh semantics.
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", "second")
    assert cfg.check_mcp_bearer("Bearer first") is False
    assert cfg.check_mcp_bearer("Bearer second") is True


def test_explicit_allow_overrides_env(monkeypatch):
    # The ASGI gate fetches the allowlist once and passes it through, so the
    # decision uses one snapshot (no add/remove TOCTOU vs a second env read).
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", "env-token")
    assert cfg.check_mcp_bearer("Bearer snapshot", allow=["snapshot"]) is True
    assert cfg.check_mcp_bearer("Bearer env-token", allow=["snapshot"]) is False
    # Empty explicit allow == gate off for this decision.
    assert cfg.check_mcp_bearer(None, allow=[]) is True


def test_sse_routes_are_prunable_to_close_gate_bypass():
    # The /mcp bearer gate would be bypassable if /sse + /messages/ (which the
    # SDK wires to the SAME tool registry, unauthenticated when OAuth is off)
    # stayed mounted. mcp_server prunes them; assert that prune fully removes
    # the SSE surface so no ungated route reaches the tools.
    pytest.importorskip("mcp.server.fastmcp")
    from mcp.server.fastmcp import FastMCP

    m = FastMCP("probe")
    app = m.sse_app()
    before = {getattr(r, "path", None) for r in app.routes}
    assert m.settings.sse_path in before  # /sse present pre-prune

    _sse = m.settings.sse_path
    _msg = m.settings.message_path.rstrip("/")
    app.routes[:] = [r for r in app.routes if getattr(r, "path", None) not in (_sse, _msg)]

    after = {getattr(r, "path", None) for r in app.routes}
    assert _sse not in after
    assert _msg not in after
