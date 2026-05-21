"""S19 peer-attestation primitives — macOS backend.

Pure-Python helpers for the four kernel/launchd queries the substrate-claim
verification path needs:

- ``read_peer_pid(sock)``           — kernel-attested PID of a UDS peer
- ``read_service_label(pid)``       — launchd label currently owning a PID
- ``read_executable_path(pid)``     — absolute binary path of a process
- ``read_process_start_time(pid)``  — Unix epoch seconds for PID-reuse detection

See ```` v2 §Verification at
connection-accept for how these compose into the M3-v2 attestation flow.

This module is dependency-light by design (stdlib only): it has to be
importable from any process that needs to reason about substrate identity,
and it must not pull in the MCP server's anyio task group. The eventual
caller (PR3) wraps these helpers in ``loop.run_in_executor`` to honor the
anyio-deadlock constraint described in CLAUDE.md "Known Issue: anyio-asyncio
Conflict."

Linux backend is stubbed (``NotImplementedError``) until the first Linux
substrate-anchored resident lands; see proposal v2 §Open questions deferred.
"""
from __future__ import annotations

import ctypes
import re
import socket
import struct
import subprocess
import sys
from typing import Optional


# =============================================================================
# Platform gate
# =============================================================================


def _require_supported_platform() -> None:
    """Raise NotImplementedError when called on an unsupported platform.

    Accepts macOS today. Linux is a future-work stub with a specific message
    pointing at the proposal so a future implementer knows where to start.
    """
    if sys.platform == "darwin":
        return
    if sys.platform == "linux":
        raise NotImplementedError(
            "S19 Linux backend not yet implemented — see "
 " §Open questions deferred"
        )
    raise NotImplementedError(
        f"S19 attestation not supported on {sys.platform!r}"
    )


# =============================================================================
# Peer PID via LOCAL_PEERPID
# =============================================================================
#
# macOS exposes the connected peer's PID on a Unix-domain socket via
# getsockopt(SOL_LOCAL, LOCAL_PEERPID). The PID is written by the kernel at
# connect/accept and cannot be forged by user-space — this is the kernel
# attestation that closes adversary A1 (proposal v2 §Adversary models).
#
# Constants from <sys/un.h>; not exposed by Python's socket module:
#   SOL_LOCAL    = 0
#   LOCAL_PEERPID = 0x002
_SOL_LOCAL = 0
_LOCAL_PEERPID = 0x002


def read_peer_pid(sock: socket.socket) -> Optional[int]:
    """Return the kernel-attested PID of the peer connected to ``sock``.

    ``sock`` must be a connected AF_UNIX socket. Returns ``None`` when the
    socket family is wrong or the syscall fails.
    """
    _require_supported_platform()
    if sock.family != socket.AF_UNIX:
        return None
    try:
        raw = sock.getsockopt(_SOL_LOCAL, _LOCAL_PEERPID, struct.calcsize("i"))
    except OSError:
        return None
    if not raw or len(raw) < struct.calcsize("i"):
        return None
    return int(struct.unpack("i", raw)[0])


# =============================================================================
# launchd label for a PID
# =============================================================================
#
# `launchctl list` output is three whitespace-separated columns: PID, Status,
# Label. PID is '-' for loaded-but-not-running services. We parse the column
# format rather than the per-PID `launchctl print pid/<N>` output because
# the latter does not expose the label directly (verified on the running
# macOS at investigation time).
#
# This format has been stable across macOS 10.x → 15.x; if Apple ever
# changes it, the regex below misses cleanly (returns None) instead of
# false-accepting a wrong label.
_LAUNCHCTL_LIST_RE = re.compile(r"^\s*(-|\d+)\s+(-?\d+)\s+(\S+)\s*$")
_LAUNCHCTL_TIMEOUT_SECONDS = 2.0


