"""Tests for SDK utilities."""

import base64
import json

import pytest

from unitares_sdk.utils import (
    atomic_write,
    load_json_state,
    notify,
    parse_continuity_token,
    save_json_state,
    validate_token_uuid,
)


# --- atomic_write ---


def test_atomic_write_creates_file(tmp_path):
    path = tmp_path / "test.json"
    atomic_write(path, '{"key": "value"}')
    assert path.read_text() == '{"key": "value"}'


def test_atomic_write_overwrites_existing(tmp_path):
    path = tmp_path / "test.json"
    path.write_text("old")
    atomic_write(path, "new")
    assert path.read_text() == "new"


def test_atomic_write_creates_parent_dirs(tmp_path):
    path = tmp_path / "sub" / "dir" / "test.json"
    atomic_write(path, "data")
    assert path.read_text() == "data"


def test_atomic_write_produces_mode_0600_by_default(tmp_path):
    """Anchors and session files hold continuity tokens — must be 0600
    against same-UID sibling processes reading them."""
    import os
    import stat as _stat

    path = tmp_path / "secret.json"
    atomic_write(path, '{"token": "secret"}')
    assert _stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_atomic_write_overwrite_preserves_mode_0600(tmp_path):
    """Overwriting a previously-loose file tightens it — the old file's
    permissions do not leak through."""
    import os
    import stat as _stat

    path = tmp_path / "secret.json"
    path.write_text("old")
    os.chmod(path, 0o644)
    atomic_write(path, "new")
    assert _stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_atomic_write_honors_explicit_mode(tmp_path):
    """Caller can opt into a different mode when the file genuinely needs
    to be world-readable (not the common case)."""
    import os
    import stat as _stat

    path = tmp_path / "public.json"
    atomic_write(path, "data", mode=0o644)
    assert _stat.S_IMODE(os.stat(path).st_mode) == 0o644


# --- load_json_state / save_json_state ---


def test_load_missing_file(tmp_path):
    assert load_json_state(tmp_path / "nope.json") == {}


