"""Every hint that shows how to set a display name carries client_session_id.

identity() resolves the caller from what the call carries. A call carrying
only name= falls back to the transport's signals, and on a shared route (an
IP:UA fingerprint, an onboard pin) those can resolve a co-located agent,
which then gets the name; the identity tool's description says so. About
fifteen recovery and onboarding hints still showed ``identity(name='...')``.
They now use one constant, identity_bootstrap.SET_DISPLAY_NAME_CALL, the form
the knowledge write's auto-name warning uses.

The knowledge write's own warning (knowledge/handlers) is out of scope here:
it is on the store_finding path, which #2479 rewrites.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.mcp_handlers.identity_bootstrap import SET_DISPLAY_NAME_CALL

ROOT = Path(__file__).resolve().parent.parent

# The modules whose caller-facing hints named identity(name=...).
HINT_MODULES = (
    "src/mcp_handlers/error_helpers.py",
    "src/mcp_handlers/support/naming_helpers.py",
    "src/mcp_handlers/support/agent_auth.py",
    "src/mcp_handlers/updates/phases.py",
    "src/mcp_handlers/updates/enrichments.py",
    "src/mcp_handlers/introspection/tool_catalog.py",
    "src/mcp_handlers/introspection/tool_introspection.py",
    "src/mcp_handlers/lifecycle/query.py",
    "src/services/identity_payloads.py",
    "src/tool_descriptions.py",
)


def _docstring_nodes(tree: ast.AST) -> set:
    nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                nodes.add(id(body[0].value))
    return nodes


@pytest.mark.parametrize("path", HINT_MODULES)
def test_no_hint_names_identity_with_a_name_alone(path):
    """Docstrings describe the call path (identity(name=X) does reach
    set_agent_label) and may name it; a string a caller can receive may not."""
    tree = ast.parse((ROOT / path).read_text())
    docstrings = _docstring_nodes(tree)
    offending = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
        and "identity(name=" in node.value
    ]
    assert offending == []


def test_the_shared_call_carries_the_session():
    assert SET_DISPLAY_NAME_CALL.startswith("identity(client_session_id=")
    assert "name=" in SET_DISPLAY_NAME_CALL


@pytest.mark.parametrize(
    "factory",
    [
        "agent_not_found_error",
        "agent_not_registered_error",
        "authentication_required_error",
        "authentication_error",
    ],
)
def test_recovery_patterns_name_the_session(factory):
    from src.mcp_handlers import error_helpers

    result = getattr(error_helpers, factory)("x") if factory != "authentication_error" \
        else error_helpers.authentication_error()
    text = json.dumps(json.loads(result[0].text))

    assert "identity(name=" not in text
    # An argument-less identity() mints before it reads.
    assert "identity() to" not in text
    assert "client_session_id" in text


def test_naming_guidance_names_the_session():
    from src.mcp_handlers.support.naming_helpers import format_naming_guidance

    guidance = format_naming_guidance(suggestions=[])

    assert SET_DISPLAY_NAME_CALL in guidance["how_to"]


def test_catalog_patterns_name_the_session():
    from src.mcp_handlers.introspection.tool_catalog import common_patterns_for

    assert SET_DISPLAY_NAME_CALL in common_patterns_for("identity")["name_yourself"]
    # A proof-less metrics read is unbound, so the example carries the proof.
    assert "client_session_id" in common_patterns_for("get_governance_metrics")["check_state"]


def test_the_first_update_reminder_names_the_session():
    from src.mcp_handlers.updates.enrichments import enrich_identity_reminder

    ctx = SimpleNamespace(
        meta=SimpleNamespace(total_updates=1, label=None, purpose=None),
        response_data={},
    )
    enrich_identity_reminder(ctx)

    missing = ctx.response_data["identity_reminder"]["missing"]
    assert any(SET_DISPLAY_NAME_CALL in item for item in missing)


def test_the_identity_quick_reference_names_the_session():
    from src.services.identity_payloads import build_identity_response_data

    data = build_identity_response_data(
        agent_uuid="1856bb5c-2553-4523-809b-a5d26bbd58d1",
        agent_id="Claude_Opus_5_5_20260926",
        display_name=None,
        client_session_id="agent-1856bb5c-255",
        continuity_source="explicit_client_session_id",
        continuity_support={"enabled": False},
        continuity_token=None,
        identity_status="resumed",
        model_type=None,
        resumed=True,
        session_continuity=None,
        verbose=True,
    )

    assert data["quick_reference"]["to_set_display_name"] == SET_DISPLAY_NAME_CALL