def read_service_label(pid: int) -> Optional[str]:
    """Return the launchd service label owning ``pid``, or ``None`` if not
    running under a launchd job (or the PID is not in this user's launchd
    domain).

    The verification path uses this to confirm a connecting peer's PID is
    running under the substrate-claim's registered label — closes the naive
    A2 adversary (proposal v2 §Adversary models).
    """
    _require_supported_platform()
    if pid <= 0:
        return None
    try:
        result = subprocess.run(
            ["launchctl", "list"],
            capture_output=True, text=True,
            timeout=_LAUNCHCTL_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None

    target = str(pid)
    for line in result.stdout.splitlines():
        m = _LAUNCHCTL_LIST_RE.match(line)
        if not m:
            continue
        # m.group(1) is '-' for loaded-but-not-running rows; only literal
        # int-string match counts. The dash rows can never accidentally
        # match because pid > 0 so str(pid) is never '-'.
        if m.group(1) == target:
            return m.group(3)
    return None


# =============================================================================
# Executable path via proc_pidpath
# =============================================================================
#
# proc_pidpath() in libproc returns the absolute path of a process's
# executable. Stable since macOS 10.5. PROC_PIDPATHINFO_MAXSIZE is
# 4 * MAXPATHLEN = 4096 bytes, defined in <libproc.h>.
_PROC_PIDPATHINFO_MAXSIZE = 4 * 1024

_libproc: Optional[ctypes.CDLL] = (
    ctypes.CDLL("/usr/lib/libproc.dylib") if sys.platform == "darwin" else None
)
if _libproc is not None:
    _libproc.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    _libproc.proc_pidpath.restype = ctypes.c_int


def read_executable_path(pid: int) -> Optional[str]:
    """Return ``pid``'s absolute executable path, or ``None`` on failure.

    Used by the verification path to compare against
    ``substrate_claims.expected_executable_path`` — closes A2-escalated
    (binary-substitution attacker; proposal v2 §Adversary models).
    """
    _require_supported_platform()
    if _libproc is None:
        return None
    if pid <= 0:
        return None
    buf = ctypes.create_string_buffer(_PROC_PIDPATHINFO_MAXSIZE)
    rc = _libproc.proc_pidpath(pid, buf, _PROC_PIDPATHINFO_MAXSIZE)
    if rc <= 0:
        return None
    try:
        return buf.value.decode("utf-8")
    except UnicodeDecodeError:
        return None


# =============================================================================
# Process start time via proc_pidinfo (PROC_PIDTBSDINFO)
# =============================================================================
#
# proc_pidinfo() with the PROC_PIDTBSDINFO selector returns BSD task info
# including pbi_start_tvsec — the process start time in Unix epoch seconds.
# Stable since macOS 10.5.
#
# The verified_pairs cache (PR3 §Sequencing step 3 sub-7) uses this to
# detect PID reuse: even when a PID is recycled, the new process has a
# different start_tvsec. Defeats Q3(e) per the council adversary review.
_PROC_PIDTBSDINFO = 3


class _ProcBsdInfo(ctypes.Structure):
    """Mirror of ``struct proc_bsdinfo`` from <sys/proc_info.h>.

    Only fields up through ``pbi_start_tvsec`` are load-bearing for our
    needs. The layout up to that field is stable across all supported
    macOS versions.
    """
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


if _libproc is not None:
    _libproc.proc_pidinfo.argtypes = [
        ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
        ctypes.c_void_p, ctypes.c_int,
    ]
    _libproc.proc_pidinfo.restype = ctypes.c_int


def read_process_start_time(pid: int) -> Optional[int]:
    """Return ``pid``'s start time in Unix epoch seconds, or ``None`` on
    failure.
    """
    _require_supported_platform()
    if _libproc is None:
        return None
    if pid <= 0:
        return None
    info = _ProcBsdInfo()
    rc = _libproc.proc_pidinfo(
        pid, _PROC_PIDTBSDINFO, 0,
        ctypes.byref(info), ctypes.sizeof(info),
    )
    if rc <= 0:
        return None
    return int(info.pbi_start_tvsec)
