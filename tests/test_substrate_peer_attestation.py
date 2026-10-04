"""S19 peer-attestation: macOS backend tests.

Coverage:
- Platform gate (macOS supported, Linux stubbed, others rejected)
- read_peer_pid: socketpair self-PID round-trip + non-Unix-socket guard
- read_service_label: launchctl list parser fixtures + subprocess failure modes
- read_executable_path: self-introspection + invalid PID
- read_process_start_time: self-introspection + invalid PID + repeated read consistency
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
from unittest.mock import MagicMock

import pytest

from src.substrate import peer_attestation as pa


# =============================================================================
# Platform gate
# =============================================================================


def test_macos_passes_platform_gate() -> None:
    """No exception on darwin (the supported platform)."""
    if sys.platform != "darwin":
        pytest.skip("test environment is not macOS")
    pa._require_supported_platform()  # should not raise


def test_linux_raises_with_proposal_pointer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pa.sys, "platform", "linux")
    with pytest.raises(NotImplementedError, match="Linux backend"):
        pa._require_supported_platform()


def test_unknown_platform_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pa.sys, "platform", "win32")
    with pytest.raises(NotImplementedError, match="not supported"):
        pa._require_supported_platform()


# =============================================================================
# read_peer_pid
# =============================================================================


def test_read_peer_pid_returns_none_for_inet_socket() -> None:
    """An AF_INET socket is not a Unix-domain socket — guard returns None."""
    if sys.platform != "darwin":
        pytest.skip("macOS-specific test")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        assert pa.read_peer_pid(sock) is None
    finally:
        sock.close()


def test_read_peer_pid_returns_self_pid_via_socketpair() -> None:
    """A Unix socketpair: each end sees the other end's PID — both ends are us."""
    if sys.platform != "darwin":
        pytest.skip("macOS-specific test")
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        observed = pa.read_peer_pid(a)
        assert observed == os.getpid()
        observed_b = pa.read_peer_pid(b)
        assert observed_b == os.getpid()
    finally:
        a.close()
        b.close()


# =============================================================================
# read_service_label
# =============================================================================


_SAMPLE_LAUNCHCTL_LIST = """\
PID	Status	Label
3326	0	com.unitares.anima-proxy
-	0	com.unitares.health-watchdog
37807	0	com.unitares.governance-mcp
13142	-9	com.unitares.sentinel
-	0	com.apple.something.loaded-not-running
85392	0	com.unitares.ipv6-loopback-proxy
"""


def _mock_launchctl(monkeypatch: pytest.MonkeyPatch, *, returncode: int = 0,
                     stdout: str = _SAMPLE_LAUNCHCTL_LIST) -> None:
    fake = MagicMock()
    fake.returncode = returncode
    fake.stdout = stdout
    monkeypatch.setattr(pa.subprocess, "run", lambda *a, **k: fake)
    # On non-darwin CI, pa._require_supported_platform() raises NotImplementedError
    # before the subprocess mock can take effect — these tests exercise the
    # launchctl parser, which is a pure-Python operation worth testing on every
    # platform. Spoof platform=darwin so the parser is reachable everywhere.
    monkeypatch.setattr(pa.sys, "platform", "darwin")


def test_read_service_label_finds_governance_mcp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_launchctl(monkeypatch)
    assert pa.read_service_label(37807) == "com.unitares.governance-mcp"


def test_read_service_label_finds_sentinel_with_negative_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sentinel has status=-9 (last-exit-status); regex must accept negative."""
    _mock_launchctl(monkeypatch)
    assert pa.read_service_label(13142) == "com.unitares.sentinel"


def test_read_service_label_returns_none_for_unknown_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_launchctl(monkeypatch)
    assert pa.read_service_label(99999) is None


def test_read_service_label_skips_dash_pid_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Loaded-but-not-running services have '-' in PID column.

    No int PID can match the dash; verifies a PID like 0 (which Python
    string-formats as '0', never '-') doesn't false-positive.
    """
    _mock_launchctl(monkeypatch)
    # PID 0 doesn't appear in fixture and definitely shouldn't match dash rows.
    assert pa.read_service_label(0) is None


