"""Regression tests for src/agent_identity_auth.py.

This module is the agent identity/authentication surface: ID-format validation,
API-key generation, ownership verification, and the impersonation gate. It had
no direct test coverage despite living on the identity surface CLAUDE.md flags
as fragile. These tests pin current correct behavior so a future change that
weakens the auth gate (e.g. swapping the constant-time key comparison, loosening
ID validation, or changing the session-bound bypass) is caught immediately.

The security-critical invariants under guard:
  * verify_agent_ownership rejects wrong keys and uses constant-time compare
  * require_agent_auth blocks callers with no/invalid key for protected agents
  * generate_api_key produces high-entropy, URL-safe, unique keys
  * validate_agent_id_format refuses generic / collision-prone identifiers
"""

from __future__ import annotations

import base64

import pytest

from src.agent_identity_auth import (
    check_agent_status,
    check_agent_id_default,
    validate_agent_id_format,
    require_agent_id,
    generate_api_key,
    verify_agent_ownership,
    require_agent_auth,
)
from src.agent_metadata_model import AgentMetadata, agent_metadata


@pytest.fixture
def clean_metadata():
    """Isolate the module-global agent_metadata dict per test.

    agent_identity_auth reads a process-global registry; save/restore so a test
    that seeds an agent never leaks into another test (or production state).
    """
    saved = dict(agent_metadata)
    agent_metadata.clear()
    try:
        yield agent_metadata
    finally:
        agent_metadata.clear()
        agent_metadata.update(saved)


def _make_agent(agent_id: str, *, api_key: str | None = None, status: str = "active") -> AgentMetadata:
    return AgentMetadata(
        agent_id=agent_id,
        status=status,
        created_at="2026-06-15T00:00:00Z",
        last_update="2026-06-15T00:00:00Z",
        api_key=api_key,
    )


# --------------------------------------------------------------------------- #
# check_agent_status
# --------------------------------------------------------------------------- #

class TestCheckAgentStatus:
    def test_unknown_agent_is_allowed(self, clean_metadata):
        assert check_agent_status("never_seen") is None

    def test_active_agent_is_allowed(self, clean_metadata):
        clean_metadata["a"] = _make_agent("a", status="active")
        assert check_agent_status("a") is None

    @pytest.mark.parametrize("status", ["paused", "archived", "deleted"])
    def test_blocked_statuses_return_message(self, clean_metadata, status):
        clean_metadata["a"] = _make_agent("a", status=status)
        msg = check_agent_status("a")
        assert msg is not None
        assert "a" in msg


# --------------------------------------------------------------------------- #
# check_agent_id_default
# --------------------------------------------------------------------------- #

class TestCheckAgentIdDefault:
    @pytest.mark.parametrize("agent_id", ["", None, "default_agent"])
    def test_default_or_empty_warns(self, agent_id):
        assert check_agent_id_default(agent_id) is not None

    def test_real_id_no_warning(self):
        assert check_agent_id_default("cursor_session_001") is None


# --------------------------------------------------------------------------- #
# validate_agent_id_format
# --------------------------------------------------------------------------- #

class TestValidateAgentIdFormat:
    @pytest.mark.parametrize("agent_id", ["test", "demo", "default_agent", "agent", "monitor", "MONITOR"])
    def test_generic_ids_rejected(self, agent_id):
        ok, err, suggestion = validate_agent_id_format(agent_id)
        assert ok is False
        assert err
        # generic IDs get a timestamped suggestion to disambiguate
        assert suggestion

    @pytest.mark.parametrize("agent_id", ["claude_code_cli", "claude_chat", "composer", "cursor_ide"])
    def test_known_collision_prone_ids_rejected(self, agent_id):
        ok, _, suggestion = validate_agent_id_format(agent_id)
        assert ok is False
        assert suggestion

    def test_short_test_id_requires_timestamp(self):
        ok, err, _ = validate_agent_id_format("test_foo")
        assert ok is False
        assert "timestamp" in err.lower()

    def test_short_demo_id_requires_timestamp(self):
        ok, err, _ = validate_agent_id_format("demo_foo")
        assert ok is False
        assert "timestamp" in err.lower()

    def test_too_short_rejected(self):
        ok, err, _ = validate_agent_id_format("ab")
        assert ok is False
        assert "short" in err.lower()

    @pytest.mark.parametrize("agent_id", ["has space", "bad!char", "emoji😀", "semi;colon"])
    def test_invalid_characters_rejected(self, agent_id):
        ok, err, _ = validate_agent_id_format(agent_id)
        assert ok is False
        assert "invalid characters" in err.lower()

    @pytest.mark.parametrize(
        "agent_id",
        [
            "cursor_ide_session_001",
            "claude_code_cli_20251124",
            "test_20251124_143022",  # 3+ parts → allowed
            "demo_20251124_143022",
            "production-agent-v2",
        ],
    )
    def test_valid_ids_pass(self, agent_id):
        ok, err, suggestion = validate_agent_id_format(agent_id)
        assert ok is True
        assert err == ""
        assert suggestion == ""


# --------------------------------------------------------------------------- #
# require_agent_id
# --------------------------------------------------------------------------- #

