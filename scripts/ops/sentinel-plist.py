#!/usr/bin/env python3
"""Render and verify the BEAM Sentinel LaunchAgent without exposing secrets.

The governance bearer is copied directly from the installed governance plist;
it is never accepted as a command-line argument or written to stdout/stderr.
The same utility also provides the deploy-time authenticated UDS probe and the
rotation-time update used by ``rotate-secrets.sh``.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import plistlib
import re
import secrets
import socket
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any


HTTP_TOKEN_KEY = "UNITARES_HTTP_API_TOKEN"
UDS_SOCKET_KEY = "UNITARES_UDS_SOCKET"
AUDIT_SESSION_KEY = "UNITARES_SENTINEL_AUDIT_SESSION"
ENFORCED_KINDS_KEY = "LEASE_PLANE_ENFORCED_SURFACE_KINDS"
TOKEN_PLACEHOLDER = "__UNITARES_HTTP_API_TOKEN__"
PLACEHOLDER_RE = re.compile(r"__[A-Z0-9_]+__")
INVALID_TOKENS = {TOKEN_PLACEHOLDER, "GENERATE_YOUR_OWN_TOKEN"}


class PlistConfigError(ValueError):
    """An installed or source plist is unsafe or incomplete."""


def _load_plist(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            payload = plistlib.load(handle)
    except FileNotFoundError as exc:
        raise PlistConfigError(f"plist missing: {path}") from exc
    except (OSError, plistlib.InvalidFileException) as exc:
        raise PlistConfigError(f"cannot read plist {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise PlistConfigError(f"plist root must be a dictionary: {path}")
    return payload


def _environment(payload: dict[str, Any], path: Path) -> dict[str, Any]:
    environment = payload.get("EnvironmentVariables")
    if not isinstance(environment, dict):
        raise PlistConfigError(f"{path} has no EnvironmentVariables dictionary")
    return environment


def _valid_token(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and value not in INVALID_TOKENS
        and PLACEHOLDER_RE.search(value) is None
    )


def _governance_token(path: Path) -> str:
    environment = _environment(_load_plist(path), path)
    token = environment.get(HTTP_TOKEN_KEY)
    if not _valid_token(token):
        raise PlistConfigError(
            f"{path} has no usable EnvironmentVariables:{HTTP_TOKEN_KEY}"
        )
    return token


def _replace_placeholders(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        for placeholder, replacement in replacements.items():
            value = value.replace(placeholder, replacement)
        return value
    if isinstance(value, list):
        return [_replace_placeholders(item, replacements) for item in value]
    if isinstance(value, dict):
        return {
            _replace_placeholders(key, replacements): _replace_placeholders(
                item, replacements
            )
            for key, item in value.items()
        }
    return value


def _remaining_placeholders(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, str):
        found.update(PLACEHOLDER_RE.findall(value))
    elif isinstance(value, list):
        for item in value:
            found.update(_remaining_placeholders(item))
    elif isinstance(value, dict):
        for key, item in value.items():
            found.update(_remaining_placeholders(key))
            found.update(_remaining_placeholders(item))
    return found


def _write_plist(path: Path, payload: dict[str, Any], *, mode: int) -> None:
    """Atomically write a plist beside its destination and preserve privacy."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            plistlib.dump(payload, handle, fmt=plistlib.FMT_XML, sort_keys=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def _existing_optional_values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    environment = _environment(_load_plist(path), path)
    values: dict[str, str] = {}
    for key in (AUDIT_SESSION_KEY, ENFORCED_KINDS_KEY):
        value = environment.get(key)
        if isinstance(value, str) and not PLACEHOLDER_RE.search(value):
            values[key] = value
    return values


