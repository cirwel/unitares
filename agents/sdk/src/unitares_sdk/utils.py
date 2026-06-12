"""Shared utilities for UNITARES agents — extracted from vigil/sentinel."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _mkdir_private(directory: Path) -> None:
    """Create ``directory`` and any missing ancestors with mode 0o700.

    ``Path.mkdir(parents=True, mode=...)`` applies the mode only to the leaf;
    intermediate dirs (e.g. ``~/.unitares`` under ``~/.unitares/anchors``)
    would silently get umask defaults.
    """
    missing = []
    current = directory
    while not current.exists():
        missing.append(current)
        current = current.parent
    for d in reversed(missing):
        d.mkdir(mode=0o700, exist_ok=True)


def atomic_write(path: Path, data: str, mode: int = 0o600) -> None:
    """Write data to a file atomically via temp file + os.replace.

    File is created with ``mode`` (default 0o600 — owner read/write only).
    ``tempfile.mkstemp`` already creates temp files 0o600 on POSIX, but
    ``os.fchmod`` is called explicitly as defense-in-depth: anchor and
    session files carry continuity tokens, and a future Python/OS change
    to mkstemp defaults would silently regress every caller.

    Parent directories are created 0o700 (umask-masked) — anchor/state dirs
    are agent-private. The file is fsync'd before the rename so a crash
    cannot replace a good anchor with an empty one.

    Raises on failure (OSError etc.) after cleaning up the temp file —
    callers persisting identity anchors must hear about a failed write, or
    the next restart silently loses identity (the 2026-04-19 silent-fork
    class). Existing call sites already wrap in try/except where best-effort
    behavior is intended.
    """
    fd = None
    tmp = None
    try:
        _mkdir_private(path.parent)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        os.write(fd, data.encode())
        os.fsync(fd)
        os.fchmod(fd, mode)
        os.close(fd)
        fd = None
        os.replace(tmp, str(path))
        tmp = None
    except Exception:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        raise
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _applescript_escape(text: str) -> str:
    """Escape a string for embedding in a double-quoted AppleScript literal.

    Notification text includes exception messages, which can carry
    server-influenced content — without escaping, an embedded ``"`` breaks
    out of the literal and the remainder executes as AppleScript.
    """
    return text.replace("\\", "\\\\").replace('"', '\\"')


_NOTIFY_MAX_LEN = 256


def notify(title: str, message: str) -> None:
    """Send a macOS notification via osascript. No-op on non-macOS.

    Title and message are escaped (and bounded) before being embedded in
    the AppleScript literal — see :func:`_applescript_escape`.
    """
    if sys.platform != "darwin":
        return
    title = _applescript_escape(title[:_NOTIFY_MAX_LEN])
    message = _applescript_escape(message[:_NOTIFY_MAX_LEN])
    try:
        subprocess.Popen(
            [
                "osascript",
                "-e",
                f'display notification "{message}" with title "{title}"',
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def load_json_state(path: Path) -> dict:
    """Load JSON state from file. Returns {} if missing or corrupt.

    Handles the current dict format and legacy bare-string format
    (migrated to dict on read). A file that exists but cannot be read as
    state logs a warning — for identity anchors, silently returning {}
    is the first step of a silent identity fork, so the corruption must
    at least be visible in the agent's log.
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        if isinstance(data, dict):
            return data
        if isinstance(data, str) and data:
            return {"client_session_id": data}
        logger.warning("state file %s has unexpected shape %s; ignoring", path, type(data).__name__)
        return {}
    except (json.JSONDecodeError, OSError):
        try:
            text = path.read_text().strip()
            if text and not text.startswith(("{", "[")):
                return {"client_session_id": text}
        except Exception:
            pass
    logger.warning("state file %s exists but is unreadable/corrupt; treating as empty", path)
    return {}


def save_json_state(path: Path, state: dict) -> None:
    """Save JSON state atomically.

    Non-JSON-serializable values (datetime, Path, custom objects) are coerced
    to their str() representation rather than raising TypeError — matching the
    defensive behavior that Vigil's original save_state override provided.
    """
    atomic_write(path, json.dumps(state, default=str))