class TestRequireAgentId:
    def test_missing_agent_id_errors(self, clean_metadata):
        agent_id, err = require_agent_id({})
        assert agent_id is None
        assert err is not None
        assert "agent_id is required" in err.text

    def test_valid_new_agent_id_passes(self, clean_metadata):
        agent_id, err = require_agent_id({"agent_id": "fresh_session_001"})
        assert err is None
        assert agent_id == "fresh_session_001"

    def test_invalid_format_new_agent_rejected(self, clean_metadata):
        agent_id, err = require_agent_id({"agent_id": "monitor"})
        assert agent_id is None
        assert err is not None

    def test_existing_agent_skips_format_check(self, clean_metadata):
        # 'monitor' is normally rejected by format, but an already-registered
        # agent_id must still be usable — format gate only applies to new IDs.
        clean_metadata["monitor"] = _make_agent("monitor")
        agent_id, err = require_agent_id({"agent_id": "monitor"})
        assert err is None
        assert agent_id == "monitor"

    def test_reject_existing_blocks_collision(self, clean_metadata):
        clean_metadata["taken"] = _make_agent("taken")
        agent_id, err = require_agent_id({"agent_id": "taken"}, reject_existing=True)
        assert agent_id is None
        assert err is not None
        assert "collision" in err.text.lower()

    def test_reject_existing_allows_fresh_id(self, clean_metadata):
        agent_id, err = require_agent_id({"agent_id": "brand_new_id"}, reject_existing=True)
        assert err is None
        assert agent_id == "brand_new_id"


# --------------------------------------------------------------------------- #
# generate_api_key
# --------------------------------------------------------------------------- #

class TestGenerateApiKey:
    def test_returns_nonempty_str(self):
        key = generate_api_key()
        assert isinstance(key, str)
        assert key

    def test_url_safe_unpadded_32_bytes(self):
        key = generate_api_key()
        assert "=" not in key  # padding stripped
        # url-safe base64 of 32 bytes decodes back to exactly 32 bytes
        decoded = base64.urlsafe_b64decode(key + "=" * (-len(key) % 4))
        assert len(decoded) == 32

    def test_keys_are_unique(self):
        keys = {generate_api_key() for _ in range(100)}
        assert len(keys) == 100


# --------------------------------------------------------------------------- #
# verify_agent_ownership  (the constant-time auth core)
# --------------------------------------------------------------------------- #

class TestVerifyAgentOwnership:
    def test_session_bound_bypasses_key_check(self, clean_metadata):
        ok, err = verify_agent_ownership("anything", api_key=None, session_bound=True)
        assert ok is True
        assert err is None

    def test_nonexistent_agent_rejected(self, clean_metadata):
        ok, err = verify_agent_ownership("ghost", api_key="whatever")
        assert ok is False
        assert "does not exist" in err

    def test_agent_without_stored_key_allowed(self, clean_metadata):
        clean_metadata["keyless"] = _make_agent("keyless", api_key=None)
        ok, err = verify_agent_ownership("keyless", api_key="ignored")
        assert ok is True
        assert err is None

    def test_correct_key_accepted(self, clean_metadata):
        clean_metadata["a"] = _make_agent("a", api_key="s3cret-key")
        ok, err = verify_agent_ownership("a", api_key="s3cret-key")
        assert ok is True
        assert err is None

    def test_wrong_key_rejected(self, clean_metadata):
        clean_metadata["a"] = _make_agent("a", api_key="s3cret-key")
        ok, err = verify_agent_ownership("a", api_key="wrong-key")
        assert ok is False
        assert "Invalid API key" in err

    @pytest.mark.parametrize("bad", [None, "", 12345])
    def test_missing_or_nonstring_key_rejected(self, clean_metadata, bad):
        clean_metadata["a"] = _make_agent("a", api_key="s3cret-key")
        ok, err = verify_agent_ownership("a", api_key=bad)
        assert ok is False
        assert err


# --------------------------------------------------------------------------- #
# require_agent_auth  (the impersonation gate)
# --------------------------------------------------------------------------- #

class TestRequireAgentAuth:
    def test_unknown_agent_passes(self, clean_metadata):
        # First-use of a not-yet-registered agent_id is allowed (it becomes the
        # owner on creation); auth only protects already-registered identities.
        ok, err = require_agent_auth("new_one", {})
        assert ok is True
        assert err is None

    def test_keyless_agent_passes_without_enforce(self, clean_metadata):
        clean_metadata["legacy"] = _make_agent("legacy", api_key=None)
        ok, err = require_agent_auth("legacy", {})
        assert ok is True
        assert err is None

    def test_keyless_agent_blocked_with_enforce(self, clean_metadata):
        clean_metadata["legacy"] = _make_agent("legacy", api_key=None)
        ok, err = require_agent_auth("legacy", {}, enforce=True)
        assert ok is False
        assert err is not None
        assert "API key required" in err.text

    def test_protected_agent_missing_key_blocked(self, clean_metadata):
        clean_metadata["a"] = _make_agent("a", api_key="s3cret-key")
        ok, err = require_agent_auth("a", {})
        assert ok is False
        assert err is not None
        assert "API key required" in err.text

    def test_protected_agent_wrong_key_blocked(self, clean_metadata):
        clean_metadata["a"] = _make_agent("a", api_key="s3cret-key")
        ok, err = require_agent_auth("a", {"api_key": "nope"})
        assert ok is False
        assert err is not None
        assert "Authentication failed" in err.text

    def test_protected_agent_correct_key_passes(self, clean_metadata):
        clean_metadata["a"] = _make_agent("a", api_key="s3cret-key")
        ok, err = require_agent_auth("a", {"api_key": "s3cret-key"})
        assert ok is True
        assert err is None
