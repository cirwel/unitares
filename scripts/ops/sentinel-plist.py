#!/usr/bin/env python3
"""Render and verify the BEAM Sentinel LaunchAgent without exposing secrets.

The governance bearer is copied directly from the installed governance plist;
it is never accepted as a command-line argument or written to stdout/stderr.
The same utility also provides the deploy-time authenticated UDS probe and the
rotation-time update used by ``rotate-secrets.sh``.

A plist may instead launch through the operator's secrets wrapper
(``with-cirwel-secrets --keys K1,K2 -- program ...``), which supplies the named
keys from a secrets file at exec time so the plist holds no value. For such a
plist the bearer is read exactly where the wrapper reads it: the job's
``CIRWEL_SECRETS_FILE``, else ``~/.config/cirwel/secrets.env``. Rotation
(``set-secret``) updates every secrets file a reader may use: the wrapper's,
and the one ``elixir/sentinel/scripts/start.sh`` and ``deploy-lease-plane.sh``
source (``UNITARES_SECRETS_ENV``, else ``~/.config/unitares/secrets.env``, else
the cirwel file).

The governance plist may also keep no bearer at all, when its job loads the
bearer from a store outside the plist before exec'ing the server. Then
``check`` asks the running service, over the per-user socket, whether it
accepts the Sentinel's bearer, and ``render`` requires a wrapped Sentinel,
since there is no value to copy.

Shared bearer resolution, secret-file grammar, and live UDS bearer checks live
in ``governance_bearer.py`` (#3019).
"""

from __future__ import annotations

import argparse
import http.client
import json
import secrets
import stat
import sys
from pathlib import Path
from typing import Any

OPS_DIR = Path(__file__).resolve().parent
if str(OPS_DIR) not in sys.path:
    sys.path.insert(0, str(OPS_DIR))

from governance_bearer import (  # noqa: E402
    EXTERNAL_ROTATE_CMD_ENV,
    HTTP_TOKEN_KEY,
    PLACEHOLDER_RE,
    SECRETS_WRAPPER,
    TOKEN_PLACEHOLDER,
    UDS_SOCKET_KEY,
    BearerShape,
    PlistConfigError,
    UnixHTTPConnection as _UnixHTTPConnection,
    check_bearer_live as _shared_check_bearer_live,
    check_private,
    default_home as _default_home,
    defines_key,
    encode_secret_value,
    governance_token,
    load_plist as _load_plist,
    parse_secret_value,
    plist_environment as _environment,
    read_secret,
    replace_secret_line,
    rotate_token as _shared_rotate_token,
    rotation_files,
    rotation_preflight as _shared_rotation_preflight,
    set_secret,
    sourced_secrets_file,
    split_secret_line,
    token_from_payload,
    valid_token as _valid_token,
    wrapped_keys,
    wrapper_secrets_file,
    write_plist as _write_plist,
)

AUDIT_SESSION_KEY = "UNITARES_SENTINEL_AUDIT_SESSION"
ENFORCED_KINDS_KEY = "LEASE_PLANE_ENFORCED_SURFACE_KINDS"


def _wrapper_secrets_file(environment: dict[str, Any] | None = None) -> Path:
    return wrapper_secrets_file(environment)


def _sourced_secrets_file() -> Path:
    return sourced_secrets_file()


def _rotation_files() -> list[Path]:
    return rotation_files()


def _check_private(path: Path) -> None:
    check_private(path)


def _split_secret_line(line: str) -> tuple[str, str, str] | None:
    return split_secret_line(line)


def _parse_secret_value(raw: str) -> str:
    return parse_secret_value(raw)


def _encode_secret_value(value: str) -> str:
    return encode_secret_value(value)


def _read_secret(key: str, path: Path) -> str | None:
    return read_secret(key, path)


def _defines_key(path: Path, key: str) -> bool:
    return defines_key(path, key)


def _replace_secret_line(path: Path, key: str, value: str) -> bool:
    return replace_secret_line(path, key, value)


def _wrapped_keys(payload: dict[str, Any]) -> set[str]:
    return wrapped_keys(payload)


def _token_from(payload: dict[str, Any], path: Path) -> object:
    return token_from_payload(payload, path)


def _governance_token(path: Path) -> str | None:
    return governance_token(path)


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
    # A re-render keeps an installed wrapper, so it never writes the bearer
    # back into a plist that had been moved off it.
    # The file its wrapper reads is pinned explicitly, so a template HOME that
    # differs from the installed one cannot move the job's credential source.
    wrapper: list[str] = []
    wrapper_file = None
    if output.exists():
        installed = _load_plist(output)
        if HTTP_TOKEN_KEY in _wrapped_keys(installed):
            wrapper = list(installed["ProgramArguments"][:4])
            wrapper_file = str(_wrapper_secrets_file(_environment(installed, output)))
    if token is None and not wrapper:
        raise PlistConfigError(
            f"{governance_plist} keeps no {HTTP_TOKEN_KEY}, so there is no bearer to "
            "copy; launch the Sentinel through the secrets wrapper "
            f"({SECRETS_WRAPPER} --keys {HTTP_TOKEN_KEY} -- ...) with the bearer in its secrets file"
        )
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
            # A wrapped job drops this entry below, so it needs no value.
            TOKEN_PLACEHOLDER: token or "",
        },
    )
    environment = _environment(payload, template)
    if wrapper:
        environment.pop(HTTP_TOKEN_KEY, None)
        environment["CIRWEL_SECRETS_FILE"] = wrapper_file
        payload["ProgramArguments"] = wrapper + list(payload.get("ProgramArguments") or [])
    else:
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
    wrapped = HTTP_TOKEN_KEY in _wrapped_keys(payload)
    token = _token_from(payload, plist)
    if not _valid_token(token):
        if wrapped:
            raise PlistConfigError(
                f"{plist} takes {HTTP_TOKEN_KEY} from the secrets file, which has no usable value"
            )
        raise PlistConfigError(
            f"{plist} has no usable EnvironmentVariables:{HTTP_TOKEN_KEY}; rerender it"
        )
    current_token = _governance_token(governance_plist)
    if current_token is not None and not secrets.compare_digest(token, current_token):
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
    if HTTP_TOKEN_KEY in environment and mode & 0o077:
        raise PlistConfigError(
            f"{plist} contains a bearer but mode is {oct(mode)}; rerender for mode 0o600"
        )

    if current_token is None:
        _check_bearer_live(expected_socket, token, governance_plist=governance_plist)


