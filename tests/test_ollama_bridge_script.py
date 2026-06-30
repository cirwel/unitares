"""ollama_bridge.py unit tests — identity posture (v2 ontology) and the
in-process client_session_id capture/echo that keeps the bridge one trajectory
under the strict-identity gate.

The module imports smolagents at top level (an optional client-only dep, not a
declared server requirement), so the whole module is skipped where it is
absent."""
import argparse
import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("smolagents")

_MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "client" / "ollama_bridge.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("ollama_bridge", _MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bridge = _load_module()


def _args(**over):
    """Build an argparse.Namespace with the identity fields from_args reads."""
    base = dict(agent_uuid=None, parent_agent_id=None, spawn_reason=None)
    base.update(over)
    return argparse.Namespace(**base)


# --- IdentityConfig: ontology posture ---------------------------------------

def test_fresh_default_is_new_session_no_lineage(monkeypatch):
    monkeypatch.delenv("UNITARES_SPAWN_REASON", raising=False)
    monkeypatch.delenv("UNITARES_PARENT_AGENT_ID", raising=False)
    monkeypatch.delenv("UNITARES_AGENT_UUID", raising=False)
    cfg = bridge.IdentityConfig.from_args(_args())
    assert cfg.spawn_reason == "new_session"
    assert cfg.parent_agent_id is None
    assert cfg.agent_uuid is None
    assert "no lineage" in cfg.describe()


def test_causal_spawn_reason_requires_parent():
    # "explicit" is a handoff from an EXITED predecessor — needs a parent UUID.
    with pytest.raises(SystemExit):
        bridge.IdentityConfig.from_args(_args(spawn_reason="explicit"))


def test_causal_spawn_reason_with_parent_is_accepted():
    cfg = bridge.IdentityConfig.from_args(
        _args(spawn_reason="explicit", parent_agent_id="parent-uuid")
    )
    assert cfg.spawn_reason == "explicit"
    assert cfg.parent_agent_id == "parent-uuid"
    assert "explicit" in cfg.describe()


def test_invalid_spawn_reason_rejected():
    with pytest.raises(SystemExit):
        bridge.IdentityConfig.from_args(_args(spawn_reason="bogus"))


def test_env_supplies_defaults(monkeypatch):
    monkeypatch.setenv("UNITARES_AGENT_UUID", "durable-uuid")
    cfg = bridge.IdentityConfig.from_args(_args())
    assert cfg.agent_uuid == "durable-uuid"
    assert "substrate-anchored" in cfg.describe()


# --- _deep_find -------------------------------------------------------------

def test_deep_find_locates_nested_key():
    blob = {"a": {"b": [{"client_session_id": "csid-123"}]}}
    assert bridge._deep_find(blob, "client_session_id") == "csid-123"
    assert bridge._deep_find(blob, "missing") is None


# --- Session: in-process continuity (the strict-safety fix) -----------------

def test_session_captures_and_echoes_client_session_id():
    sess = bridge.Session()
    # An onboard-shaped response carrying the proof signal.
    sess.capture('{"agent_uuid": "u-1", "client_session_id": "csid-xyz"}')
    assert sess.client_session_id == "csid-xyz"
    assert sess.agent_uuid == "u-1"
    # Every later call must echo it — otherwise the write resolves by transport
    # fingerprint and lands on a sibling identity (or refuses under strict).
    bound = sess.bind(response_text="did a thing", complexity=0.2)
    assert bound["client_session_id"] == "csid-xyz"


def test_session_bind_is_noop_before_capture():
    sess = bridge.Session()
    assert sess.bind(action="list") == {"action": "list"}


def test_session_capture_tolerates_non_json():
    sess = bridge.Session()
    assert sess.capture("not json") == "not json"
    assert sess.client_session_id is None


# --- make_wrappers: writes echo the captured session id ---------------------

def test_wrappers_echo_session_id_on_writes():
    calls = {}

    def rec(name):
        def _f(**kwargs):
            calls[name] = kwargs
            # onboard hands back the proof signal the session should capture.
            if name == "onboard":
                return '{"agent_uuid": "u-9", "client_session_id": "csid-9"}'
            return "{}"
        return _f

    mcp_tools = {n: rec(n) for n in (
        "health_check", "onboard", "identity", "sync_state",
        "check_working_state", "knowledge", "leave_note", "agent",
        "describe_tool",
    )}
    identity = bridge.IdentityConfig()  # fresh new_session
    session = bridge.Session()
    tools = {t.name: t for t in bridge.make_wrappers(mcp_tools, identity, session)}

    tools["register_agent"]("ollama-local")
    assert session.client_session_id == "csid-9"

    tools["checkin"]("verified", 0.3, 0.8)
    assert calls["sync_state"]["client_session_id"] == "csid-9"

    tools["list_agents"]()
    assert calls["agent"]["client_session_id"] == "csid-9"
