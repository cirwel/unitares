"""The OAuth setup failure path must not be silently permissive.

Setting UNITARES_OAUTH_ISSUER_URL is a request for an auth gate. If provider
construction then fails, the old handler logged one line and served the MCP
route ungated — silent in both directions: it hides a lockout from whoever is
debugging one, and it hides an unauthenticated surface from whoever believes
auth is on.

These tests call the real predicate. Two earlier versions did not and were
weaker than they looked. Asserting against the parsed source of
``src/mcp_server.py`` passed unchanged when the refusal was guarded by
``if False`` and when the condition was inverted. Re-implementing the decision
as a local mirror passed unchanged when the real flag parse dropped its
``.strip()`` — which is precisely how a fail-closed safety flag fails open on a
value of `` 1`` from a sourced env file. The decision now lives in
``src.mcp_listen_config`` so it can be imported and exercised; that module is
side-effect free, unlike ``src.mcp_server``, which builds the server at import.

The source is still inspected, but only for the things a function call cannot
prove: that the startup path routes through this predicate, and that the failure
log does not interpolate an exception whose text can carry a URL credential.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.mcp_listen_config import auth_gate_refusal, oauth_gate_required

REPO_ROOT = Path(__file__).resolve().parent.parent
MCP_SERVER = REPO_ROOT / "src" / "mcp_server.py"

REQUIRED_FLAG = "UNITARES_OAUTH_REQUIRED"
BEARER_ENV = "UNITARES_MCP_BEARER_TOKENS"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (REQUIRED_FLAG, BEARER_ENV, "UNITARES_OAUTH_ISSUER_URL"):
        monkeypatch.delenv(var, raising=False)


# --- the decision -----------------------------------------------------------

def test_default_still_starts_when_oauth_construction_fails():
    """An unreachable server helps nobody; the default is unchanged."""
    assert auth_gate_refusal(
        provider_present=False, issuer_set=True, setup_error_name="ValidationError"
    ) is None


def test_required_flag_refuses_to_serve_unauthenticated(monkeypatch):
    monkeypatch.setenv(REQUIRED_FLAG, "1")
    refusal = auth_gate_refusal(
        provider_present=False, issuer_set=True, setup_error_name="ValidationError"
    )
    assert refusal is not None
    assert "ValidationError" in refusal


def test_required_flag_is_not_inert_when_the_issuer_is_unset(monkeypatch):
    """A typo'd issuer name must not silently disarm a fail-closed flag."""
    monkeypatch.setenv(REQUIRED_FLAG, "1")
    refusal = auth_gate_refusal(provider_present=False, issuer_set=False)
    assert refusal is not None
    assert "UNITARES_OAUTH_ISSUER_URL is unset" in refusal


def test_a_bearer_allowlist_satisfies_the_requirement(monkeypatch):
    """The requirement is an auth gate, not OAuth specifically."""
    monkeypatch.setenv(REQUIRED_FLAG, "1")
    monkeypatch.setenv(BEARER_ENV, "deadbeef")
    assert auth_gate_refusal(provider_present=False, issuer_set=False) is None


def test_no_refusal_when_oauth_built_fine(monkeypatch):
    monkeypatch.setenv(REQUIRED_FLAG, "1")
    assert auth_gate_refusal(provider_present=True, issuer_set=True) is None


def test_unset_flag_never_refuses():
    assert auth_gate_refusal(provider_present=False, issuer_set=True) is None


def test_refusal_names_where_the_flag_must_live(monkeypatch):
    """~/.env.mcp is loaded after import and cannot reach this flag."""
    monkeypatch.setenv(REQUIRED_FLAG, "1")
    refusal = auth_gate_refusal(provider_present=False, issuer_set=False)
    assert "env.mcp" in refusal


# --- the parse, which is where a safety flag most easily fails open ---------

@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on", " 1", "1\n", "  true  "])
def test_flag_parse_accepts_padded_and_cased_truthy_values(monkeypatch, raw):
    """A value of ' 1' from a sourced env file must not read as unset."""
    monkeypatch.setenv(REQUIRED_FLAG, raw)
    assert oauth_gate_required() is True


@pytest.mark.parametrize("raw", ["", "0", "false", "no", "off", "maybe"])
def test_flag_parse_rejects_falsey_values(monkeypatch, raw):
    monkeypatch.setenv(REQUIRED_FLAG, raw)
    assert oauth_gate_required() is False


# --- what a function call cannot prove -------------------------------------

@pytest.fixture(scope="module")
def server_source() -> str:
    return MCP_SERVER.read_text()


def test_startup_routes_through_the_predicate(server_source):
    tree = ast.parse(server_source)
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        and n.func.id == "auth_gate_refusal"
    ]
    assert calls, (
        "src/mcp_server.py no longer calls auth_gate_refusal, so the tests above "
        "grade a predicate the server does not consult"
    )

    # The raise must hang off the refusal value alone. `if False and _auth_refusal:`
    # keeps a call and a raise in the file while making fail-closed unreachable,
    # and an earlier version of this test passed on exactly that mutation.
    guards = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.If)
        and isinstance(n.test, ast.Name)
        and n.test.id == "_auth_refusal"
        and any(isinstance(s, ast.Raise) for s in n.body)
    ]
    assert guards, (
        "no `if _auth_refusal: raise` at module scope — fail-closed is unreachable, "
        "or its guard has been widened with another term"
    )


def test_predicate_is_consulted_outside_the_issuer_branch(server_source):
    """Inside it, the flag goes inert whenever the issuer variable is missing."""
    tree = ast.parse(server_source)
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and isinstance(node.test, ast.Name) \
                and node.test.id == "_oauth_issuer_url":
            inside = [
                sub for stmt in node.body for sub in ast.walk(stmt)
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                and sub.func.id == "auth_gate_refusal"
            ]
            assert not inside, (
                "the refusal is decided inside the issuer branch again; it goes "
                "inert whenever UNITARES_OAUTH_ISSUER_URL is missing or misspelled"
            )
            return
    pytest.fail("could not locate the OAuth issuer branch in src/mcp_server.py")


def test_failure_warning_states_the_route_is_ungated(server_source):
    assert "NO AUTH GATE" in server_source, (
        "the warning no longer states that the MCP route is serving without an "
        "auth gate — the whole point is that this cannot be skimmed past"
    )


def test_failure_warning_does_not_interpolate_the_exception_value(server_source):
    """A pydantic ValidationError echoes the offending URL, userinfo included."""
    tree = ast.parse(server_source)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.If) and isinstance(node.test, ast.Name)
                and node.test.id == "_oauth_issuer_url"):
            continue
        for stmt in node.body:
            if isinstance(stmt, ast.Try):
                handlers = "\n".join(
                    ast.get_source_segment(server_source, h) or "" for h in stmt.handlers
                )
                assert "{e}" not in handlers, (
                    "the failure log interpolates the raw exception again; use "
                    "type(e).__name__ so a URL carrying credentials is not logged"
                )
                return
    pytest.fail("could not locate the OAuth setup try/except in src/mcp_server.py")