def test_load_corrupt_json(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not valid json")
    assert load_json_state(path) == {}


def test_load_dict_format(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"client_session_id": "abc123", "continuity_token": "tok"}')
    result = load_json_state(path)
    assert result["client_session_id"] == "abc123"
    assert result["continuity_token"] == "tok"


def test_load_legacy_bare_string(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('"abc123"')
    result = load_json_state(path)
    assert result == {"client_session_id": "abc123"}


def test_load_legacy_plain_text(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("abc123")
    result = load_json_state(path)
    assert result == {"client_session_id": "abc123"}


def test_load_rejects_list(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("[1, 2, 3]")
    assert load_json_state(path) == {}


def test_load_rejects_null(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("null")
    assert load_json_state(path) == {}


def test_save_and_load_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    save_json_state(path, {"client_session_id": "s1", "continuity_token": "t1"})
    result = load_json_state(path)
    assert result["client_session_id"] == "s1"
    assert result["continuity_token"] == "t1"


def test_save_json_state_coerces_non_serializable(tmp_path):
    """save_json_state uses default=str, so datetime/Path values persist as strings."""
    from datetime import datetime, timezone
    from pathlib import Path

    state = {
        "when": datetime(2026, 4, 24, 12, 0, 0, tzinfo=timezone.utc),
        "path": Path("/tmp/example"),
        "ok": True,
    }
    target = tmp_path / "state.json"
    save_json_state(target, state)

    loaded = load_json_state(target)
    # datetime/Path coerced to str on write; load gets strings back.
    assert isinstance(loaded["when"], str)
    assert loaded["when"].startswith("2026-04-24")
    assert isinstance(loaded["path"], str)
    assert loaded["path"] == "/tmp/example"
    assert loaded["ok"] is True


# --- parse_continuity_token ---


def _make_token(payload: dict, version: str = "v1") -> str:
    """Build a fake v1.<payload>.<sig> token for testing."""
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{version}.{encoded}.fakesig"


def test_parse_valid_token():
    payload = {"aid": "uuid-1234", "model": "test", "exp": 9999999999}
    token = _make_token(payload)
    result = parse_continuity_token(token)
    assert result is not None
    assert result["aid"] == "uuid-1234"
    assert result["model"] == "test"


def test_parse_malformed_token():
    assert parse_continuity_token("not.a.valid.token") is None
    assert parse_continuity_token("v2.abc.def") is None
    assert parse_continuity_token("v1.!!!.sig") is None
    assert parse_continuity_token("garbage") is None
    assert parse_continuity_token("") is None


def test_parse_non_dict_payload():
    encoded = base64.urlsafe_b64encode(b'"just a string"').decode().rstrip("=")
    token = f"v1.{encoded}.sig"
    assert parse_continuity_token(token) is None


# --- validate_token_uuid ---


def test_validate_matching_uuid():
    token = _make_token({"aid": "uuid-1234"})
    assert validate_token_uuid(token, "uuid-1234") is True


def test_validate_mismatched_uuid():
    token = _make_token({"aid": "uuid-1234"})
    assert validate_token_uuid(token, "uuid-5678") is False


def test_validate_no_aid_in_token():
    token = _make_token({"model": "test"})
    assert validate_token_uuid(token, "uuid-1234") is False


def test_validate_malformed_token():
    assert validate_token_uuid("garbage", "uuid-1234") is False


# --- notify ---


def test_notify_does_not_raise():
    """notify is best-effort; should never raise regardless of platform."""
    notify("Test", "This is a test notification")


def test_notify_escapes_applescript_metacharacters(monkeypatch):
    """Exception text reaches notify(); a quote in it must not break out of
    the AppleScript string literal (injection on the operator's Mac)."""
    import subprocess
    import sys

    import unitares_sdk.utils as utils

    captured = []
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        subprocess, "Popen", lambda args, **kw: captured.append(args)
    )

    utils.notify('Ti"tle', 'pwned" with title "x')

    assert captured, "notify should have invoked osascript"
    script = captured[0][2]
    # Every interior quote is escaped — the only unescaped quotes are the
    # literal delimiters of the AppleScript string.
    assert 'Ti\\"tle' in script
    assert 'pwned\\" with title \\"x' in script


def test_notify_bounds_message_length(monkeypatch):
    import subprocess
    import sys

    import unitares_sdk.utils as utils

    captured = []
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        subprocess, "Popen", lambda args, **kw: captured.append(args)
    )

    utils.notify("T", "x" * 10_000)

    script = captured[0][2]
    assert len(script) < 1_000


def test_applescript_escape_backslash_then_quote():
    from unitares_sdk.utils import _applescript_escape

    # Backslash escaped first, so a pre-escaped \" cannot sneak through.
    assert _applescript_escape('\\"') == '\\\\\\"'


# --- atomic_write failure propagation ---


def test_atomic_write_raises_on_failure_and_preserves_target(tmp_path, monkeypatch):
    """A failed anchor write must be loud: silently swallowing it means the
    next restart loses identity (the 2026-04-19 silent-fork class). The
    existing target and temp-file hygiene survive the failure."""
    import os as os_mod

    target = tmp_path / "anchor.json"
    target.write_text('{"agent_uuid": "original"}')

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os_mod, "replace", boom)

    with pytest.raises(OSError):
        atomic_write(target, '{"agent_uuid": "new"}')

    assert target.read_text() == '{"agent_uuid": "original"}'
    assert not list(tmp_path.glob("*.tmp")), "temp file must be cleaned up"


def test_atomic_write_creates_private_parent_dirs(tmp_path):
    """Anchor/state dirs are agent-private — created 0o700, not umask default."""
    target = tmp_path / "anchors" / "deep" / "anchor.json"
    atomic_write(target, "x")
    for d in (tmp_path / "anchors", tmp_path / "anchors" / "deep"):
        assert (d.stat().st_mode & 0o777) == 0o700


# --- load_json_state corruption visibility ---


def test_load_corrupt_json_logs_warning(tmp_path, caplog):
    """A corrupt anchor silently becoming {} is the first step of a silent
    identity fork — the corruption must be visible in the log."""
    import logging

    path = tmp_path / "state.json"
    path.write_text('{"agent_uuid": truncated-mid-wri')

    with caplog.at_level(logging.WARNING, logger="unitares_sdk.utils"):
        assert load_json_state(path) == {}

    assert any("corrupt" in rec.message for rec in caplog.records)
