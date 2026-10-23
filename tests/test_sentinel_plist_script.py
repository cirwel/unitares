"""Regression coverage for the Sentinel authenticated-UDS plist workflow."""

from __future__ import annotations

import json
import os
import plistlib
import socket
import stat
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts/ops/sentinel-plist.py"
ROTATE_SCRIPT = REPO / "scripts/ops/rotate-secrets.sh"
TEMPLATE = REPO / "scripts/ops/com.unitares.sentinel-beam.plist.template"


def _write_plist(
    path: Path,
    environment: dict[str, str],
    *,
    root: str = "/srv/unitares",
    mode: int = 0o600,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": "com.unitares.sentinel-beam",
        "ProgramArguments": [
            "/bin/bash",
            f"{root}/elixir/sentinel/scripts/start.sh",
        ],
        "WorkingDirectory": f"{root}/elixir/sentinel",
        "EnvironmentVariables": environment,
    }
    with path.open("wb") as handle:
        plistlib.dump(payload, handle, sort_keys=False)
    path.chmod(mode)


# set-secret discovers secrets files from ~/Library/LaunchAgents, so a test
# that inherits the developer's HOME can rotate the developer's real secrets
# (it did, once, during development of this file). Every run gets a HOME that
# does not exist unless the test supplies its own.
ISOLATED_HOME = "/nonexistent/sentinel-plist-test-home"