def _check_bearer_live(
    socket_path: Path, token: str, *, governance_plist: Path, timeout: float = 5.0
) -> None:
    """Confirm the running governance service accepts ``token``."""
    _shared_check_bearer_live(
        socket_path,
        token,
        governance_plist=governance_plist,
        subject="the Sentinel",
        timeout=timeout,
    )


def rotation_preflight(plist: Path, *, allow_external_store: bool = False) -> bool:
    """Require a rotatable bearer. True when it lives in the secrets file."""
    shape = _shared_rotation_preflight(
        plist, allow_external_store=allow_external_store
    )
    return shape is BearerShape.SECRETS_WRAPPER


def rotate_token(
    plist: Path, token: str, *, allow_external_store: bool = False
) -> BearerShape:
    """Install ``token`` as the plist's bearer."""
    return _shared_rotate_token(
        plist, token, allow_external_store=allow_external_store
    )


def probe_identity(plist: Path, *, timeout: float) -> None:
    payload = _load_plist(plist)
    environment = _environment(payload, plist)
    token = _token_from(payload, plist)
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
    except ValueError:
        # http.client quotes the whole header value when it rejects one.
        raise PlistConfigError(f"{plist} {HTTP_TOKEN_KEY} is not a valid header value") from None
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
        "rotation-preflight", help="require an existing rotatable bearer key"
    )
    preflight.add_argument("--plist", type=Path, required=True)
    preflight.add_argument(
        "--allow-external-store",
        action="store_true",
        help="accept an external-store governance plist when UNITARES_EXTERNAL_BEARER_ROTATE_CMD is set",
    )

    rotate = commands.add_parser(
        "rotate-token", help="replace the existing bearer with a token read from stdin"
    )
    rotate.add_argument("--plist", type=Path, required=True)
    rotate.add_argument(
        "--allow-external-store",
        action="store_true",
        help="rotate an external-store governance plist via UNITARES_EXTERNAL_BEARER_ROTATE_CMD",
    )

    verify = commands.add_parser(
        "verify-bearer",
        help="verify the running governance service accepts the bearer read from stdin over UDS",
    )
    _add_home_and_paths(verify, plist=False)
    verify.add_argument("--timeout", type=float, default=5.0)

    set_file = commands.add_parser(
        "set-secret",
        help="replace KEY in every secrets file with a value read from stdin",
    )
    set_file.add_argument("--key", required=True)

    commands.add_parser(
        "secrets-files", help="list the existing secrets files set-secret rotates"
    )
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
            rotation_preflight(
                args.plist.expanduser(),
                allow_external_store=args.allow_external_store,
            )
            print(f"validated {args.plist}: Sentinel bearer key is rotatable")
        elif args.command == "rotate-token":
            plist = args.plist.expanduser()
            shape = rotate_token(
                plist,
                sys.stdin.read().strip(),
                allow_external_store=args.allow_external_store,
            )
            if shape is BearerShape.SECRETS_WRAPPER:
                print(
                    f"{args.plist} takes {HTTP_TOKEN_KEY} from the secrets file, which holds the new value"
                )
            elif shape is BearerShape.EXTERNAL_STORE:
                print(
                    f"rotated {HTTP_TOKEN_KEY} for {args.plist} via {EXTERNAL_ROTATE_CMD_ENV}"
                )
            else:
                print(f"rotated {HTTP_TOKEN_KEY} in {args.plist}")
        elif args.command == "verify-bearer":
            home = args.home.expanduser().resolve()
            governance = (
                args.governance_plist
                or home / "Library/LaunchAgents/com.unitares.governance-mcp.plist"
            ).expanduser()
            uds_socket = (args.socket or home / ".unitares/governance.sock").expanduser()
            token = sys.stdin.read().strip()
            if not _valid_token(token):
                raise PlistConfigError("refusing to verify an empty or placeholder bearer")
            _shared_check_bearer_live(
                uds_socket,
                token,
                governance_plist=governance,
                subject="the rotated",
                timeout=args.timeout,
            )
            print(f"verified {uds_socket}: running governance service accepts rotated bearer")
        elif args.command == "set-secret":
            set_secret(args.key, sys.stdin.read().strip())
            # Reported from key names alone, never from anything the value
            # touched, so no secret can reach the output.
            defined = [path for path in _rotation_files() if _defines_key(path, args.key)]
            for path in defined:
                print(f"rotated {args.key} in {path}")
            if not defined:
                print(f"no secrets file defines {args.key}; left unchanged")
        elif args.command == "secrets-files":
            for path in _rotation_files():
                print(path)
        return 0
    except PlistConfigError as exc:
        print(f"sentinel-plist: REFUSING: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