def render_plist(
    *,
    template: Path,
    output: Path,
    governance_plist: Path,
    unitares_root: Path,
    home: Path,
    uds_socket: Path,
    audit_session: str | None,
    enforced_surface_kinds: str | None,
) -> None:
    if not uds_socket.is_absolute():
        raise PlistConfigError(f"{UDS_SOCKET_KEY} must be an absolute path")

    token = _governance_token(governance_plist)
    existing = _existing_optional_values(output)
    if audit_session is None:
        audit_session = existing.get(AUDIT_SESSION_KEY, "")
    if enforced_surface_kinds is None:
        enforced_surface_kinds = existing.get(ENFORCED_KINDS_KEY, "")

    payload = _load_plist(template)
    payload = _replace_placeholders(
        payload,
        {
            "__UNITARES_ROOT__": str(unitares_root),
            "__HOME__": str(home),
            "__SENTINEL_AUDIT_SESSION__": audit_session,
            "__LEASE_PLANE_ENFORCED_SURFACE_KINDS__": enforced_surface_kinds,
            TOKEN_PLACEHOLDER: token,
        },
    )
    environment = _environment(payload, template)
    environment[HTTP_TOKEN_KEY] = token
    environment[UDS_SOCKET_KEY] = str(uds_socket)

    unresolved = _remaining_placeholders(payload)
    if unresolved:
        names = ", ".join(sorted(unresolved))
        raise PlistConfigError(f"template still contains unresolved placeholders: {names}")

    _write_plist(output, payload, mode=0o600)
    validate_plist(
        plist=output,
        governance_plist=governance_plist,
        expected_socket=uds_socket,
    )


def validate_plist(
    *, plist: Path, governance_plist: Path, expected_socket: Path
) -> None:
    payload = _load_plist(plist)
    environment = _environment(payload, plist)
    token = environment.get(HTTP_TOKEN_KEY)
    if not _valid_token(token):
        raise PlistConfigError(
            f"{plist} has no usable EnvironmentVariables:{HTTP_TOKEN_KEY}; rerender it"
        )
    current_token = _governance_token(governance_plist)
    if not secrets.compare_digest(token, current_token):
        raise PlistConfigError(
            f"{plist} bearer does not match the current governance bearer; rerender it"
        )

    configured_socket = environment.get(UDS_SOCKET_KEY)
    if not isinstance(configured_socket, str) or not configured_socket:
        raise PlistConfigError(
            f"{plist} has no EnvironmentVariables:{UDS_SOCKET_KEY}; rerender it"
        )
    if not Path(configured_socket).is_absolute():
        raise PlistConfigError(f"{plist} {UDS_SOCKET_KEY} must be an absolute path")
    if Path(configured_socket) != expected_socket:
        raise PlistConfigError(
            f"{plist} {UDS_SOCKET_KEY} does not match the per-user governance socket"
        )

    unresolved = _remaining_placeholders(payload)
    if unresolved:
        names = ", ".join(sorted(unresolved))
        raise PlistConfigError(f"{plist} contains unresolved placeholders: {names}")

    mode = stat.S_IMODE(plist.stat().st_mode)
    if mode & 0o077:
        raise PlistConfigError(
            f"{plist} contains a bearer but mode is {oct(mode)}; rerender for mode 0o600"
        )


def rotation_preflight(plist: Path) -> None:
    environment = _environment(_load_plist(plist), plist)
    if not _valid_token(environment.get(HTTP_TOKEN_KEY)):
        raise PlistConfigError(
            f"{plist} lacks a rotatable {HTTP_TOKEN_KEY}; rerender before rotating"
        )


def rotate_token(plist: Path, token: str) -> None:
    if not _valid_token(token):
        raise PlistConfigError("refusing to install an empty or placeholder bearer")
    rotation_preflight(plist)
    payload = _load_plist(plist)
    _environment(payload, plist)[HTTP_TOKEN_KEY] = token
    mode = stat.S_IMODE(plist.stat().st_mode)
    _write_plist(plist, payload, mode=mode)


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: Path, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        connection.connect(str(self.socket_path))
        self.sock = connection


