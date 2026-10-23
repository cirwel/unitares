#!/usr/bin/env python3
"""Canonical resolver and verifier for the UNITARES governance HTTP bearer token.

Supported LaunchAgent bearer storage shapes (``BearerShape``):
  - ``plist_env``: ``EnvironmentVariables:UNITARES_HTTP_API_TOKEN`` is stored
    directly in the LaunchAgent plist.
  - ``secrets_wrapper``: ``ProgramArguments`` launches through
    ``with-cirwel-secrets --keys ...UNITARES_HTTP_API_TOKEN... -- ...``, which
    reads the job's ``CIRWEL_SECRETS_FILE`` (or ``~/.config/cirwel/secrets.env``)
    at exec time so the plist holds no value.
  - ``external_store``: the plist names neither ``EnvironmentVariables:UNITARES_HTTP_API_TOKEN``
    nor the secrets wrapper (for example, a launcher script that reads the
    macOS login Keychain before ``exec``). Callers verify a candidate bearer
    against the running governance service over the per-user Unix domain socket,
    and rotation refuses unless an explicit external-store rotation hook
    (``UNITARES_EXTERNAL_BEARER_ROTATE_CMD``) is configured.
"""

from __future__ import annotations

import http.client
import os
import plistlib
import re
import secrets
import socket
import stat
import subprocess
import tempfile
import time
from enum import StrEnum
from pathlib import Path
from typing import Any


HTTP_TOKEN_KEY = "UNITARES_HTTP_API_TOKEN"
UDS_SOCKET_KEY = "UNITARES_UDS_SOCKET"
TOKEN_PLACEHOLDER = "__UNITARES_HTTP_API_TOKEN__"
PLACEHOLDER_RE = re.compile(r"__[A-Z0-9_]+__")
INVALID_TOKENS = {TOKEN_PLACEHOLDER, "GENERATE_YOUR_OWN_TOKEN"}
SECRETS_WRAPPER = "with-cirwel-secrets"
SECRET_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
EXTERNAL_ROTATE_CMD_ENV = "UNITARES_EXTERNAL_BEARER_ROTATE_CMD"


class BearerShape(StrEnum):
    """Where a LaunchAgent plist obtains ``UNITARES_HTTP_API_TOKEN``."""

    PLIST_ENV = "plist_env"
    SECRETS_WRAPPER = "secrets_wrapper"
    EXTERNAL_STORE = "external_store"


class PlistConfigError(ValueError):
    """An installed or source plist is unsafe or incomplete."""


def default_home() -> Path:
    return Path(os.environ.get("HOME") or Path.home())


def load_plist(path: Path) -> dict[str, Any]:
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


def plist_environment(payload: dict[str, Any], path: Path) -> dict[str, Any]:
    environment = payload.get("EnvironmentVariables")
    if not isinstance(environment, dict):
        raise PlistConfigError(f"{path} has no EnvironmentVariables dictionary")
    return environment


def valid_token(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and value not in INVALID_TOKENS
        and PLACEHOLDER_RE.search(value) is None
    )


def wrapped_keys(payload: dict[str, Any]) -> set[str]:
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


def wrapper_secrets_file(environment: dict[str, Any] | None = None) -> Path:
    """The file ``with-cirwel-secrets`` reads: ``CIRWEL_SECRETS_FILE`` from the
    job's environment (a plist's ``EnvironmentVariables``; this process's own
    environment when no plist is given), else ``~/.config/cirwel/secrets.env``."""
    source = os.environ if environment is None else environment
    override = source.get("CIRWEL_SECRETS_FILE")
    if isinstance(override, str) and override:
        if not os.path.isabs(override):
            raise PlistConfigError(
                f"CIRWEL_SECRETS_FILE must be an absolute path: {override!r}"
            )
        return Path(override)
    home = source.get("HOME")
    base = Path(home) if isinstance(home, str) and home else default_home()
    return base / ".config/cirwel/secrets.env"


