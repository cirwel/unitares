"""Regression: identity() response harness_context must match onboard().

identity() resume previously reported harness_context.harness_type="unknown"
because its response builders received only arguments.get("client_hint"),
while onboard() falls back to the transport-bound hint. The
_resolve_response_client_hint helper mirrors that fallback so the
descriptive harness context is consistent across both surfaces.
"""

import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.mcp_handlers.identity.handlers import _resolve_response_client_hint
from src.mcp_handlers.context import (
    set_transport_client_hint,
    reset_transport_client_hint,
)


def test_explicit_arg_wins():
    assert _resolve_response_client_hint({"client_hint": "cursor"}) == "cursor"


def test_falls_back_to_transport_when_arg_missing():
    """The resume case: no client_hint in args, transport carries it."""
    token = set_transport_client_hint("claude_code")
    try:
        assert _resolve_response_client_hint({}) == "claude_code"
        assert _resolve_response_client_hint(None) == "claude_code"
    finally:
        reset_transport_client_hint(token)


def test_falls_back_to_transport_when_arg_is_unknown():
    token = set_transport_client_hint("claude_code")
    try:
        assert _resolve_response_client_hint({"client_hint": "unknown"}) == "claude_code"
    finally:
        reset_transport_client_hint(token)


def test_returns_none_when_nothing_available():
    """No arg, no transport hint -> None (builder maps to 'unknown')."""
    assert _resolve_response_client_hint({}) is None


def test_arg_preferred_over_transport():
    token = set_transport_client_hint("claude_code")
    try:
        assert _resolve_response_client_hint({"client_hint": "cursor"}) == "cursor"
    finally:
        reset_transport_client_hint(token)
