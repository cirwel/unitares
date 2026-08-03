#!/usr/bin/env python3
"""Run pytest under signal-safe supervision and publish its cache atomically."""

from __future__ import annotations

import argparse
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True)
class ProcessIdentity:
    """PID plus start token, used to avoid signaling a reused PID."""

    pid: int
    parent_pid: int
    start_token: str


def process_table() -> dict[int, ProcessIdentity]:
    """Return a portable macOS/Linux process table with identity tokens."""
    result = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,lstart="],
        check=True,
        capture_output=True,
        text=True,
    )
    records: dict[int, ProcessIdentity] = {}
    for line in result.stdout.splitlines():
        try:
            pid_text, parent_text, start_token = line.split(maxsplit=2)
            pid = int(pid_text)
            parent_pid = int(parent_text)
        except (TypeError, ValueError):
            continue
        records[pid] = ProcessIdentity(pid, parent_pid, start_token)
    return records


def descendant_identities(
    root_pid: int,
    table: dict[int, ProcessIdentity] | None = None,
) -> dict[int, ProcessIdentity]:
    """Snapshot recursive descendants, including new-session children."""
    records = process_table() if table is None else table
    children: dict[int, set[int]] = {}
    for record in records.values():
        children.setdefault(record.parent_pid, set()).add(record.pid)

    found: dict[int, ProcessIdentity] = {}
    pending = [root_pid]
    while pending:
        parent_pid = pending.pop()
        for child_pid in children.get(parent_pid, ()):
            if child_pid not in found:
                found[child_pid] = records[child_pid]
                pending.append(child_pid)
    return found


def atomic_publish(output_path: Path, cache_file: Path, cache_format: str) -> None:
    """Publish a complete, versioned cache entry with a same-directory rename."""
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=cache_file.parent,
        prefix=f".{cache_file.name}.tmp.",
    )
    temporary_path = Path(temporary_name)
    try:
        summary_lines: deque[str] = deque(maxlen=5)
        with output_path.open("r", encoding="utf-8", errors="replace") as output:
            for line in output:
                summary_lines.append(line)
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as cache_output:
            cache_output.write(f"{cache_format}\n")
            cache_output.writelines(summary_lines)
            cache_output.flush()
            os.fsync(cache_output.fileno())
        file_descriptor = -1
        os.replace(temporary_path, cache_file)
    except BaseException:
        if file_descriptor >= 0:
            try:
                os.close(file_descriptor)
            except OSError:
                pass
        temporary_path.unlink(missing_ok=True)
        raise