def parse_continuity_token(token: str) -> dict | None:
    """Parse a v1.<payload>.<sig> continuity token.

    Extracts the payload (base64url-decoded JSON with aid, model, exp, etc.).
    Returns None if the token is malformed or not v1 format.
    Does NOT verify the HMAC signature — that's the server's responsibility.
    """
    try:
        parts = token.split(".")
        if len(parts) != 3 or parts[0] != "v1":
            return None
        # base64url decode with padding
        payload_bytes = base64.urlsafe_b64decode(parts[1] + "==")
        payload = json.loads(payload_bytes.decode())
        if not isinstance(payload, dict):
            return None
        return payload
    except Exception:
        return None


def trim_log(log_file: Path, max_lines: int) -> None:
    """Keep log_file bounded to the last ``max_lines`` lines.

    Silent no-op on OSError or if the file doesn't exist — log rotation
    should never be the reason an agent crashes.
    """
    if not log_file.exists():
        return
    try:
        lines = log_file.read_text().splitlines()
    except OSError:
        return
    if len(lines) <= max_lines:
        return
    try:
        log_file.write_text("\n".join(lines[-max_lines:]) + "\n")
    except OSError:
        pass


def validate_token_uuid(token: str, expected_uuid: str) -> bool:
    """Parse token, extract aid, return True if it matches expected_uuid.

    Returns False if token is unparseable or aid doesn't match.
    """
    payload = parse_continuity_token(token)
    if payload is None:
        return False
    aid = payload.get("aid")
    if not aid:
        return False
    return aid == expected_uuid


def capture_process_fingerprint(
    transport: str = "unknown",
    anchor_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the client-reported process_fingerprint for onboard().

    Concurrent identity binding invariant (issue #123). The server uses this
    tuple to detect same-UUID siphoning across live execution contexts. The
    fingerprint is declaration-only: it is recorded for audit, never used to
    resolve or recover identity.

    Fields:
      - host_id: stable per-machine identifier (hostname + machine-id hash)
      - pid, pid_start_time: identify the current process even across PID reuse
      - ppid: optional evidence for lineage verification
      - tty: nullable — daemons have no controlling TTY
      - transport: caller-declared MCP channel (stdio/http/websocket/...)
      - anchor_path_hash: SHA-256 of the resident's anchor file path if any

    All fields are best-effort: any capture failure yields a skipped field
    rather than an exception. The caller passes the resulting dict straight
    into onboard(process_fingerprint=...).
    """
    fp: Dict[str, Any] = {}

    try:
        hostname = socket.gethostname()
    except Exception:
        hostname = "unknown"

    machine_id = ""
    for candidate in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            with open(candidate, "r") as f:
                machine_id = f.read().strip()
            if machine_id:
                break
        except Exception:
            continue
    if not machine_id and sys.platform == "darwin":
        try:
            out = subprocess.check_output(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                stderr=subprocess.DEVNULL,
                timeout=2,
            ).decode()
            for line in out.splitlines():
                if "IOPlatformUUID" in line:
                    machine_id = line.split('"')[-2]
                    break
        except Exception:
            pass

    fp["host_id"] = hashlib.sha256(
        f"{hostname}:{machine_id}".encode()
    ).hexdigest()[:16]

    try:
        fp["pid"] = os.getpid()
    except Exception:
        pass

    try:
        fp["ppid"] = os.getppid()
    except Exception:
        pass

    try:
        import psutil  # type: ignore
        fp["pid_start_time"] = psutil.Process().create_time()
    except Exception:
        # Linux fallback: parse /proc/self/stat field 22 (starttime in clock ticks
        # since boot). Combine with /proc/stat's btime to get epoch seconds.
        try:
            with open(f"/proc/{os.getpid()}/stat", "r") as f:
                stat_fields = f.read().split()
            starttime_ticks = int(stat_fields[21])
            with open("/proc/stat", "r") as f:
                for line in f:
                    if line.startswith("btime "):
                        btime = int(line.split()[1])
                        break
                else:
                    btime = 0
            hz = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
            if hz > 0 and btime > 0:
                fp["pid_start_time"] = float(btime + starttime_ticks / hz)
        except Exception:
            pass

    try:
        if os.isatty(0):
            fp["tty"] = os.ttyname(0)
    except Exception:
        pass

    if transport:
        fp["transport"] = transport

    if anchor_path:
        try:
            fp["anchor_path_hash"] = hashlib.sha256(
                anchor_path.encode()
            ).hexdigest()[:16]
        except Exception:
            pass

    return fp
