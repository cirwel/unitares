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


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_a_fifo_at_the_key_path_is_refused_without_blocking(secret_path):
    secret_path.parent.mkdir(parents=True)
    os.mkfifo(secret_path, 0o600)

    assert session_mod._get_continuity_secret() is None
    with pytest.raises(OSError, match="not a regular file"):
        cs.ensure_generated_secret()


def test_stdio_startup_creates_the_key(secret_path, monkeypatch):
    import asyncio

    import src.mcp_server_std as std

    monkeypatch.setattr(std, "STDIO_PROXY_URL", None, raising=False)
    monkeypatch.setattr(std, "STDIO_PROXY_HTTP_URL", None, raising=False)
    monkeypatch.setattr(std, "init_server_process", lambda: None)

    async def stop_after_the_probe():
        raise SystemExit("probe reached")

    monkeypatch.setattr(std, "probe_identity_continuity_status", stop_after_the_probe)
    with pytest.raises(SystemExit, match="probe reached"):
        asyncio.run(std.main())

    assert session_mod._get_continuity_secret()


def _open_fds() -> int:
    return len(os.listdir("/dev/fd"))


def test_a_directory_at_the_key_path_is_refused_without_leaking(secret_path):
    secret_path.mkdir(parents=True)
    before = _open_fds()

    for _ in range(5):
        assert session_mod._get_continuity_secret() is None
        assert session_mod.continuity_token_support_status() == {"enabled": False, "secret_source": None}
    with pytest.raises(OSError, match="not a regular file"):
        cs.ensure_generated_secret()

    assert _open_fds() == before


def test_a_refused_key_file_does_not_leak_descriptors(secret_path):
    cs.ensure_generated_secret()
    os.chmod(secret_path, 0o644)
    before = _open_fds()

    for _ in range(5):
        assert session_mod._get_continuity_secret() is None

    assert _open_fds() == before


# ── a running server never loses its key ────────────────────────────────────


def test_a_server_holds_its_key_when_the_file_is_deleted(secret_path):
    from src.mcp_handlers.identity import stable_session

    cs.hold_for_server()
    key = session_mod._get_continuity_secret()
    tag_key = stable_session.csid_key()
    token = session_mod.create_continuity_token(_UUID, _SID)
    secret_path.unlink()

    assert key
    assert session_mod._get_continuity_secret() == key
    assert stable_session.csid_key() == tag_key
    assert session_mod.resolve_continuity_token(token) == _SID


def test_a_server_holds_its_key_when_the_file_is_opened_up(secret_path):
    from src.mcp_handlers.identity import stable_session

    cs.hold_for_server()
    key = session_mod._get_continuity_secret()
    tag_key = stable_session.csid_key()
    os.chmod(secret_path, 0o644)

    assert session_mod._get_continuity_secret() == key
    assert stable_session.csid_key() == tag_key


def test_a_server_holds_a_configured_key(secret_path, monkeypatch):
    monkeypatch.setenv(cs.ENV, "operator-chosen")
    cs.hold_for_server()
    monkeypatch.delenv(cs.ENV)

    assert session_mod._get_continuity_secret() == b"operator-chosen"
    assert not secret_path.exists()


def test_a_server_without_a_usable_key_raises(secret_path):
    secret_path.mkdir(parents=True)

    with pytest.raises(OSError, match="not a regular file"):
        cs.hold_for_server()
    assert session_mod._get_continuity_secret() is None


def test_running_without_a_key_needs_the_explicit_override(monkeypatch):
    monkeypatch.delenv(cs.ALLOW_NO_SECRET_ENV, raising=False)
    assert not cs.no_secret_allowed()
    for value in ("1", "true", "yes", "on"):
        assert cs.no_secret_allowed({cs.ALLOW_NO_SECRET_ENV: value})
    assert not cs.no_secret_allowed({cs.ALLOW_NO_SECRET_ENV: "0"})


def _run_stdio_to_the_probe(monkeypatch):
    import asyncio

    import src.mcp_server_std as std

    monkeypatch.setattr(std, "STDIO_PROXY_URL", None, raising=False)
    monkeypatch.setattr(std, "STDIO_PROXY_HTTP_URL", None, raising=False)
    monkeypatch.setattr(std, "init_server_process", lambda: None)

    async def stop_after_the_probe():
        raise SystemExit("probe reached")

    monkeypatch.setattr(std, "probe_identity_continuity_status", stop_after_the_probe)
    with pytest.raises(SystemExit) as exc:
        asyncio.run(std.main())
    return exc.value.code


