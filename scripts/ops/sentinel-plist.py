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
SECRETS_WRAPPER = "with-cirwel-secrets"
SECRET_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class PlistConfigError(ValueError):
    """An installed or source plist is unsafe or incomplete."""


def _wrapper_secrets_file(environment: dict[str, Any] | None = None) -> Path:
    """The file with-cirwel-secrets reads: ``CIRWEL_SECRETS_FILE`` from the
    job's environment (a plist's EnvironmentVariables; this process's own
    environment when no plist is given), else ~/.config/cirwel/secrets.env."""
    source = os.environ if environment is None else environment
    override = source.get("CIRWEL_SECRETS_FILE")
    if isinstance(override, str) and override:
        # Taken literally, as the wrapper takes it: no ~ expansion. A relative
        # path would resolve against the job's cwd, so it is refused instead.
        if not os.path.isabs(override):
            raise PlistConfigError(f"CIRWEL_SECRETS_FILE must be an absolute path: {override!r}")
        return Path(override)
    # The wrapper resolves ~ in the launched job, so the job's HOME decides.
    home = source.get("HOME")
    base = Path(home) if isinstance(home, str) and home else _default_home()
    return base / ".config/cirwel/secrets.env"


def _sourced_secrets_file() -> Path:
    """The file start.sh and deploy-lease-plane.sh source."""
    explicit = os.environ.get("UNITARES_SECRETS_ENV")
    if explicit:
        return Path(explicit).expanduser()
    home = _default_home()
    path = home / ".config/unitares/secrets.env"
    fallback = home / ".config/cirwel/secrets.env"
    if not path.exists() and fallback.exists():
        return fallback
    return path


def _rotation_files() -> list[Path]:
    """Every existing secrets file a reader of the rotated keys may use: this
    process's wrapper default, the file start.sh sources, and the file each
    installed wrapped LaunchAgent's wrapper reads (its own override or HOME)."""
    candidates = [_wrapper_secrets_file(), _sourced_secrets_file()]
    for plist in sorted((_default_home() / "Library/LaunchAgents").glob("*.plist")):
        try:
            payload = _load_plist(plist)
        except PlistConfigError:
            continue
        environment = payload.get("EnvironmentVariables")
        if _wrapped_keys(payload) and isinstance(environment, dict):
            candidates.append(_wrapper_secrets_file(environment))
        elif _wrapped_keys(payload):
            candidates.append(_wrapper_secrets_file({}))
    found: list[Path] = []
    for path in candidates:
        if path.exists() and not any(path.samefile(seen) for seen in found):
            found.append(path)
    return found