def _run(*arguments: object, input_text: str | None = None, env=None):
    if env is None:
        env = _clean_env()
    assert env.get("HOME") != str(Path.home()), "tests must not run with the real HOME"
    return subprocess.run(
        [sys.executable, str(SCRIPT), *(str(argument) for argument in arguments)],
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def test_template_declares_bearer_and_per_user_uds_placeholders() -> None:
    with TEMPLATE.open("rb") as handle:
        payload = plistlib.load(handle)
    environment = payload["EnvironmentVariables"]

    assert environment["UNITARES_HTTP_API_TOKEN"] == "__UNITARES_HTTP_API_TOKEN__"
    assert environment["UNITARES_UDS_SOCKET"] == "__HOME__/.unitares/governance.sock"


def test_render_copies_current_bearer_without_logging_and_preserves_options(
    tmp_path: Path,
) -> None:
    home = tmp_path / "operator-home"
    root = tmp_path / "unitares-deploy"
    output = home / "Library/LaunchAgents/com.unitares.sentinel-beam.plist"
    governance = home / "Library/LaunchAgents/com.unitares.governance-mcp.plist"
    token = "current-secret-with-&-xml-characters"
    _write_plist(governance, {"UNITARES_HTTP_API_TOKEN": token})

    first = _run(
        "render",
        "--home",
        home,
        "--root",
        root,
        "--output",
        output,
        "--governance-plist",
        governance,
        "--audit-session",
        "agent-audit-session",
        "--enforced-surface-kinds",
        "resident",
    )
    assert first.returncode == 0, first.stderr
    assert token not in first.stdout + first.stderr
    assert stat.S_IMODE(output.stat().st_mode) == 0o600

    with output.open("rb") as handle:
        rendered = plistlib.load(handle)
    environment = rendered["EnvironmentVariables"]
    assert environment["UNITARES_HTTP_API_TOKEN"] == token
    assert environment["UNITARES_UDS_SOCKET"] == str(home / ".unitares/governance.sock")
    assert environment["UNITARES_SENTINEL_AUDIT_SESSION"] == "agent-audit-session"
    assert environment["LEASE_PLANE_ENFORCED_SURFACE_KINDS"] == "resident"
    assert rendered["WorkingDirectory"] == str(root / "elixir/sentinel")

    # A repair render can omit operational options without resetting the live
    # audit-session or lease-enforcement posture.
    second = _run(
        "render",
        "--home",
        home,
        "--root",
        root,
        "--output",
        output,
        "--governance-plist",
        governance,
    )
    assert second.returncode == 0, second.stderr
    with output.open("rb") as handle:
        rerendered = plistlib.load(handle)["EnvironmentVariables"]
    assert rerendered["UNITARES_SENTINEL_AUDIT_SESSION"] == "agent-audit-session"
    assert rerendered["LEASE_PLANE_ENFORCED_SURFACE_KINDS"] == "resident"
    assert token not in second.stdout + second.stderr


@pytest.mark.parametrize(
    ("sentinel_environment", "mode", "message"),
    [
        (
            {
                "UNITARES_HTTP_API_TOKEN": "stale-token",
                "UNITARES_UDS_SOCKET": "SOCKET",
            },
            0o600,
            "does not match the current governance bearer",
        ),
        (
            {"UNITARES_HTTP_API_TOKEN": "current-token"},
            0o600,
            "has no EnvironmentVariables:UNITARES_UDS_SOCKET",
        ),
        (
            {
                "UNITARES_HTTP_API_TOKEN": "current-token",
                "UNITARES_UDS_SOCKET": "SOCKET",
                "BROKEN": "__UNRESOLVED_VALUE__",
            },
            0o600,
            "contains unresolved placeholders",
        ),
        (
            {
                "UNITARES_HTTP_API_TOKEN": "current-token",
                "UNITARES_UDS_SOCKET": "SOCKET",
            },
            0o644,
            "mode is 0o644",
        ),
    ],
)
def test_check_refuses_stale_missing_unresolved_or_public_config(
    tmp_path: Path,
    sentinel_environment: dict[str, str],
    mode: int,
    message: str,
) -> None:
    socket_path = tmp_path / "home/.unitares/governance.sock"
    sentinel_environment = {
        key: (str(socket_path) if value == "SOCKET" else value)
        for key, value in sentinel_environment.items()
    }
    governance = tmp_path / "governance.plist"
    sentinel = tmp_path / "sentinel.plist"
    _write_plist(governance, {"UNITARES_HTTP_API_TOKEN": "current-token"})
    _write_plist(sentinel, sentinel_environment, mode=mode)

    result = _run(
        "check",
        "--plist",
        sentinel,
        "--governance-plist",
        governance,
        "--socket",
        socket_path,
    )

    assert result.returncode == 2
    assert message in result.stderr
    assert "current-token" not in result.stdout + result.stderr
    assert "stale-token" not in result.stdout + result.stderr


def test_render_refuses_missing_governance_bearer_without_creating_output(
    tmp_path: Path,
) -> None:
    governance = tmp_path / "governance.plist"
    output = tmp_path / "sentinel.plist"
    _write_plist(governance, {"UNITARES_HTTP_API_TOKEN": ""})

    result = _run(
        "render",
        "--root",
        tmp_path / "deploy",
        "--output",
        output,
        "--governance-plist",
        governance,
        "--socket",
        tmp_path / "governance.sock",
    )

    assert result.returncode == 2
    assert "no usable EnvironmentVariables:UNITARES_HTTP_API_TOKEN" in result.stderr
    assert not output.exists()


def test_rotate_token_requires_existing_key_and_reads_new_secret_from_stdin(
    tmp_path: Path,
) -> None:
    sentinel = tmp_path / "sentinel.plist"
    old_token = "old-sentinel-token"
    new_token = "new-sentinel-token"
    _write_plist(
        sentinel,
        {
            "UNITARES_HTTP_API_TOKEN": old_token,
            "UNITARES_UDS_SOCKET": str(tmp_path / "governance.sock"),
        },
    )

    result = _run("rotate-token", "--plist", sentinel, input_text=new_token)
    assert result.returncode == 0, result.stderr
    assert old_token not in result.stdout + result.stderr
    assert new_token not in result.stdout + result.stderr
    with sentinel.open("rb") as handle:
        environment = plistlib.load(handle)["EnvironmentVariables"]
    assert environment["UNITARES_HTTP_API_TOKEN"] == new_token
    assert stat.S_IMODE(sentinel.stat().st_mode) == 0o600

    missing = tmp_path / "missing-key.plist"
    _write_plist(missing, {"UNITARES_UDS_SOCKET": str(tmp_path / "governance.sock")})
    refused = _run("rotation-preflight", "--plist", missing)
    assert refused.returncode == 2
    assert "lacks a rotatable UNITARES_HTTP_API_TOKEN" in refused.stderr


def _serve_one_uds_response(
    listener: socket.socket,
    status: int,
    captured: list[bytes],
    errors: list[BaseException],
) -> None:
    try:
        connection, _ = listener.accept()
        with connection:
            request = b""
            while b"\r\n\r\n" not in request:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                request += chunk
            headers, _, body = request.partition(b"\r\n\r\n")
            content_length = 0
            for line in headers.split(b"\r\n")[1:]:
                key, _, value = line.partition(b":")
                if key.lower() == b"content-length":
                    content_length = int(value.strip())
            while len(body) < content_length:
                body += connection.recv(4096)
            captured.append(headers + b"\r\n\r\n" + body)

            response_payload = json.dumps(
                {
                    "success": True,
                    "result": {
                        "success": False,
                        "status": "identity_required",
                    },
                }
            ).encode()
            reason = b"OK" if status == 200 else b"Unauthorized"
            connection.sendall(
                b"HTTP/1.1 "
                + str(status).encode()
                + b" "
                + reason
                + b"\r\nContent-Type: application/json\r\nContent-Length: "
                + str(len(response_payload)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + response_payload
            )
    except BaseException as exc:  # surfaced in the test thread below
        errors.append(exc)
    finally:
        listener.close()


@pytest.mark.parametrize("status,expected_code", [(200, 0), (401, 2)])
def test_probe_uses_bearer_over_uds_and_distinguishes_outer_401(
    tmp_path: Path, status: int, expected_code: int
) -> None:
    token = "probe-secret-token"
    # Darwin caps sockaddr_un.sun_path at 104 bytes; pytest's tmp path can be
    # longer before the socket filename is appended.
    short_socket_dir = tempfile.TemporaryDirectory(prefix="unitares-uds-", dir="/tmp")
    socket_path = Path(short_socket_dir.name) / "governance.sock"
    governance = tmp_path / f"governance-{status}.plist"
    sentinel = tmp_path / f"sentinel-{status}.plist"
    _write_plist(governance, {"UNITARES_HTTP_API_TOKEN": token})
    _write_plist(
        sentinel,
        {
            "UNITARES_HTTP_API_TOKEN": token,
            "UNITARES_UDS_SOCKET": str(socket_path),
        },
    )

    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(1)
    captured: list[bytes] = []
    errors: list[BaseException] = []
    thread = threading.Thread(
        target=_serve_one_uds_response,
        args=(listener, status, captured, errors),
        daemon=True,
    )
    thread.start()

    result = _run(
        "probe",
        "--plist",
        sentinel,
        "--governance-plist",
        governance,
        "--socket",
        socket_path,
        "--timeout",
        "2",
    )
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert not errors
    assert result.returncode == expected_code, result.stderr
    assert token not in result.stdout + result.stderr
    assert len(captured) == 1
    request = captured[0]
    assert request.startswith(b"POST /v1/tools/call HTTP/1.1")
    assert f"Authorization: Bearer {token}".encode() in request
    assert json.loads(request.partition(b"\r\n\r\n")[2]) == {
        "name": "identity",
        "arguments": {},
    }
    if status == 200:
        assert "reached governance identity policy" in result.stdout
    else:
        assert "outer bearer gate (HTTP 401)" in result.stderr
    short_socket_dir.cleanup()


def _governance_without_bearer(path: Path) -> None:
    """A governance plist whose launcher loads the bearer from outside the
    plist, as a keychain launcher does: neither the key nor the wrapper."""
    _write_plist(path, {"HOME": "/srv/operator"})
    with path.open("rb") as handle:
        payload = plistlib.load(handle)
    payload["ProgramArguments"] = [
        "/srv/operator/bin/governance-launch",
        "/usr/bin/python3",
        "/srv/unitares/src/mcp_server.py",
    ]
    with path.open("wb") as handle:
        plistlib.dump(payload, handle, sort_keys=False)


@pytest.mark.parametrize("status,expected_code", [(200, 0), (401, 2)])
def test_check_asks_the_running_service_when_governance_keeps_no_bearer(
    tmp_path: Path, status: int, expected_code: int
) -> None:
    token = "sentinel-bearer-token"
    short_socket_dir = tempfile.TemporaryDirectory(prefix="unitares-uds-", dir="/tmp")
    socket_path = Path(short_socket_dir.name) / "governance.sock"
    governance = tmp_path / "governance.plist"
    sentinel = tmp_path / "sentinel.plist"
    _governance_without_bearer(governance)
    _write_plist(
        sentinel,
        {"UNITARES_HTTP_API_TOKEN": token, "UNITARES_UDS_SOCKET": str(socket_path)},
    )

    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(1)
    captured: list[bytes] = []
    errors: list[BaseException] = []
    thread = threading.Thread(
        target=_serve_one_uds_response,
        args=(listener, status, captured, errors),
        daemon=True,
    )
    thread.start()

    result = _run(
        "check", "--plist", sentinel, "--governance-plist", governance, "--socket", socket_path
    )
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert not errors
    assert result.returncode == expected_code, result.stderr
    assert token not in result.stdout + result.stderr
    assert len(captured) == 1
    # A read-only listing, never a tool call: the check touches no identity.
    assert captured[0].startswith(b"GET /v1/tools HTTP/1.1")
    assert f"Authorization: Bearer {token}".encode() in captured[0]
    if status == 401:
        assert "rejected the Sentinel bearer (HTTP 401)" in result.stderr
    short_socket_dir.cleanup()


def test_live_check_never_echoes_a_bearer_that_is_not_a_valid_header(
    tmp_path: Path,
) -> None:
    short_socket_dir = tempfile.TemporaryDirectory(prefix="unitares-uds-", dir="/tmp")
    socket_path = Path(short_socket_dir.name) / "governance.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(1)
    governance = tmp_path / "governance.plist"
    sentinel = tmp_path / "sentinel.plist"
    _governance_without_bearer(governance)
    _write_plist(
        sentinel,
        {"UNITARES_HTTP_API_TOKEN": "leaky\nbearer", "UNITARES_UDS_SOCKET": str(socket_path)},
    )

    result = _run(
        "check", "--plist", sentinel, "--governance-plist", governance, "--socket", socket_path
    )
    listener.close()
    short_socket_dir.cleanup()

    assert result.returncode == 2
    assert "is not a valid header value" in result.stderr
    assert "leaky" not in result.stdout + result.stderr


def test_check_refuses_when_governance_keeps_no_bearer_and_is_unreachable(
    tmp_path: Path,
) -> None:
    governance = tmp_path / "governance.plist"
    sentinel = tmp_path / "sentinel.plist"
    socket_path = tmp_path / "absent.sock"
    # No EnvironmentVariables at all is the same case as no bearer key.
    with governance.open("wb") as handle:
        plistlib.dump({"Label": "com.unitares.governance-mcp", "ProgramArguments": ["/srv/launch"]}, handle)
    _write_plist(
        sentinel,
        {"UNITARES_HTTP_API_TOKEN": "sentinel-bearer", "UNITARES_UDS_SOCKET": str(socket_path)},
    )

    result = _run(
        "check", "--plist", sentinel, "--governance-plist", governance, "--socket", socket_path
    )

    assert result.returncode == 2
    assert "could not be reached to check the Sentinel bearer" in result.stderr
    assert "sentinel-bearer" not in result.stdout + result.stderr


def test_render_refuses_an_unwrapped_sentinel_when_governance_keeps_no_bearer(
    tmp_path: Path,
) -> None:
    governance = tmp_path / "governance.plist"
    output = tmp_path / "sentinel.plist"
    _governance_without_bearer(governance)

    result = _run(
        "render",
        "--root",
        tmp_path / "deploy",
        "--output",
        output,
        "--governance-plist",
        governance,
        "--socket",
        tmp_path / "governance.sock",
    )

    assert result.returncode == 2
    assert "keeps no UNITARES_HTTP_API_TOKEN, so there is no bearer to copy" in result.stderr
    assert not output.exists()


def _write_fake_command(path: Path, body: str = "exit 0") -> None:
    path.write_text(f"#!/usr/bin/env bash\nset -euo pipefail\n{body}\n")
    path.chmod(0o755)


def _rotation_home(tmp_path: Path, *, include_sentinel_token: bool) -> tuple[Path, Path]:
    home = tmp_path / "home"
    anchors = home / ".unitares/anchors"
    anchors.mkdir(parents=True)
    (anchors / "sentinel.json").write_text(
        json.dumps(
            {
                "agent_uuid": "11111111-2222-4333-8444-555555555555",
                "client_session_id": "agent-old",
                "continuity_token": "continuity-old",
            }
        )
    )
    launchagents = home / "Library/LaunchAgents"
    governance = launchagents / "com.unitares.governance-mcp.plist"
    sentinel = launchagents / "com.unitares.sentinel-beam.plist"
    _write_plist(
        governance,
        {
            "UNITARES_HTTP_API_TOKEN": "old-governance-token",
            "UNITARES_CONTINUITY_TOKEN_SECRET": "old-continuity-secret",
        },
    )
    sentinel_environment = {
        "UNITARES_UDS_SOCKET": str(home / ".unitares/governance.sock")
    }
    if include_sentinel_token:
        sentinel_environment["UNITARES_HTTP_API_TOKEN"] = "old-sentinel-token"
    _write_plist(sentinel, sentinel_environment)
    return home, sentinel


def _rotation_env(home: Path, *, plist_buddy: Path, launchctl: Path) -> dict[str, str]:
    return {
        **os.environ,
        "HOME": str(home),
        "UNITARES_PLIST_BUDDY": str(plist_buddy),
        "UNITARES_LAUNCHCTL": str(launchctl),
        "UNITARES_SENTINEL_PLIST_TOOL": str(SCRIPT),
        # Pinned inside the test home, so a developer's own setting is never
        # the file a test rotates.
        "UNITARES_SECRETS_ENV": str(home / ".config/unitares/secrets.env"),
    }


WRAPPER = "/opt/example/bin/with-secrets"


def _wrap(path: Path, keys: str, *, secrets_file: Path | None = None) -> None:
    """Move a test plist onto the secrets wrapper, as the operator's
    externalizer does: the wrapper prefix goes on, the values come off.
    ``secrets_file`` sets the job's UNITARES_SECRETS_ENV, the wrapper's
    override of its default file."""
    with path.open("rb") as handle:
        payload = plistlib.load(handle)
    payload["ProgramArguments"] = [WRAPPER, "--keys", keys, "--", *payload["ProgramArguments"]]
    for key in keys.split(","):
        payload["EnvironmentVariables"].pop(key, None)
    payload["EnvironmentVariables"]["UNITARES_SECRETS_WRAPPER"] = WRAPPER
    if secrets_file is not None:
        payload["EnvironmentVariables"]["UNITARES_SECRETS_ENV"] = str(secrets_file)
    with path.open("wb") as handle:
        plistlib.dump(payload, handle, sort_keys=False)


def _clean_env(**overrides: str) -> dict[str, str]:
    env = {**os.environ, "HOME": ISOLATED_HOME, **overrides}
    for name in ("UNITARES_SECRETS_ENV", "UNITARES_SECRETS_WRAPPER"):
        if name not in overrides:
            env.pop(name, None)
    return env


def _write_secrets(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)


def test_check_and_probe_read_a_wrapped_bearer_from_the_secrets_file(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    sentinel = home / "Library/LaunchAgents/com.unitares.sentinel-beam.plist"
    governance = home / "Library/LaunchAgents/com.unitares.governance-mcp.plist"
    uds = home / ".unitares/governance.sock"
    secrets_file = home / ".config/operator/secrets.env"
    token = "wrapped-bearer-$with'quote"
    _write_plist(governance, {"UNITARES_HTTP_API_TOKEN": token})
    _write_plist(sentinel, {"UNITARES_HTTP_API_TOKEN": "x", "UNITARES_UDS_SOCKET": str(uds)}, mode=0o644)
    _wrap(sentinel, "UNITARES_MCP_BEARER_TOKEN,UNITARES_HTTP_API_TOKEN", secrets_file=secrets_file)
    encoded = token.replace("$", "\\$")
    _write_secrets(secrets_file, f'# fleet\nexport OTHER=1\nUNITARES_HTTP_API_TOKEN="{encoded}"\n')
    # start.sh would source this file first; the wrapper never reads it, so a
    # different value here must not change what the check sees.
    _write_secrets(home / ".config/unitares/secrets.env", "UNITARES_HTTP_API_TOKEN=start-sh-only\n")
    env = _clean_env(HOME=str(home))

    # The job's explicit file decides; a wrapped plist holds no bearer, so
    # its mode is not a secret's concern.
    ok = _run("check", "--home", home, env=env)
    assert ok.returncode == 0, ok.stderr
    assert token not in ok.stdout + ok.stderr

    # The job's UNITARES_SECRETS_ENV moves the wrapper, and the check with it.
    moved = tmp_path / "moved.env"
    _write_secrets(moved, "UNITARES_HTTP_API_TOKEN=elsewhere\n")
    _wrap(sentinel, "UNITARES_HTTP_API_TOKEN", secrets_file=moved)
    redirected = _run("check", "--home", home, env=env)
    assert redirected.returncode == 2
    assert "does not match the current governance bearer" in redirected.stderr
    _write_secrets(moved, f"UNITARES_HTTP_API_TOKEN='{token}'\n")
    assert _run("check", "--home", home, env=env).returncode == 0

    # A separately selected job file wins over the checking process's HOME.
    job_home = tmp_path / "job-home"
    _write_secrets(job_home / ".config/operator/secrets.env", "UNITARES_HTTP_API_TOKEN=job-home-value\n")
    _write_plist(sentinel, {"HOME": str(job_home), "UNITARES_UDS_SOCKET": str(uds)})
    _wrap(sentinel, "UNITARES_HTTP_API_TOKEN", secrets_file=job_home / ".config/operator/secrets.env")
    job = _run("check", "--home", home, env=env)
    assert job.returncode == 2
    assert "does not match the current governance bearer" in job.stderr

    # The wrapper takes an override literally (no ~), and a relative one would
    # depend on the job's cwd: both are refused rather than guessed.
    for override in ("~/private.env", "private.env"):
        _write_plist(sentinel, {"UNITARES_UDS_SOCKET": str(uds), "UNITARES_SECRETS_ENV": override})
        _wrap(sentinel, "UNITARES_HTTP_API_TOKEN")
        literal = _run("check", "--home", home, env=env)
        assert literal.returncode == 2
        assert "UNITARES_SECRETS_ENV must be an absolute path" in literal.stderr
    _write_plist(sentinel, {"UNITARES_HTTP_API_TOKEN": "x", "UNITARES_UDS_SOCKET": str(uds)})
    _wrap(sentinel, "UNITARES_MCP_BEARER_TOKEN,UNITARES_HTTP_API_TOKEN", secrets_file=secrets_file)
    _write_plist(sentinel, {"UNITARES_HTTP_API_TOKEN": "x", "UNITARES_UDS_SOCKET": str(uds)})
    _wrap(sentinel, "UNITARES_MCP_BEARER_TOKEN,UNITARES_HTTP_API_TOKEN", secrets_file=secrets_file)

    # The governance plist can be wrapped too: both sides read the one file.
    _wrap(governance, "UNITARES_HTTP_API_TOKEN", secrets_file=secrets_file)
    assert _run("check", "--home", home, env=env).returncode == 0

    _write_plist(governance, {"UNITARES_HTTP_API_TOKEN": "rotated-elsewhere"})
    stale = _run("check", "--home", home, env=env)
    assert stale.returncode == 2
    assert "does not match the current governance bearer" in stale.stderr

    _write_secrets(secrets_file, "OTHER=1\n")
    missing = _run("check", "--home", home, env=env)
    assert missing.returncode == 2
    assert "secrets file, which has no usable value" in missing.stderr

    _write_secrets(secrets_file, f"UNITARES_HTTP_API_TOKEN='{token}'\n", mode=0o644)
    public = _run("check", "--home", home, env=env)
    assert public.returncode == 2
    assert "readable by group or others" in public.stderr


def test_render_keeps_an_installed_wrapper_and_never_writes_the_bearer_back(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    root = tmp_path / "unitares-deploy"
    output = home / "Library/LaunchAgents/com.unitares.sentinel-beam.plist"
    governance = home / "Library/LaunchAgents/com.unitares.governance-mcp.plist"
    secrets_file = tmp_path / "secrets.env"
    token = "current-governance-bearer"
    _write_plist(governance, {"UNITARES_HTTP_API_TOKEN": token})
    _write_plist(output, {"UNITARES_HTTP_API_TOKEN": token})
    _wrap(output, "UNITARES_HTTP_API_TOKEN", secrets_file=secrets_file)
    _write_secrets(secrets_file, f"UNITARES_HTTP_API_TOKEN={token}\n")
    env = _clean_env(HOME=str(home))

    result = _run("render", "--home", home, "--root", root, env=env)
    assert result.returncode == 0, result.stderr
    with output.open("rb") as handle:
        rendered = plistlib.load(handle)
    assert "UNITARES_HTTP_API_TOKEN" not in rendered["EnvironmentVariables"]
    assert rendered["EnvironmentVariables"]["UNITARES_SECRETS_ENV"] == str(secrets_file)
    assert token.encode() not in output.read_bytes()
    assert rendered["ProgramArguments"][:4] == [WRAPPER, "--keys", "UNITARES_HTTP_API_TOKEN", "--"]
    assert rendered["ProgramArguments"][4:] == [
        "/bin/bash", str(root / "elixir/sentinel/scripts/start.sh")
    ]

    # The render preserves the installed job's explicit file, so a different
    # template --home cannot move the credential source.
    job_home = tmp_path / "job-home"
    job_file = job_home / ".config/operator/secrets.env"
    _write_secrets(job_file, f"UNITARES_HTTP_API_TOKEN={token}\n")
    _write_plist(output, {"HOME": str(job_home)})
    _wrap(output, "UNITARES_HTTP_API_TOKEN", secrets_file=job_file)
    pinned = _run("render", "--home", home, "--root", root, env=env)
    assert pinned.returncode == 0, pinned.stderr
    with output.open("rb") as handle:
        environment = plistlib.load(handle)["EnvironmentVariables"]
    assert environment["UNITARES_SECRETS_ENV"] == str(job_file)


def test_set_secret_rewrites_only_the_named_key(tmp_path: Path) -> None:
    secrets_file = tmp_path / "secrets.env"
    before = (
        "# operator secrets\n"
        "export UNITARES_HTTP_API_TOKEN='old-value'\n"
        'UNRELATED="keep \\"me\\" exactly"\n'
        "UNITARES_HTTP_API_TOKEN=older-duplicate\n"
    )
    _write_secrets(secrets_file, before)
    env = _clean_env(UNITARES_SECRETS_ENV=str(secrets_file))
    new_value = "new-value-with-'quote-and-$dollar"

    result = _run("set-secret", "--key", "UNITARES_HTTP_API_TOKEN", input_text=new_value, env=env)
    assert result.returncode == 0, result.stderr
    assert new_value not in result.stdout + result.stderr
    lines = secrets_file.read_text().splitlines()
    assert lines[0] == "# operator secrets"
    assert lines[2] == 'UNRELATED="keep \\"me\\" exactly"'
    assert lines[1].startswith("export UNITARES_HTTP_API_TOKEN=")
    assert stat.S_IMODE(secrets_file.stat().st_mode) == 0o600
    # Read back through the same grammar the wrapper uses: both definitions
    # now carry the new value, so the last-wins rule cannot resurrect the old.
    check = subprocess.run(
        ["bash", "-c", f'source "{secrets_file}"; printf %s "$UNITARES_HTTP_API_TOKEN"'],
        text=True, capture_output=True, check=True,
    )
    assert check.stdout == new_value

    skipped = _run("set-secret", "--key", "NOT_DEFINED", input_text="value-x", env=env)
    assert skipped.returncode == 0
    assert "no secrets file defines NOT_DEFINED" in skipped.stdout
    assert "NOT_DEFINED" not in secrets_file.read_text()

    # Both readers' files are rotated: the wrapper's and the one start.sh
    # sources. A file that does not define the key is left alone.
    sourced = tmp_path / "sourced.env"
    _write_secrets(sourced, "UNITARES_HTTP_API_TOKEN=start-sh-copy\n")
    home = tmp_path / "rotation-home"
    selected_job = home / "Library/LaunchAgents/selected.plist"
    _write_plist(selected_job, {"UNITARES_SECRETS_ENV": str(sourced)})
    both = _clean_env(HOME=str(home), UNITARES_SECRETS_ENV=str(secrets_file))
    listed = _run("secrets-files", env=both)
    assert listed.stdout.splitlines() == [str(secrets_file), str(sourced)]
    rotated = _run("set-secret", "--key", "UNITARES_HTTP_API_TOKEN", input_text="v2-value", env=both)
    assert rotated.returncode == 0, rotated.stderr
    assert sourced.read_text() == "UNITARES_HTTP_API_TOKEN='v2-value'\n"
    assert "export UNITARES_HTTP_API_TOKEN='v2-value'" in secrets_file.read_text()

    # Every file is checked before any is written.
    sourced.chmod(0o640)
    snapshot = secrets_file.read_bytes()
    refused = _run("set-secret", "--key", "UNITARES_HTTP_API_TOKEN", input_text="v3-value", env=both)
    assert refused.returncode == 2
    assert "readable by group or others" in refused.stderr
    assert secrets_file.read_bytes() == snapshot


def test_rotate_token_on_a_wrapped_plist_requires_the_secrets_file_first(
    tmp_path: Path,
) -> None:
    sentinel = tmp_path / "sentinel.plist"
    secrets_file = tmp_path / "secrets.env"
    _write_plist(sentinel, {"UNITARES_UDS_SOCKET": str(tmp_path / "governance.sock")})
    _wrap(sentinel, "UNITARES_HTTP_API_TOKEN", secrets_file=secrets_file)
    _write_secrets(secrets_file, "UNITARES_HTTP_API_TOKEN=old-bearer\n")
    env = _clean_env()
    before = sentinel.read_bytes()

    early = _run("rotate-token", "--plist", sentinel, input_text="new-bearer", env=env)
    assert early.returncode == 2
    assert "rotate the secrets file first" in early.stderr

    _write_secrets(secrets_file, "UNITARES_HTTP_API_TOKEN=new-bearer\n")
    done = _run("rotate-token", "--plist", sentinel, input_text="new-bearer", env=env)
    assert done.returncode == 0, done.stderr
    assert sentinel.read_bytes() == before


def test_rotate_script_rotates_the_secrets_file_for_wrapped_jobs(
    tmp_path: Path,
) -> None:
    home, sentinel = _rotation_home(tmp_path, include_sentinel_token=False)
    # The installed Sentinel points its wrapper at its own file; rotation must
    # find it from the plist, not only from this process's environment.
    custom = home / "sentinel-secrets.env"
    _write_secrets(custom, "UNITARES_HTTP_API_TOKEN=old-http-token\n")
    _wrap(sentinel, "UNITARES_HTTP_API_TOKEN", secrets_file=custom)
    # This process's wrapper default.
    secrets_file = home / ".config/operator/secrets.env"
    _write_secrets(
        secrets_file,
        "UNITARES_HTTP_API_TOKEN=old-http-token\n"
        "UNITARES_CONTINUITY_TOKEN_SECRET=old-continuity-secret\n"
        "LEASE_PLANE_BEARER_TOKEN=untouched\n",
    )
    # The file start.sh sources: rotated to the same new value.
    sourced = home / ".config/unitares/secrets.env"
    _write_secrets(sourced, "UNITARES_HTTP_API_TOKEN=old-http-token\n")
    _write_plist(home / "Library/LaunchAgents/extra-source.plist",
                 {"UNITARES_SECRETS_ENV": str(secrets_file)})
    sentinel_before = sentinel.read_bytes()
    fake = tmp_path / "fake-command"
    _write_fake_command(fake)

    result = subprocess.run(
        ["bash", str(ROTATE_SCRIPT)],
        text=True,
        capture_output=True,
        check=False,
        env=_rotation_env(home, plist_buddy=fake, launchctl=fake),
    )

    assert result.returncode == 0, result.stderr
    values = dict(
        line.split("=", 1) for line in secrets_file.read_text().splitlines()
    )
    http = values["UNITARES_HTTP_API_TOKEN"].strip("'")
    continuity = values["UNITARES_CONTINUITY_TOKEN_SECRET"].strip("'")
    assert http != "old-http-token" and len(http) >= 32
    assert continuity != "old-continuity-secret" and len(continuity) >= 32
    assert values["LEASE_PLANE_BEARER_TOKEN"] == "untouched"
    assert stat.S_IMODE(secrets_file.stat().st_mode) == 0o600
    assert http not in result.stdout + result.stderr
    assert continuity not in result.stdout + result.stderr
    assert sentinel.read_bytes() == sentinel_before
    assert sourced.read_text() == f"UNITARES_HTTP_API_TOKEN='{http}'\n"
    assert custom.read_text() == f"UNITARES_HTTP_API_TOKEN='{http}'\n"
    assert "com.unitares.sentinel-beam" in result.stderr
    (backup,) = (home / ".unitares").glob("rotation-backup-*")
    assert stat.S_IMODE(backup.stat().st_mode) == 0o700
    for index in (1, 2, 3):
        assert "old-http-token" in (backup / f"secrets-{index}.env").read_text()


def test_rotate_script_updates_sentinel_key_and_does_not_log_secret(
    tmp_path: Path,
) -> None:
    home, sentinel = _rotation_home(tmp_path, include_sentinel_token=True)
    fake = tmp_path / "fake-command"
    _write_fake_command(fake)
    environment = _rotation_env(home, plist_buddy=fake, launchctl=fake)

    result = subprocess.run(
        ["bash", str(ROTATE_SCRIPT)],
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    with sentinel.open("rb") as handle:
        new_token = plistlib.load(handle)["EnvironmentVariables"][
            "UNITARES_HTTP_API_TOKEN"
        ]
    assert new_token != "old-sentinel-token"
    assert len(new_token) >= 32
    assert new_token not in result.stdout + result.stderr
    anchor = json.loads((home / ".unitares/anchors/sentinel.json").read_text())
    assert anchor == {"agent_uuid": "11111111-2222-4333-8444-555555555555"}


def test_rotate_script_refuses_missing_sentinel_key_before_any_plist_write(
    tmp_path: Path,
) -> None:
    home, _sentinel = _rotation_home(tmp_path, include_sentinel_token=False)
    marker = tmp_path / "plistbuddy-called"
    fake_plistbuddy = tmp_path / "fake-plistbuddy"
    fake_launchctl = tmp_path / "fake-launchctl"
    _write_fake_command(fake_plistbuddy, f"touch {marker!s}")
    _write_fake_command(fake_launchctl)
    environment = _rotation_env(
        home, plist_buddy=fake_plistbuddy, launchctl=fake_launchctl
    )

    result = subprocess.run(
        ["bash", str(ROTATE_SCRIPT)],
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )

    assert result.returncode == 2
    assert "lacks a rotatable UNITARES_HTTP_API_TOKEN" in result.stderr
    assert not marker.exists()
    assert not list((home / ".unitares").glob("rotation-backup-*"))


def test_deploy_preflight_validates_auth_before_fast_forward() -> None:
    content = (REPO / "scripts/ops/deploy-sentinel.sh").read_text()
    check = content.index('python3 "$SENTINEL_PLIST_TOOL" check')
    fast_forward = content.index("deploy_lib_ff_worktree")
    assert check < fast_forward


def test_start_script_keeps_launchd_bearer_authoritative_over_secrets_file() -> None:
    content = (REPO / "elixir/sentinel/scripts/start.sh").read_text()
    capture = content.index(
        'sentinel_launchd_http_token="${UNITARES_HTTP_API_TOKEN:-}"'
    )
    source = content.index('source "$SECRETS_FILE"')
    restore = content.index(
        'export UNITARES_HTTP_API_TOKEN="$sentinel_launchd_http_token"'
    )
    execute = content.index("exec mix run --no-halt")
    assert capture < source < restore < execute


def test_unconfigured_wrapper_and_home_files_are_not_discovered(tmp_path):
    home = tmp_path / "home"
    _write_secrets(home / ".config/operator/secrets.env", "UNITARES_HTTP_API_TOKEN=untouched\n")
    _write_secrets(home / ".config/unitares/secrets.env", "UNITARES_HTTP_API_TOKEN=untouched\n")
    result = _run("secrets-files", env=_clean_env(HOME=str(home)))
    assert result.returncode == 0
    assert result.stdout == ""


def test_configured_wrapper_without_explicit_source_refuses(tmp_path):
    plist = tmp_path / "sentinel.plist"
    _write_plist(plist, {"UNITARES_UDS_SOCKET": str(tmp_path / "socket")})
    _wrap(plist, "UNITARES_HTTP_API_TOKEN")
    result = _run("rotate-token", "--plist", plist, input_text="test-token", env=_clean_env())
    assert result.returncode == 2
    assert "requires UNITARES_SECRETS_ENV" in result.stderr


def test_governance_bearer_enumerates_all_three_bearer_shapes(tmp_path: Path) -> None:
    """#3019: one canonical resolver classifies plist_env, secrets_wrapper, and external_store."""
    sys.path.insert(0, str(REPO / "scripts/ops"))
    import governance_bearer as gb

    plist_env = tmp_path / "plist-env.plist"
    _write_plist(plist_env, {"UNITARES_HTTP_API_TOKEN": "direct-plist-token"})
    assert gb.resolve_plist_bearer(plist_env) == (
        gb.BearerShape.PLIST_ENV,
        "direct-plist-token",
    )

    wrapped_plist = tmp_path / "wrapped.plist"
    secrets_file = tmp_path / "secrets.env"
    _write_plist(wrapped_plist, {})
    _wrap(wrapped_plist, "UNITARES_HTTP_API_TOKEN", secrets_file=secrets_file)
    _write_secrets(secrets_file, "UNITARES_HTTP_API_TOKEN='wrapped-secret-token'\n")
    assert gb.resolve_plist_bearer(wrapped_plist) == (
        gb.BearerShape.SECRETS_WRAPPER,
        "wrapped-secret-token",
    )

    external_plist = tmp_path / "external.plist"
    _governance_without_bearer(external_plist)
    assert gb.resolve_plist_bearer(external_plist) == (
        gb.BearerShape.EXTERNAL_STORE,
        None,
    )


def test_rotate_script_refuses_external_store_governance_without_hook(
    tmp_path: Path,
) -> None:
    """#3018: when governance keeps its bearer outside the plist and no hook is
    configured, rotate-secrets.sh refuses in pre-flight before touching any
    secrets file, plist, or backup."""
    home, sentinel = _rotation_home(tmp_path, include_sentinel_token=False)
    governance = home / "Library/LaunchAgents/com.unitares.governance-mcp.plist"
    _governance_without_bearer(governance)
    secrets_file = home / ".config/cirwel/secrets.env"
    _write_secrets(secrets_file, "UNITARES_HTTP_API_TOKEN=old-http-token\n")
    _wrap(sentinel, "UNITARES_HTTP_API_TOKEN", secrets_file=secrets_file)

    marker = tmp_path / "plistbuddy-called"
    fake_plistbuddy = tmp_path / "fake-plistbuddy"
    fake_launchctl = tmp_path / "fake-launchctl"
    _write_fake_command(fake_plistbuddy, f"touch {marker!s}")
    _write_fake_command(fake_launchctl)
    environment = _rotation_env(
        home, plist_buddy=fake_plistbuddy, launchctl=fake_launchctl
    )
    environment.pop("UNITARES_EXTERNAL_BEARER_ROTATE_CMD", None)

    result = subprocess.run(
        ["bash", str(ROTATE_SCRIPT)],
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )

    assert result.returncode == 2
    assert "external store" in result.stderr
    assert "UNITARES_EXTERNAL_BEARER_ROTATE_CMD" in result.stderr
    assert not marker.exists()
    assert not list((home / ".unitares").glob("rotation-backup-*"))
    assert secrets_file.read_text() == "UNITARES_HTTP_API_TOKEN=old-http-token\n"


@pytest.mark.parametrize("status,expected_code", [(200, 0), (401, 2)])
def test_rotate_script_runs_external_store_hook_and_verifies_live_socket(
    tmp_path: Path, status: int, expected_code: int
) -> None:
    """#3018: with UNITARES_EXTERNAL_BEARER_ROTATE_CMD configured, rotate-secrets.sh
    pipes the new token to the hook and verifies over UDS that the restarted
    governance service accepts the new bearer (failing on 401)."""
    home, sentinel = _rotation_home(tmp_path, include_sentinel_token=False)
    governance = home / "Library/LaunchAgents/com.unitares.governance-mcp.plist"
    _governance_without_bearer(governance)
    secrets_file = home / ".config/cirwel/secrets.env"
    _write_secrets(secrets_file, "UNITARES_HTTP_API_TOKEN=old-http-token\n")
    _wrap(sentinel, "UNITARES_HTTP_API_TOKEN", secrets_file=secrets_file)

    hook_capture = tmp_path / "hook-token.txt"
    fake = tmp_path / "fake-command"
    _write_fake_command(fake)

    short_socket_dir = tempfile.TemporaryDirectory(prefix="unitares-uds-", dir="/tmp")
    socket_path = Path(short_socket_dir.name) / "governance.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(1)
    captured: list[bytes] = []
    errors: list[BaseException] = []
    thread = threading.Thread(
        target=_serve_one_uds_response,
        args=(listener, status, captured, errors),
        daemon=True,
    )
    thread.start()

    environment = {
        **_rotation_env(home, plist_buddy=fake, launchctl=fake),
        "UNITARES_EXTERNAL_BEARER_ROTATE_CMD": f"cat > {hook_capture!s}",
        "UNITARES_UDS_SOCKET": str(socket_path),
        "UNITARES_ROTATE_VERIFY_WAIT_SECONDS": "0",
    }

    result = subprocess.run(
        ["bash", str(ROTATE_SCRIPT)],
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )
    thread.join(timeout=5)
    short_socket_dir.cleanup()

    assert not thread.is_alive()
    assert not errors
    assert result.returncode == expected_code, result.stderr
    rotated_token = hook_capture.read_text().strip()
    assert len(rotated_token) >= 32
    assert rotated_token not in result.stdout + result.stderr
    assert len(captured) == 1
    assert captured[0].startswith(b"GET /v1/tools HTTP/1.1")
    assert f"Authorization: Bearer {rotated_token}".encode() in captured[0]
    if status == 401:
        assert "rejected the rotated bearer (HTTP 401)" in result.stderr


def test_rotate_script_rolls_back_when_external_store_hook_fails(
    tmp_path: Path,
) -> None:
    """When UNITARES_EXTERNAL_BEARER_ROTATE_CMD exits non-zero, rotate-secrets.sh
    aborts before rewriting client plists and restores the backed-up secrets file."""
    home, sentinel = _rotation_home(tmp_path, include_sentinel_token=False)
    governance = home / "Library/LaunchAgents/com.unitares.governance-mcp.plist"
    _governance_without_bearer(governance)
    client_plist = home / "Library/LaunchAgents/com.unitares.agent-orchestrator.plist"
    _write_plist(
        client_plist,
        {"UNITARES_HTTP_API_TOKEN": "old-client-token"},
        mode=0o600,
    )
    secrets_file = home / ".config/cirwel/secrets.env"
    _write_secrets(secrets_file, "UNITARES_HTTP_API_TOKEN='old-http-token'\n")
    _wrap(sentinel, "UNITARES_HTTP_API_TOKEN", secrets_file=secrets_file)

    fake_launchctl = tmp_path / "fake-launchctl"
    _write_fake_command(fake_launchctl)
    environment = {
        **_rotation_env(
            home,
            plist_buddy=Path("/usr/libexec/PlistBuddy"),
            launchctl=fake_launchctl,
        ),
        "UNITARES_EXTERNAL_BEARER_ROTATE_CMD": "echo 'keychain locked' >&2; exit 1",
    }

    result = subprocess.run(
        ["bash", str(ROTATE_SCRIPT)],
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )

    assert result.returncode != 0
    assert "keychain locked" in result.stderr
    assert "restoring secrets, plists, and anchors" in result.stderr
    assert secrets_file.read_text() == "UNITARES_HTTP_API_TOKEN='old-http-token'\n"
    with client_plist.open("rb") as handle:
        client_payload = plistlib.load(handle)
    assert (
        client_payload["EnvironmentVariables"]["UNITARES_HTTP_API_TOKEN"]
        == "old-client-token"
    )


def test_verify_bearer_polls_across_restart_and_reports_accurate_unreachable_cause(
    tmp_path: Path,
) -> None:
    """check_bearer_live with wait_seconds>0 retries transient 401s from an
    outgoing process, and an unreachable socket with a plist_env governance
    plist does not falsely claim the plist lacks UNITARES_HTTP_API_TOKEN."""
    import sys

    sys.path.insert(0, str(SCRIPT.parent))
    import governance_bearer as gb

    plist_env_gov = tmp_path / "gov-plist-env.plist"
    _write_plist(
        plist_env_gov,
        {"UNITARES_HTTP_API_TOKEN": "configured-token"},
        mode=0o600,
    )
    missing_sock = tmp_path / "missing.sock"
    with pytest.raises(gb.PlistConfigError) as exc_info:
        gb.check_bearer_live(
            missing_sock,
            "configured-token",
            governance_plist=plist_env_gov,
            subject="the rotated",
            wait_seconds=0.0,
        )
    assert "keeps no UNITARES_HTTP_API_TOKEN" not in str(exc_info.value)
    assert f"could not be reached over {missing_sock}" in str(exc_info.value)

    short_socket_dir = tempfile.TemporaryDirectory(prefix="unitares-uds-", dir="/tmp")
    socket_path = Path(short_socket_dir.name) / "governance.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(2)

    def _serve_401_then_200() -> None:
        try:
            for status in (401, 200):
                conn, _ = listener.accept()
                with conn:
                    conn.recv(4096)
                    reason = "OK" if status == 200 else "Unauthorized"
                    conn.sendall(
                        f"HTTP/1.1 {status} {reason}\r\nContent-Length: 2\r\n\r\n{{}}".encode()
                    )
        finally:
            listener.close()

    thread = threading.Thread(target=_serve_401_then_200, daemon=True)
    thread.start()
    try:
        gb.check_bearer_live(
            socket_path,
            "configured-token",
            governance_plist=plist_env_gov,
            subject="the rotated",
            wait_seconds=2.0,
            poll_interval=0.05,
        )
    finally:
        thread.join(timeout=5)
        short_socket_dir.cleanup()


