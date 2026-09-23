"""Contract tests for the portable Hermes Agent plugin package.

The package pins the harness in static transport headers so a Hermes check-in
is filed under Hermes whichever model provider Hermes runs on. Hermes provider
ids (``openai-codex``, ``claude``) collide with harness aliases in
``normalize_s22_harness``; the header is what keeps them apart.
"""

import json
from pathlib import Path

from src.identity.s22_h5_comparison import normalize_s22_harness
from src.mcp_handlers.context import SessionSignals, detect_client_from_user_agent
from src.model_harness_provenance import (
    build_runtime_provenance,
    runtime_signal_fields_from_headers,
)

PACKAGE = Path(__file__).resolve().parents[1] / "integrations" / "hermes"


def _server_entry() -> dict:
    mcp = json.loads((PACKAGE / "mcp.json").read_text())
    return mcp["mcpServers"]["unitares"]


def _signals(user_agent: str = "python-httpx/0.28") -> SessionSignals:
    headers = _server_entry()["headers"]
    return SessionSignals(
        user_agent=user_agent,
        client_hint=detect_client_from_user_agent(user_agent),
        **runtime_signal_fields_from_headers(headers),
    )


def test_headers_are_static_and_carry_no_credentials():
    headers = _server_entry()["headers"]
    for name, value in headers.items():
        assert name.lower().startswith("x-unitares-"), name
        assert "authorization" not in name.lower()
        assert "${" not in value, "package headers must be literal, not secrets"


def test_adapter_version_tracks_plugin_version():
    plugin = json.loads((PACKAGE / "plugin.json").read_text())
    headers = _server_entry()["headers"]
    assert headers["X-Unitares-Adapter-Version"] == plugin["version"]


def test_declared_harness_normalizes_to_hermes():
    headers = _server_entry()["headers"]
    assert normalize_s22_harness(headers["X-Unitares-Harness-Type"]) == "hermes"


def test_header_envelope_is_hermes_and_caller_declared():
    envelope = build_runtime_provenance({}, signals=_signals())

    assert envelope["harness"]["type"] == "hermes"
    assert envelope["harness"]["type_source"] == "caller_declared"
    assert envelope["adapter"]["type"] == "unitares-hermes-plugin"
    assert envelope["authority"]["is_identity_proof"] is False


def test_provider_named_harness_claim_does_not_override_header():
    """A Hermes agent on the openai-codex provider must stay a Hermes entry."""
    envelope = build_runtime_provenance(
        {
            "harness_type": "openai-codex",
            "provenance_context": {
                "model_provider": "openai-codex",
                "model": "gpt-5.6-sol",
            },
        },
        signals=_signals(),
    )

    assert envelope["harness"]["type"] == "hermes"
    assert envelope["model"]["provider"] == "openai-codex"
    assert envelope["model"]["identifier"] == "gpt-5.6-sol"
    assert envelope["model"]["exact"] is False


def test_delegated_runtime_user_agent_does_not_override_header():
    """Runtimes that hand the tool loop to Claude Code or Codex keep the header."""
    for user_agent in ("claude-code/2.1.0", "codex_cli_rs/0.115.0"):
        signals = _signals(user_agent)
        assert signals.client_hint in {"claude_code", "chatgpt"}
        envelope = build_runtime_provenance({}, signals=signals)
        assert envelope["harness"]["type"] == "hermes", user_agent
