"""The OAuth setup failure path must not be silently permissive.

Setting UNITARES_OAUTH_ISSUER_URL is a request for an auth gate. If provider
construction then fails, the old handler logged one line and served the MCP
route ungated — silent in both directions: it hides a lockout from whoever is
debugging one, and it hides an unauthenticated surface from whoever believes
auth is on.

The default is still to start (an unreachable server helps nobody), so what is
pinned here is the posture, not the outcome: the failure must be legible, and
an operator must be able to choose down-over-open. Asserted against the source
rather than by import because the block runs at module import time, where
exercising it means constructing the whole server.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MCP_SERVER = REPO_ROOT / "src" / "mcp_server.py"

REQUIRED_FLAG = "UNITARES_OAUTH_REQUIRED"


@pytest.fixture(scope="module")
def oauth_handler_source() -> str:
    """Source of the except-handler guarding OAuth provider construction."""
    source = MCP_SERVER.read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        if not (isinstance(node.test, ast.Name) and node.test.id == "_oauth_issuer_url"):
            continue
        for stmt in node.body:
            if isinstance(stmt, ast.Try) and stmt.handlers:
                return "\n".join(
                    ast.get_source_segment(source, h) or "" for h in stmt.handlers
                )
    pytest.fail("could not locate the OAuth setup try/except in src/mcp_server.py")


def test_failure_path_offers_a_fail_closed_choice(oauth_handler_source):
    assert REQUIRED_FLAG in oauth_handler_source, (
        f"the OAuth failure handler no longer consults {REQUIRED_FLAG}; a "
        "deployment that would rather be down than open has lost its only lever"
    )


def test_failure_path_can_actually_refuse_to_start(oauth_handler_source):
    handler_ast = ast.parse(_as_module(oauth_handler_source))
    assert any(isinstance(n, ast.Raise) for n in ast.walk(handler_ast)), (
        "the OAuth failure handler never re-raises, so fail-closed is unreachable"
    )


def test_failure_path_says_the_surface_is_unauthenticated(oauth_handler_source):
    lowered = oauth_handler_source.lower()
    assert "no auth gate" in lowered, (
        "the warning no longer states that the MCP route is serving without an "
        "auth gate — the whole point is that this cannot be skimmed past"
    )


def _as_module(handler_source: str) -> str:
    """Wrap bare `except ...:` clauses back into a parseable try statement."""
    return "try:\n    pass\n" + handler_source