def prune_cache(cache_directory: Path, keep: int = 20) -> None:
    """Remove older published cache entries while ignoring temporary files."""
    entries = sorted(
        (
            path
            for path in cache_directory.iterdir()
            if path.is_file() and not path.name.startswith(".")
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for entry in entries[keep:]:
        entry.unlink(missing_ok=True)


class PytestSupervisor:
    """Stream pytest output while owning signal and descendant cleanup."""

    def __init__(
        self,
        command: Sequence[str],
        output_path: Path,
        cache_file: Path,
        cache_format: str,
        cache_label: str,
        lock_directory: Path,
    ) -> None:
        """Initialize a supervisor without spawning pytest yet."""
        self.command = list(command)
        self.output_path = output_path
        self.cache_file = cache_file
        self.cache_format = cache_format
        self.cache_label = cache_label
        self.lock_directory = lock_directory
        self.process: subprocess.Popen[bytes] | None = None
        self.pending_signal: int | None = None
        self.descendants: dict[int, ProcessIdentity] = {}
        self.descendants_lock = threading.Lock()
        self.tracker_stop = threading.Event()
        self.tracker: threading.Thread | None = None

    def _snapshot_descendants(self) -> dict[int, ProcessIdentity]:
        """Snapshot descendants without allowing process-inspection failure to escape."""
        if self.process is None:
            return {}
        try:
            return descendant_identities(self.process.pid)
        except (OSError, subprocess.SubprocessError):
            return {}

    def _track_descendants(self) -> None:
        """Retain identities before children can detach or become reparented."""
        while not self.tracker_stop.is_set():
            snapshot = self._snapshot_descendants()
            if snapshot:
                with self.descendants_lock:
                    self.descendants.update(snapshot)
            if self.process is not None and self.process.poll() is not None:
                return
            self.tracker_stop.wait(0.02)

    def _live_identities(
        self,
        identities: dict[int, ProcessIdentity],
    ) -> dict[int, ProcessIdentity]:
        """Filter captured identities against current PID start tokens."""
        try:
            current = process_table()
        except (OSError, subprocess.SubprocessError):
            return identities
        return {
            pid: identity
            for pid, identity in identities.items()
            if (current_identity := current.get(pid)) is not None
            and current_identity.start_token == identity.start_token
        }

    def _signal_processes(
        self,
        signum: int,
        identities: dict[int, ProcessIdentity],
    ) -> None:
        """Signal the pytest process group and identity-verified descendants."""
        if self.process is None:
            return
        try:
            os.killpg(self.process.pid, signum)
        except (PermissionError, ProcessLookupError):
            pass
        for pid in self._live_identities(identities):
            try:
                os.kill(pid, signum)
            except (PermissionError, ProcessLookupError):
                pass

    def _handle_signal(self, signum: int, _frame: object) -> None:
        """Terminate the full tracked tree, then report a conventional signal exit."""
        if self.process is None:
            self.pending_signal = signum
            return

        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        initial = self._snapshot_descendants()
        with self.descendants_lock:
            self.descendants.update(initial)
            captured = dict(self.descendants)
        self._signal_processes(signum, captured)

        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            current = self._snapshot_descendants()
            if current:
                with self.descendants_lock:
                    self.descendants.update(current)
                    captured = dict(self.descendants)
                self._signal_processes(signum, current)
            survivors = self._live_identities(captured)
            if self.process.poll() is not None and not survivors:
                break
            time.sleep(0.05)

        with self.descendants_lock:
            captured = dict(self.descendants)
        survivors = self._live_identities(captured)
        if self.process.poll() is None or survivors:
            self._signal_processes(signal.SIGKILL, survivors)
        try:
            self.process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            self._signal_processes(signal.SIGKILL, survivors)
            self.process.wait()
        raise SystemExit(128 + signum)

    def _stream_output(self) -> None:
        """Copy pytest output to the terminal and the temporary summary source."""
        assert self.process is not None
        assert self.process.stdout is not None
        output_descriptor = self.process.stdout.fileno()
        with self.output_path.open("wb") as output:
            while True:
                readable, _, _ = select.select([output_descriptor], [], [], 0.1)
                if not readable:
                    continue
                chunk = os.read(output_descriptor, 65536)
                if not chunk:
                    return
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
                output.write(chunk)
                output.flush()

    def run(self) -> int:
        """Run pytest, publish only successful output, and always release the lock."""
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)
        try:
            self.process = subprocess.Popen(
                self.command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            self.tracker = threading.Thread(
                target=self._track_descendants,
                name="test-cache-descendant-tracker",
                daemon=True,
            )
            self.tracker.start()
            if self.pending_signal is not None:
                self._handle_signal(self.pending_signal, None)

            self._stream_output()
            exit_code = self.process.wait()
            if exit_code == 0:
                atomic_publish(self.output_path, self.cache_file, self.cache_format)
                print(f"[test-cache] CACHED — {self.cache_label}", flush=True)
            else:
                print(
                    f"[test-cache] FAILED (exit {exit_code}) — not cached",
                    flush=True,
                )
            prune_cache(self.cache_file.parent)
            return exit_code
        finally:
            self.tracker_stop.set()
            if self.tracker is not None:
                self.tracker.join(timeout=1.0)
            self.output_path.unlink(missing_ok=True)
            shutil.rmtree(self.lock_directory, ignore_errors=True)


def parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse supervisor metadata and the pytest command after `--`."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--cache-file", required=True, type=Path)
    parser.add_argument("--cache-format", required=True)
    parser.add_argument("--cache-label", required=True)
    parser.add_argument("--lock-dir", required=True, type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    parsed = parser.parse_args(arguments)
    if parsed.command and parsed.command[0] == "--":
        parsed.command = parsed.command[1:]
    if not parsed.command:
        parser.error("pytest command is required after --")
    return parsed


def main(arguments: Sequence[str] | None = None) -> int:
    """CLI entry point."""
    parsed = parse_args(arguments)
    supervisor = PytestSupervisor(
        command=parsed.command,
        output_path=parsed.output,
        cache_file=parsed.cache_file,
        cache_format=parsed.cache_format,
        cache_label=parsed.cache_label,
        lock_directory=parsed.lock_dir,
    )
    return supervisor.run()


if __name__ == "__main__":
    raise SystemExit(main())
