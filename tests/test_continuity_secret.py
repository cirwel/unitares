"""The continuity token secret: configured, generated, never the API token.

A continuity token proves ownership of an agent, so its signing key must be
private to the server. The HTTP API token used to be the fallback (every holder
of that bearer could forge ownership proofs), and docker-compose.yml used to
supply a published default.
"""

import os
import stat

import pytest

from src import continuity_secret as cs
from src.insecure_defaults import startup_warnings
from src.mcp_handlers.identity import session as session_mod

_RETIRED_DEFAULT = "unitares-local-continuity-secret"
_UUID = "11111111-2222-4333-8444-555555555555"
_SID = "agent-111111112222"


@pytest.fixture
def secret_path(tmp_path, monkeypatch):
    path = tmp_path / "secrets" / "continuity_token_secret"
    monkeypatch.setenv(cs.FILE_ENV, str(path))
    monkeypatch.delenv(cs.ENV, raising=False)
    return path


def test_the_retired_compose_default_is_recognised():
    assert cs.is_retired_compose_default(_RETIRED_DEFAULT)
    assert not cs.is_retired_compose_default("operator-chosen")


def test_the_http_api_token_is_never_the_signing_key(secret_path, monkeypatch):
    monkeypatch.setenv("UNITARES_HTTP_API_TOKEN", "bearer-anyone-with-api-access-holds")
    monkeypatch.setenv("UNITARES_API_TOKEN", "another-bearer")

    assert session_mod._get_continuity_secret() is None
    assert session_mod.create_continuity_token(_UUID, _SID) is None
    assert session_mod.continuity_token_support_status() == {"enabled": False, "secret_source": None}


def test_a_configured_secret_wins(secret_path, monkeypatch):
    monkeypatch.setenv(cs.ENV, "operator-chosen")
    cs.ensure_generated_secret()

    assert not secret_path.exists()
    assert session_mod._get_continuity_secret() == b"operator-chosen"
    assert session_mod.continuity_token_support_status()["secret_source"] == cs.ENV


@pytest.mark.parametrize("value", [_RETIRED_DEFAULT, "", "   "])
def test_the_published_default_and_blank_values_fall_back_to_a_generated_key(
    secret_path, monkeypatch, value,
):
    monkeypatch.setenv(cs.ENV, value)
    cs.ensure_generated_secret()

    key = session_mod._get_continuity_secret()
    assert key and key != _RETIRED_DEFAULT.encode()
    assert session_mod.continuity_token_support_status()["secret_source"] == "generated"


def test_a_token_signed_with_the_published_default_does_not_verify(secret_path, monkeypatch):
    # What an attacker who read docker-compose.yml could sign.
    with monkeypatch.context() as m:
        m.setattr(session_mod, "_get_continuity_secret", lambda: _RETIRED_DEFAULT.encode())
        forged = session_mod.create_continuity_token(_UUID, _SID)
    monkeypatch.setenv(cs.ENV, _RETIRED_DEFAULT)
    cs.ensure_generated_secret()

    assert forged
    assert session_mod.resolve_continuity_token(forged) is None


def test_the_generated_key_is_private_stable_and_survives_restart(secret_path):
    assert cs.ensure_generated_secret() == secret_path
    first = secret_path.read_text()

    assert stat.S_IMODE(os.stat(secret_path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(secret_path.parent).st_mode) == 0o700
    assert len(first.strip()) == 64

    assert cs.ensure_generated_secret() == secret_path  # a restart keeps it
    assert secret_path.read_text() == first

    token = session_mod.create_continuity_token(_UUID, _SID)
    assert session_mod.resolve_continuity_token(token) == _SID


def test_deleting_the_generated_key_rotates_it(secret_path):
    cs.ensure_generated_secret()
    token = session_mod.create_continuity_token(_UUID, _SID)
    secret_path.unlink()
    cs.ensure_generated_secret()

    assert session_mod.resolve_continuity_token(token) is None


def test_lookup_never_creates_the_key(secret_path):
    assert session_mod._get_continuity_secret() is None
    assert not secret_path.exists()


def test_an_unwritable_location_raises(tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    monkeypatch.setenv(cs.FILE_ENV, str(blocker / "continuity_token_secret"))
    monkeypatch.delenv(cs.ENV, raising=False)

    with pytest.raises(OSError):
        cs.ensure_generated_secret()


def test_startup_warns_about_the_retired_default():
    [warning] = startup_warnings(
        rest_strict=False, in_container=False, environ={cs.ENV: _RETIRED_DEFAULT},
    )
    assert cs.ENV in warning and "ignores" in warning
    assert startup_warnings(rest_strict=False, in_container=False, environ={cs.ENV: "mine"}) == []


def test_a_configured_secret_is_used_byte_for_byte(secret_path, monkeypatch):
    # Upgrading must not change an existing deployment's key.
    monkeypatch.setenv(cs.ENV, "  operator-key  ")
    assert session_mod._get_continuity_secret() == b"  operator-key  "


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644])
def test_a_key_file_open_to_other_users_is_refused(secret_path, mode):
    cs.ensure_generated_secret()
    os.chmod(secret_path, mode)

    assert session_mod._get_continuity_secret() is None
    with pytest.raises(OSError, match="readable by other users"):
        cs.ensure_generated_secret()


def test_a_symlinked_key_file_is_refused(secret_path, tmp_path):
    target = tmp_path / "elsewhere"
    target.write_text("a" * 64)
    os.chmod(target, 0o600)
    secret_path.parent.mkdir(parents=True)
    secret_path.symlink_to(target)

    assert session_mod._get_continuity_secret() is None
    with pytest.raises(OSError):
        cs.ensure_generated_secret()