def test_read_service_label_rejects_negative_pid_without_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative PIDs are rejected before the subprocess call.

    Defense in depth: even if a caller passes a malformed PID upstream,
    we don't shell out and don't risk a false-positive on a shell quirk.
    """
    monkeypatch.setattr(pa.sys, "platform", "darwin")
    called = MagicMock()
    monkeypatch.setattr(pa.subprocess, "run", called)
    assert pa.read_service_label(-1) is None
    called.assert_not_called()


def test_read_service_label_handles_subprocess_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_launchctl(monkeypatch, returncode=1, stdout="")
    assert pa.read_service_label(123) is None


def test_read_service_label_handles_missing_launchctl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*a: object, **k: object) -> None:
        raise FileNotFoundError("launchctl")

    monkeypatch.setattr(pa.sys, "platform", "darwin")
    monkeypatch.setattr(pa.subprocess, "run", boom)
    assert pa.read_service_label(123) is None


def test_read_service_label_handles_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object, **k: object) -> None:
        raise subprocess.TimeoutExpired(["launchctl", "list"], 2.0)

    monkeypatch.setattr(pa.sys, "platform", "darwin")
    monkeypatch.setattr(pa.subprocess, "run", boom)
    assert pa.read_service_label(123) is None


def test_read_service_label_ignores_header_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 'PID Status Label' header line must not parse as a row."""
    _mock_launchctl(monkeypatch)
    # If the header row parsed, str-target='Label' would match m.group(3).
    # We're confirming the regex only matches int-like first columns.
    assert pa.read_service_label(123) is None


# =============================================================================
# read_executable_path
# =============================================================================


def test_read_executable_path_for_self_returns_python_binary() -> None:
    """proc_pidpath on our own PID returns the Python interpreter."""
    if sys.platform != "darwin":
        pytest.skip("macOS-specific test")
    path = pa.read_executable_path(os.getpid())
    assert path is not None
    assert "python" in os.path.basename(path).lower()


def test_read_executable_path_returns_absolute_path() -> None:
    if sys.platform != "darwin":
        pytest.skip("macOS-specific test")
    path = pa.read_executable_path(os.getpid())
    assert path is not None
    assert os.path.isabs(path)


def test_read_executable_path_invalid_pid_returns_none() -> None:
    """A PID that almost certainly doesn't exist returns None gracefully."""
    if sys.platform != "darwin":
        pytest.skip("macOS-specific test")
    assert pa.read_executable_path(999_999_999) is None


def test_read_executable_path_rejects_nonpositive_pid() -> None:
    if sys.platform != "darwin":
        pytest.skip("macOS-specific test")
    assert pa.read_executable_path(0) is None
    assert pa.read_executable_path(-1) is None


# =============================================================================
# read_process_start_time
# =============================================================================


def test_read_process_start_time_for_self_is_recent_unix_seconds() -> None:
    if sys.platform != "darwin":
        pytest.skip("macOS-specific test")
    import time
    start = pa.read_process_start_time(os.getpid())
    assert start is not None
    # Sanity: start time should be in the past, well after Unix epoch.
    assert 1_500_000_000 < start <= int(time.time())


def test_read_process_start_time_invalid_pid_returns_none() -> None:
    if sys.platform != "darwin":
        pytest.skip("macOS-specific test")
    assert pa.read_process_start_time(999_999_999) is None


def test_read_process_start_time_rejects_nonpositive_pid() -> None:
    if sys.platform != "darwin":
        pytest.skip("macOS-specific test")
    assert pa.read_process_start_time(0) is None
    assert pa.read_process_start_time(-1) is None


def test_read_process_start_time_consistent_across_repeated_reads() -> None:
    """Two reads of the same PID return the same start_tvsec.

    Used by the verified_pairs cache: even if PID is recycled, the new
    process has a *different* start_tvsec — the same PID's start_tvsec
    must be stable across reads.
    """
    if sys.platform != "darwin":
        pytest.skip("macOS-specific test")
    a = pa.read_process_start_time(os.getpid())
    b = pa.read_process_start_time(os.getpid())
    assert a == b