def test_stdio_startup_refuses_without_a_usable_key(secret_path, monkeypatch):
    monkeypatch.delenv(cs.ALLOW_NO_SECRET_ENV, raising=False)
    secret_path.mkdir(parents=True)

    assert _run_stdio_to_the_probe(monkeypatch) == 1


def test_stdio_startup_runs_keyless_only_with_the_override(secret_path, monkeypatch):
    monkeypatch.setenv(cs.ALLOW_NO_SECRET_ENV, "1")
    secret_path.mkdir(parents=True)

    assert _run_stdio_to_the_probe(monkeypatch) == "probe reached"
    assert session_mod._get_continuity_secret() is None


def _proxied_stdio_to_the_server(monkeypatch, *, strict):
    import asyncio

    import src.mcp_server_std as std

    monkeypatch.setattr(std, "STDIO_PROXY_URL", "http://127.0.0.1:1/mcp/", raising=False)
    monkeypatch.setattr(std, "STDIO_PROXY_HTTP_URL", None, raising=False)
    monkeypatch.setattr(std, "STDIO_PROXY_STRICT", strict, raising=False)
    monkeypatch.setattr(std, "init_server_process", lambda: None)
    monkeypatch.setattr(std, "remove_pid_file", lambda: None)

    def stop_at_the_transport():
        raise SystemExit("stdio reached")

    monkeypatch.setattr(std, "stdio_server", stop_at_the_transport)
    with pytest.raises(SystemExit) as exc:
        asyncio.run(std.main())
    return exc.value.code


def test_stdio_with_a_fallback_proxy_refuses_without_a_usable_key(secret_path, monkeypatch):
    # A non-strict proxy runs the local handlers when the upstream fails.
    monkeypatch.delenv(cs.ALLOW_NO_SECRET_ENV, raising=False)
    secret_path.mkdir(parents=True)

    assert _proxied_stdio_to_the_server(monkeypatch, strict=False) == 1


def test_stdio_with_a_strict_proxy_needs_no_local_key(secret_path, monkeypatch):
    monkeypatch.delenv(cs.ALLOW_NO_SECRET_ENV, raising=False)
    secret_path.mkdir(parents=True)

    assert _proxied_stdio_to_the_server(monkeypatch, strict=True) == "stdio reached"


class _BootstrapReached(Exception):
    pass


async def _run_http_main(monkeypatch):
    from types import SimpleNamespace

    from src import mcp_server

    # Importing the server may load a .env; the fixture's key state must win.
    monkeypatch.delenv(cs.ENV, raising=False)
    bootstrapped = []

    async def _bootstrap(**_kwargs):
        bootstrapped.append(True)
        raise _BootstrapReached()

    monkeypatch.setattr("src.services.mcp_server_bootstrap.bootstrap_server", _bootstrap)
    monkeypatch.setattr("src.insecure_defaults.published_default_refusal", lambda: None)
    monkeypatch.setattr(mcp_server, "_OAUTH_GATE_REQUIRED", False)
    monkeypatch.setattr(mcp_server, "_auth_settings", None)
    monkeypatch.setattr(
        mcp_server,
        "parse_args",
        lambda: SimpleNamespace(host="127.0.0.1", port=8767, force=False, reload=False),
    )
    try:
        await mcp_server.main()
    except SystemExit as exc:
        return exc.code, bootstrapped
    except _BootstrapReached:
        return "bootstrap reached", bootstrapped
    return None, bootstrapped


@pytest.mark.asyncio
async def test_http_startup_refuses_without_a_key_before_bootstrap(secret_path, monkeypatch):
    # Bootstrap SIGTERMs a running predecessor, so the refusal must come first.
    monkeypatch.delenv(cs.ALLOW_NO_SECRET_ENV, raising=False)
    secret_path.mkdir(parents=True)

    code, bootstrapped = await _run_http_main(monkeypatch)

    assert code == 1
    assert bootstrapped == []


@pytest.mark.asyncio
async def test_http_startup_runs_keyless_only_with_the_override(secret_path, monkeypatch):
    monkeypatch.setenv(cs.ALLOW_NO_SECRET_ENV, "1")
    secret_path.mkdir(parents=True)

    code, bootstrapped = await _run_http_main(monkeypatch)

    assert code == "bootstrap reached"
    assert bootstrapped == [True]
    assert session_mod._get_continuity_secret() is None


@pytest.mark.asyncio
async def test_http_startup_holds_a_usable_key_and_proceeds(secret_path, monkeypatch):
    monkeypatch.delenv(cs.ALLOW_NO_SECRET_ENV, raising=False)

    code, bootstrapped = await _run_http_main(monkeypatch)

    assert code == "bootstrap reached"
    key = session_mod._get_continuity_secret()
    assert key
    secret_path.unlink()
    assert session_mod._get_continuity_secret() == key
