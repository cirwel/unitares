"""The operator's policy on identities a harness asks for, and the
``spawned_under`` context: validation, defaults and the served summary."""

from datetime import datetime, timezone

import pytest

from src.identity.harness_onboarding import (
    HARNESS_ONBOARDING_DECLINED,
    HARNESS_ONBOARDING_ENV,
    SPAWNED_UNDER_SCHEMA,
    harness_onboarding_declined_payload,
    harness_onboarding_policy,
    harness_onboarding_summary,
    is_loopback_host,
    normalize_spawned_under,
    record_listen_exposure,
)

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _no_ambient_policy(monkeypatch):
    for name in (HARNESS_ONBOARDING_ENV, "UNITARES_MCP_HOST", "UNITARES_BIND_ALL_INTERFACES"):
        monkeypatch.delenv(name, raising=False)
    record_listen_exposure(None)
    yield
    record_listen_exposure(None)


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.53", "::1", "[::1]", "localhost", " LOCALHOST "])
def test_loopback_hosts(host):
    assert is_loopback_host(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "10.0.0.5", "example.org", "", None, 7])
def test_hosts_that_are_not_loopback(host):
    assert not is_loopback_host(host)


def test_a_loopback_server_welcomes_harness_identities_by_default():
    assert harness_onboarding_policy() == ("welcome", "default_loopback")


@pytest.mark.parametrize("env", [
    {"UNITARES_BIND_ALL_INTERFACES": "1"},
    {"UNITARES_MCP_HOST": "192.168.1.10"},
    {"UNITARES_MCP_HOST": "0.0.0.0"},
])
def test_an_exposed_server_accepts_agent_claims_only_by_default(monkeypatch, env):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert harness_onboarding_policy() == ("agent_only", "default_exposed")


@pytest.mark.parametrize("declared,policy", [
    ("welcome", "welcome"), ("agent_only", "agent_only"), (" Agent_Only ", "agent_only"),
])
def test_the_operators_setting_wins_over_the_exposure_default(monkeypatch, declared, policy):
    monkeypatch.setenv(HARNESS_ONBOARDING_ENV, declared)
    assert harness_onboarding_policy() == (policy, "explicit")
    monkeypatch.setenv("UNITARES_BIND_ALL_INTERFACES", "1")
    assert harness_onboarding_policy() == (policy, "explicit")


@pytest.mark.parametrize("declared", ["", "yes", "on", "welcome!", "agent-only"])
def test_an_unrecognised_setting_is_ignored_not_guessed(monkeypatch, declared):
    monkeypatch.setenv(HARNESS_ONBOARDING_ENV, declared)
    assert harness_onboarding_policy() == ("welcome", "default_loopback")
    monkeypatch.setenv("UNITARES_BIND_ALL_INTERFACES", "1")
    assert harness_onboarding_policy() == ("agent_only", "default_exposed")


def test_summary_is_the_policy_and_its_basis(monkeypatch):
    assert harness_onboarding_summary() == {"harness": "welcome", "basis": "default_loopback"}
    monkeypatch.setenv(HARNESS_ONBOARDING_ENV, "agent_only")
    assert harness_onboarding_summary() == {"harness": "agent_only", "basis": "explicit"}


def test_decline_is_a_marked_non_error_with_no_identity():
    payload = harness_onboarding_declined_payload("onboard")
    assert payload["refused"] is True
    assert payload["status"] == HARNESS_ONBOARDING_DECLINED
    assert payload["policy"] == "agent_only"
    assert HARNESS_ONBOARDING_ENV in payload["hint"]
    for absent in ("uuid", "agent_uuid", "client_session_id", "error"):
        assert absent not in payload


@pytest.mark.parametrize("anchor", [
    "agent:/thread-1573223283237847113",
    "agent:_thread-1573223283237847113",
    "agent:/thread-slack-T024BE7LD-1732062000.004200",
])
def test_spawned_under_is_stored_as_context_that_is_not_lineage(anchor):
    assert normalize_spawned_under({"anchor": anchor}, now=NOW) == {
        "schema": SPAWNED_UNDER_SCHEMA,
        "anchor": anchor,
        "recorded_at": NOW.isoformat(),
        "source": "caller_declared",
        "is_lineage": False,
    }


def test_nothing_declared_is_nothing_stored():
    assert normalize_spawned_under(None) is None


@pytest.mark.parametrize("value", [
    "agent:/thread-1",
    [],
    {},
    {"anchor": None},
    {"anchor": 5},
    {"anchor": ""},
    {"anchor": "thread-1"},
    {"anchor": "agent:/thread-"},
    {"anchor": "agent:/thread-a b"},
    {"anchor": "agent:/thread-a\nb"},
    {"anchor": "agent:/thread-4242\n"},
    {"anchor": "\nagent:/thread-4242"},
    {"anchor": " agent:/thread-4242"},
    {"anchor": "agent:/thread-" + "x" * 129},
    {"anchor": "11111111-2222-4333-8444-555555555555"},
    {"anchor": "agent:/thread-1", "parent_agent_id": "11111111-2222-4333-8444-555555555555"},
    {"anchor": "agent:/thread-1", "uuid": "x"},
])
def test_malformed_spawned_under_is_rejected(value):
    with pytest.raises(ValueError):
        normalize_spawned_under(value)


@pytest.mark.parametrize("host,public,expected", [
    ("127.0.0.1", False, ("welcome", "default_loopback")),
    ("localhost", False, ("welcome", "default_loopback")),
    # --host on the command line, with nothing in the environment.
    ("0.0.0.0", False, ("agent_only", "default_exposed")),
    ("192.168.1.10", False, ("agent_only", "default_exposed")),
    # Loopback main listener, with the public listener up for a tunnel.
    ("127.0.0.1", True, ("agent_only", "default_exposed")),
])
def test_the_default_follows_what_the_server_actually_bound(host, public, expected):
    record_listen_exposure(host, public_listener=public)
    assert harness_onboarding_policy() == expected


def test_what_was_bound_outranks_the_environment_default(monkeypatch):
    monkeypatch.setenv("UNITARES_BIND_ALL_INTERFACES", "1")
    record_listen_exposure("127.0.0.1")
    assert harness_onboarding_policy() == ("welcome", "default_loopback")


def test_the_operators_setting_outranks_what_was_bound(monkeypatch):
    record_listen_exposure("0.0.0.0", public_listener=True)
    monkeypatch.setenv(HARNESS_ONBOARDING_ENV, "welcome")
    assert harness_onboarding_policy() == ("welcome", "explicit")


def test_server_startup_records_the_bound_address():
    """The default must not depend on someone remembering to call this."""
    from pathlib import Path

    source = (Path(__file__).parent.parent / "src" / "mcp_server.py").read_text()
    assert "record_listen_exposure(args.host, public_listener=_public_socket is not None)" in source