def probe_identity(plist: Path, *, timeout: float) -> None:
    payload = _load_plist(plist)
    environment = _environment(payload, plist)
    token = environment.get(HTTP_TOKEN_KEY)
    if not _valid_token(token):
        raise PlistConfigError(f"{plist} has no usable {HTTP_TOKEN_KEY}")
    socket_value = environment.get(UDS_SOCKET_KEY)
    if not isinstance(socket_value, str) or not Path(socket_value).is_absolute():
        raise PlistConfigError(f"{plist} has no usable {UDS_SOCKET_KEY}")

    body = json.dumps({"name": "identity", "arguments": {}}).encode()
    connection = _UnixHTTPConnection(Path(socket_value), timeout)
    try:
        connection.request(
            "POST",
            "/v1/tools/call",
            body=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
            },
        )
        response = connection.getresponse()
        response_body = response.read(1_048_576)
    except (OSError, TimeoutError, http.client.HTTPException) as exc:
        raise PlistConfigError(f"authenticated UDS probe failed: {exc}") from exc
    finally:
        connection.close()

    if response.status == 401:
        raise PlistConfigError(
            "authenticated UDS probe was rejected at the outer bearer gate (HTTP 401)"
        )
    if response.status != 200:
        raise PlistConfigError(
            f"authenticated UDS probe returned unexpected HTTP {response.status}"
        )
    try:
        decoded = json.loads(response_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlistConfigError("authenticated UDS probe returned invalid JSON") from exc
    if not isinstance(decoded, dict) or not ({"success", "result"} & decoded.keys()):
        raise PlistConfigError(
            "authenticated UDS probe did not return a governance tool envelope"
        )


def _default_home() -> Path:
    return Path(os.environ.get("HOME") or Path.home())


def _add_home_and_paths(parser: argparse.ArgumentParser, *, plist: bool) -> None:
    parser.add_argument("--home", type=Path, default=_default_home())
    if plist:
        parser.add_argument("--plist", type=Path)
    parser.add_argument("--governance-plist", type=Path)
    parser.add_argument("--socket", type=Path)


def _resolved_paths(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    home = args.home.expanduser().resolve()
    plist = (args.plist or home / "Library/LaunchAgents/com.unitares.sentinel-beam.plist").expanduser()
    governance = (
        args.governance_plist
        or home / "Library/LaunchAgents/com.unitares.governance-mcp.plist"
    ).expanduser()
    uds_socket = (args.socket or home / ".unitares/governance.sock").expanduser()
    return plist, governance, uds_socket


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    render = commands.add_parser("render", help="render a private Sentinel plist")
    _add_home_and_paths(render, plist=False)
    render.add_argument("-o", "--output", "--plist", dest="plist", type=Path)
    render.add_argument("-r", "--root", type=Path, required=True)
    render.add_argument(
        "--template",
        type=Path,
        default=Path(__file__).with_name("com.unitares.sentinel-beam.plist.template"),
    )
    render.add_argument("-a", "--audit-session")
    render.add_argument("-k", "--enforced-surface-kinds")

    check = commands.add_parser("check", help="validate installed auth/UDS config")
    _add_home_and_paths(check, plist=True)

    probe = commands.add_parser("probe", help="make a harmless identity call over UDS")
    _add_home_and_paths(probe, plist=True)
    probe.add_argument("--timeout", type=float, default=5.0)

    preflight = commands.add_parser(
        "rotation-preflight", help="require an existing rotatable Sentinel key"
    )
    preflight.add_argument("--plist", type=Path, required=True)

    rotate = commands.add_parser(
        "rotate-token", help="replace the existing bearer with a token read from stdin"
    )
    rotate.add_argument("--plist", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "render":
            plist, governance, uds_socket = _resolved_paths(args)
            render_plist(
                template=args.template.expanduser(),
                output=plist,
                governance_plist=governance,
                unitares_root=args.root.expanduser().resolve(),
                home=args.home.expanduser().resolve(),
                uds_socket=uds_socket,
                audit_session=args.audit_session,
                enforced_surface_kinds=args.enforced_surface_kinds,
            )
            print(f"rendered {plist} with authenticated UDS configuration")
        elif args.command == "check":
            plist, governance, uds_socket = _resolved_paths(args)
            validate_plist(
                plist=plist,
                governance_plist=governance,
                expected_socket=uds_socket,
            )
            print(f"validated {plist}: bearer and UDS configuration are current")
        elif args.command == "probe":
            plist, governance, uds_socket = _resolved_paths(args)
            validate_plist(
                plist=plist,
                governance_plist=governance,
                expected_socket=uds_socket,
            )
            probe_identity(plist, timeout=args.timeout)
            print("authenticated UDS probe reached governance identity policy")
        elif args.command == "rotation-preflight":
            rotation_preflight(args.plist.expanduser())
            print(f"validated {args.plist}: Sentinel bearer key is rotatable")
        elif args.command == "rotate-token":
            rotate_token(args.plist.expanduser(), sys.stdin.read().strip())
            print(f"rotated {HTTP_TOKEN_KEY} in {args.plist}")
        return 0
    except PlistConfigError as exc:
        print(f"sentinel-plist: REFUSING: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
