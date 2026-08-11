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


def _run(*arguments: object, input_text: str | None = None, env=None):
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


def test_rotate_script_updates_sentinel_key_and_does_not_log_secret(
    tmp_path: Path,
) -> None:
    home, sentinel = _rotation_home(tmp_path, include_sentinel_token=True)
    fake = tmp_path / "fake-command"
    _write_fake_command(fake)
    environment = {
        **os.environ,
        "HOME": str(home),
        "UNITARES_PLIST_BUDDY": str(fake),
        "UNITARES_LAUNCHCTL": str(fake),
        "UNITARES_SENTINEL_PLIST_TOOL": str(SCRIPT),
    }

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
    environment = {
        **os.environ,
        "HOME": str(home),
        "UNITARES_PLIST_BUDDY": str(fake_plistbuddy),
        "UNITARES_LAUNCHCTL": str(fake_launchctl),
        "UNITARES_SENTINEL_PLIST_TOOL": str(SCRIPT),
    }

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