def _check_private(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise PlistConfigError(f"secrets file missing: {path}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise PlistConfigError(f"secrets file is not a regular file: {path}")
    if info.st_uid != os.getuid():
        raise PlistConfigError(f"secrets file is not owned by this user: {path}")
    if info.st_mode & 0o077:
        raise PlistConfigError(f"secrets file is readable by group or others: {path}")


def _split_secret_line(line: str) -> tuple[str, str, str] | None:
    """(prefix, key, raw value) of a KEY=VALUE line, or None for other lines."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    key, _, value = stripped.partition("=")
    key = key.strip()
    prefix = ""
    if key.startswith("export "):
        prefix, key = "export ", key[len("export "):].strip()
    return (prefix, key, value) if key else None


def _parse_secret_value(raw: str) -> str:
    """One value, with the wrapper's grammar: single quotes are literal; in
    double quotes the shell escapes \\\\ \\" \\$ and \\` are decoded."""
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        return raw[1:-1]
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        out, index, body = [], 0, raw[1:-1]
        while index < len(body):
            if body[index] == "\\" and index + 1 < len(body) and body[index + 1] in '\\"$`':
                out.append(body[index + 1])
                index += 2
            else:
                out.append(body[index])
                index += 1
        return "".join(out)
    return raw


def _encode_secret_value(value: str) -> str:
    if "'" not in value:
        return "'" + value + "'"
    return '"' + "".join("\\" + c if c in '\\"$`' else c for c in value) + '"'


def _read_secret(key: str, path: Path) -> str | None:
    """The last value of ``key`` in ``path`` (the wrapper's rule)."""
    _check_private(path)
    value = None
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = _split_secret_line(line)
        if parts and parts[1] == key:
            value = _parse_secret_value(parts[2])
    return value


def _defines_key(path: Path, key: str) -> bool:
    """Whether ``path`` has a line defining ``key``; values are not parsed."""
    return any(
        (parts := _split_secret_line(line)) is not None and parts[1] == key
        for line in path.read_text(encoding="utf-8").splitlines()
    )


def set_secret(key: str, value: str) -> list[Path]:
    """Replace ``key`` in every secrets file a reader may use.

    Returns the files rewritten. A file that does not define ``key`` is left
    alone: nothing reads the key from there. Every file is checked before any
    is written, so an unsafe file refuses the whole rotation.
    """
    if not SECRET_KEY_RE.fullmatch(key):
        raise PlistConfigError(f"not a variable name: {key!r}")
    if not _valid_token(value) or "\n" in value or "\r" in value:
        raise PlistConfigError("refusing to install an empty or placeholder secret")
    files = _rotation_files()
    for path in files:
        _check_private(path)
    return [path for path in files if _replace_secret_line(path, key, value)]


def _replace_secret_line(path: Path, key: str, value: str) -> bool:
    """Atomically rewrite every line of ``path`` defining ``key``; other lines
    are kept byte for byte. False, writing nothing, when none does."""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    changed = False
    for index, line in enumerate(lines):
        parts = _split_secret_line(line)
        if parts and parts[1] == key:
            ending = line[len(line.rstrip("\r\n")):]
            lines[index] = f"{parts[0]}{key}={_encode_secret_value(value)}{ending}"
            changed = True
    if not changed:
        return False
    mode = stat.S_IMODE(path.stat().st_mode)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write("".join(lines))
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
    return True


def _wrapped_keys(payload: dict[str, Any]) -> set[str]:
    """Keys the secrets wrapper supplies at exec time, or an empty set."""
    arguments = payload.get("ProgramArguments")
    if (
        not isinstance(arguments, list)
        or len(arguments) < 5
        or not all(isinstance(argument, str) for argument in arguments[:4])
        or Path(arguments[0]).name != SECRETS_WRAPPER
        or arguments[1] != "--keys"
        or arguments[3] != "--"
    ):
        return set()
    return {key for key in arguments[2].split(",") if key}


def _token_from(payload: dict[str, Any], path: Path) -> object:
    """The bearer the job runs with. The wrapper exports over the plist's
    environment, so for a wrapped key the secrets file wins."""
    environment = _environment(payload, path)
    if HTTP_TOKEN_KEY in _wrapped_keys(payload):
        return _read_secret(HTTP_TOKEN_KEY, _wrapper_secrets_file(environment))
    return environment.get(HTTP_TOKEN_KEY)


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
    payload = _load_plist(path)
    token = _token_from(payload, path)
    if not _valid_token(token):
        if HTTP_TOKEN_KEY in _wrapped_keys(payload):
            raise PlistConfigError(
                f"{path} takes {HTTP_TOKEN_KEY} from the secrets file, which has no usable value"
            )
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
    if HTTP_TOKEN_KEY in environment and mode & 0o077:
        raise PlistConfigError(
            f"{plist} contains a bearer but mode is {oct(mode)}; rerender for mode 0o600"
        )


def rotation_preflight(plist: Path) -> bool:
    """Require a rotatable bearer. True when it lives in the secrets file."""
    payload = _load_plist(plist)
    wrapped = HTTP_TOKEN_KEY in _wrapped_keys(payload)
    if not _valid_token(_token_from(payload, plist)):
        if wrapped:
            raise PlistConfigError(
                f"{plist} lacks a rotatable {HTTP_TOKEN_KEY} in the secrets file its wrapper reads"
            )
        raise PlistConfigError(
            f"{plist} lacks a rotatable {HTTP_TOKEN_KEY}; rerender before rotating"
        )
    return wrapped


def rotate_token(plist: Path, token: str) -> None:
    """Install ``token`` as the plist's bearer.

    A wrapped plist holds no bearer: rotate-secrets.sh writes the secrets file
    first, and this only confirms the job will start with the new value.
    """
    if not _valid_token(token):
        raise PlistConfigError("refusing to install an empty or placeholder bearer")
    if rotation_preflight(plist):
        current = _token_from(_load_plist(plist), plist)
        if not isinstance(current, str) or not secrets.compare_digest(current, token):
            raise PlistConfigError(
                f"{plist} takes {HTTP_TOKEN_KEY} from the secrets file, "
                "which does not hold the new value; rotate the secrets file first"
            )
        return
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
            rotation_preflight(args.plist.expanduser())
            print(f"validated {args.plist}: Sentinel bearer key is rotatable")
        elif args.command == "rotate-token":
            plist = args.plist.expanduser()
            rotate_token(plist, sys.stdin.read().strip())
            if HTTP_TOKEN_KEY in _wrapped_keys(_load_plist(plist)):
                print(f"{args.plist} takes {HTTP_TOKEN_KEY} from the secrets file, which holds the new value")
            else:
                print(f"rotated {HTTP_TOKEN_KEY} in {args.plist}")
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