def sourced_secrets_file() -> Path:
    """The file ``start.sh`` and ``deploy-lease-plane.sh`` source."""
    explicit = os.environ.get("UNITARES_SECRETS_ENV")
    if explicit:
        return Path(explicit).expanduser()
    home = default_home()
    path = home / ".config/unitares/secrets.env"
    fallback = home / ".config/cirwel/secrets.env"
    if not path.exists() and fallback.exists():
        return fallback
    return path


def rotation_files() -> list[Path]:
    """Every existing secrets file a reader of the rotated keys may use."""
    candidates = [wrapper_secrets_file(), sourced_secrets_file()]
    for plist in sorted((default_home() / "Library/LaunchAgents").glob("*.plist")):
        try:
            payload = load_plist(plist)
        except PlistConfigError:
            continue
        environment = payload.get("EnvironmentVariables")
        if wrapped_keys(payload) and isinstance(environment, dict):
            candidates.append(wrapper_secrets_file(environment))
        elif wrapped_keys(payload):
            candidates.append(wrapper_secrets_file({}))
    found: list[Path] = []
    for path in candidates:
        if path.exists() and not any(path.samefile(seen) for seen in found):
            found.append(path)
    return found


def check_private(path: Path) -> None:
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


def split_secret_line(line: str) -> tuple[str, str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    key, _, value = stripped.partition("=")
    key = key.strip()
    prefix = ""
    if key.startswith("export "):
        prefix, key = "export ", key[len("export "):].strip()
    return (prefix, key, value) if key else None


def parse_secret_value(raw: str) -> str:
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


def encode_secret_value(value: str) -> str:
    if "'" not in value:
        return "'" + value + "'"
    return '"' + "".join("\\" + c if c in '\\"$`' else c for c in value) + '"'


def _read_secrets_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise PlistConfigError(f"cannot read secrets file {path}: {exc}") from exc


def read_secret(key: str, path: Path) -> str | None:
    check_private(path)
    value = None
    for line in _read_secrets_text(path).splitlines():
        parts = split_secret_line(line)
        if parts and parts[1] == key:
            value = parse_secret_value(parts[2])
    return value


def defines_key(path: Path, key: str) -> bool:
    return any(
        (parts := split_secret_line(line)) is not None and parts[1] == key
        for line in _read_secrets_text(path).splitlines()
    )


def set_secret(key: str, value: str) -> list[Path]:
    if not SECRET_KEY_RE.fullmatch(key):
        raise PlistConfigError(f"not a variable name: {key!r}")
    if not valid_token(value) or "\n" in value or "\r" in value:
        raise PlistConfigError("refusing to install an empty or placeholder secret")
    files = rotation_files()
    for path in files:
        check_private(path)
    return [path for path in files if replace_secret_line(path, key, value)]


def replace_secret_line(path: Path, key: str, value: str) -> bool:
    lines = _read_secrets_text(path).splitlines(keepends=True)
    changed = False
    for index, line in enumerate(lines):
        parts = split_secret_line(line)
        if parts and parts[1] == key:
            ending = line[len(line.rstrip("\r\n")):]
            lines[index] = f"{parts[0]}{key}={encode_secret_value(value)}{ending}"
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


def classify_bearer_shape(payload: dict[str, Any]) -> BearerShape:
    """Return how ``payload`` supplies ``UNITARES_HTTP_API_TOKEN``."""
    if HTTP_TOKEN_KEY in wrapped_keys(payload):
        return BearerShape.SECRETS_WRAPPER
    environment = payload.get("EnvironmentVariables")
    if isinstance(environment, dict) and HTTP_TOKEN_KEY in environment:
        return BearerShape.PLIST_ENV
    return BearerShape.EXTERNAL_STORE


def token_from_payload(payload: dict[str, Any], path: Path) -> object:
    """The bearer configured in ``payload`` or its wrapper secrets file."""
    environment = plist_environment(payload, path)
    if HTTP_TOKEN_KEY in wrapped_keys(payload):
        return read_secret(HTTP_TOKEN_KEY, wrapper_secrets_file(environment))
    return environment.get(HTTP_TOKEN_KEY)


def resolve_plist_bearer(path: Path) -> tuple[BearerShape, str | None]:
    """Return ``(shape, token)`` for ``path``.

    * ``PLIST_ENV`` / ``SECRETS_WRAPPER``: returns the validated bearer, or
      raises ``PlistConfigError`` if the value is missing, empty, a placeholder,
      or in an unsafe secrets file.
    * ``EXTERNAL_STORE``: returns ``(BearerShape.EXTERNAL_STORE, None)``; callers
      verify candidate bearers against the running governance service over the
      per-user UDS socket instead.
    """
    payload = load_plist(path)
    shape = classify_bearer_shape(payload)
    if shape is BearerShape.EXTERNAL_STORE:
        return shape, None
    token = token_from_payload(payload, path)
    if not valid_token(token):
        if shape is BearerShape.SECRETS_WRAPPER:
            raise PlistConfigError(
                f"{path} takes {HTTP_TOKEN_KEY} from the secrets file, which has no usable value"
            )
        raise PlistConfigError(
            f"{path} has no usable EnvironmentVariables:{HTTP_TOKEN_KEY}"
        )
    return shape, str(token)


def governance_token(path: Path) -> str | None:
    """The governance bearer, or ``None`` when the plist uses an external store."""
    return resolve_plist_bearer(path)[1]


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: Path, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        connection.connect(str(self.socket_path))
        self.sock = connection


def _plist_uses_external_store(governance_plist: Path | None) -> bool:
    if governance_plist is None:
        return False
    try:
        return classify_bearer_shape(load_plist(governance_plist)) is BearerShape.EXTERNAL_STORE
    except PlistConfigError:
        return True


def _check_bearer_live_once(
    socket_path: Path,
    token: str,
    *,
    governance_plist: Path | None = None,
    subject: str = "the Sentinel",
    timeout: float = 5.0,
) -> None:
    connection = UnixHTTPConnection(socket_path, timeout)
    try:
        connection.request(
            "GET", "/v1/tools", headers={"Authorization": f"Bearer {token}"}
        )
        response = connection.getresponse()
        response.read(1_048_576)
    except ValueError:
        raise PlistConfigError(
            f"{subject} {HTTP_TOKEN_KEY} is not a valid header value"
        ) from None
    except (OSError, TimeoutError, http.client.HTTPException) as exc:
        if governance_plist is not None and _plist_uses_external_store(governance_plist):
            raise PlistConfigError(
                f"{governance_plist} keeps no {HTTP_TOKEN_KEY}, and the running governance "
                f"service could not be reached to check {subject} bearer instead: {exc}"
            ) from exc
        raise PlistConfigError(
            f"the running governance service could not be reached over {socket_path}: {exc}"
        ) from exc
    finally:
        connection.close()
    if response.status == 401:
        raise PlistConfigError(
            f"the running governance service rejected {subject} bearer (HTTP 401); "
            "it does not match the current governance bearer"
        )
    if response.status != 200:
        raise PlistConfigError(
            f"bearer check against the running governance service returned HTTP {response.status}"
        )


def check_bearer_live(
    socket_path: Path,
    token: str,
    *,
    governance_plist: Path | None = None,
    subject: str = "the Sentinel",
    timeout: float = 5.0,
    wait_seconds: float = 0.0,
    poll_interval: float = 0.25,
) -> None:
    """Confirm the running governance service accepts ``token`` over UDS.

    When ``wait_seconds > 0`` (used right after ``launchctl unload`` / ``load``),
    polls until ``wait_seconds`` elapses so an asynchronous launchd teardown or
    startup race (stale socket connection refused, or the outgoing process
    briefly answering 401 with the pre-rotation token) does not fail a healthy
    rotation.
    """
    deadline = time.monotonic() + max(0.0, wait_seconds)
    while True:
        try:
            _check_bearer_live_once(
                socket_path,
                token,
                governance_plist=governance_plist,
                subject=subject,
                timeout=timeout,
            )
            return
        except PlistConfigError as exc:
            if "not a valid header value" in str(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(min(poll_interval, max(0.01, deadline - time.monotonic())))


def write_plist(path: Path, payload: dict[str, Any], *, mode: int) -> None:
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


def rotation_preflight(
    plist: Path,
    *,
    allow_external_store: bool = False,
    external_rotate_cmd: str | None = None,
) -> BearerShape:
    """Require a rotatable bearer on ``plist`` and return its ``BearerShape``.

    When ``allow_external_store`` is True and ``plist`` keeps its bearer in an
    external store (such as an OS Keychain launcher), preflight requires
    ``external_rotate_cmd`` (or ``UNITARES_EXTERNAL_BEARER_ROTATE_CMD``) so
    rotation never silently leaves the governance service on its old bearer
    while updating clients (#3018).
    """
    payload = load_plist(plist)
    shape = classify_bearer_shape(payload)
    if shape is BearerShape.EXTERNAL_STORE:
        cmd = (
            external_rotate_cmd
            if external_rotate_cmd is not None
            else os.environ.get(EXTERNAL_ROTATE_CMD_ENV, "")
        )
        if allow_external_store and cmd.strip():
            return shape
        if allow_external_store:
            raise PlistConfigError(
                f"{plist} keeps no {HTTP_TOKEN_KEY} in its plist or secrets wrapper "
                "(its launcher loads the bearer from an external store such as OS Keychain); "
                f"update that external store or set {EXTERNAL_ROTATE_CMD_ENV} before rotating"
            )
        raise PlistConfigError(
            f"{plist} lacks a rotatable {HTTP_TOKEN_KEY}; rerender before rotating"
        )
    if not valid_token(token_from_payload(payload, plist)):
        if shape is BearerShape.SECRETS_WRAPPER:
            raise PlistConfigError(
                f"{plist} lacks a rotatable {HTTP_TOKEN_KEY} in the secrets file its wrapper reads"
            )
        raise PlistConfigError(
            f"{plist} lacks a rotatable {HTTP_TOKEN_KEY}; rerender before rotating"
        )
    return shape


def rotate_token(
    plist: Path,
    token: str,
    *,
    allow_external_store: bool = False,
    external_rotate_cmd: str | None = None,
) -> BearerShape:
    """Install ``token`` as ``plist``'s bearer according to its ``BearerShape``."""
    if not valid_token(token):
        raise PlistConfigError("refusing to install an empty or placeholder bearer")
    shape = rotation_preflight(
        plist,
        allow_external_store=allow_external_store,
        external_rotate_cmd=external_rotate_cmd,
    )
    if shape is BearerShape.SECRETS_WRAPPER:
        current = token_from_payload(load_plist(plist), plist)
        if not isinstance(current, str) or not secrets.compare_digest(current, token):
            raise PlistConfigError(
                f"{plist} takes {HTTP_TOKEN_KEY} from the secrets file, "
                "which does not hold the new value; rotate the secrets file first"
            )
        return shape
    if shape is BearerShape.EXTERNAL_STORE:
        cmd = (
            external_rotate_cmd
            if external_rotate_cmd is not None
            else os.environ.get(EXTERNAL_ROTATE_CMD_ENV, "")
        ).strip()
        proc = subprocess.run(
            ["/bin/sh", "-c", cmd],
            input=token,
            text=True,
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
            raise PlistConfigError(
                f"external bearer rotation hook ({EXTERNAL_ROTATE_CMD_ENV}) failed for {plist}: {detail}"
            )
        return shape
    payload = load_plist(plist)
    plist_environment(payload, plist)[HTTP_TOKEN_KEY] = token
    mode = stat.S_IMODE(plist.stat().st_mode)
    write_plist(plist, payload, mode=mode)
    return shape
