"""
Agent process management.

PID/lock files, process cleanup, signal handling, server initialization.
"""

from __future__ import annotations

import os
import signal
import atexit
import time
from pathlib import Path

from src.logging_utils import get_logger
from src.agent_metadata_model import project_root

logger = get_logger(__name__)

# Optional dependency flags
try:
    import aiofiles  # noqa: F401 — availability probe
    AIOFILES_AVAILABLE = True
except ImportError:
    AIOFILES_AVAILABLE = False
    logger.warning("aiofiles not available. File I/O will be synchronous. Install with: pip install aiofiles")

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
    logger.warning("psutil not available. Process cleanup disabled. Install with: pip install psutil")

# PID file for process tracking
PID_FILE = Path(project_root) / "data" / ".mcp_server.pid"
LOCK_FILE = Path(project_root) / "data" / ".mcp_server.lock"

MAX_KEEP_PROCESSES = 42
CURRENT_PID = os.getpid()

# Initialize managers
from src.state_locking import StateLockManager
from src.health_thresholds import HealthThresholds
from src.process_cleanup import ProcessManager

lock_manager = StateLockManager()
health_checker = HealthThresholds()
process_mgr = ProcessManager()

# Track server start time for loop detection grace period
from datetime import datetime
SERVER_START_TIME = datetime.now()

# Global flag for graceful shutdown
_shutdown_requested = False


def signal_handler(signum, frame):
    """Handle shutdown signals gracefully"""
    global _shutdown_requested
    _shutdown_requested = True


def write_pid_file():
    """Write PID file for process tracking"""
    try:
        PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(PID_FILE, 'w') as f:
            f.write(f"{CURRENT_PID}\n")
    except Exception as e:
        logger.warning(f"Could not write PID file: {e}", exc_info=True)


def remove_pid_file():
    """Remove PID file on shutdown"""
    try:
        if PID_FILE.exists():
            PID_FILE.unlink()
    except Exception as e:
        logger.warning(f"Could not remove PID file: {e}", exc_info=True)


def init_server_process():
    """
    Initialize server-process-specific state.

    Call this from the server's main() function. Registers signal handlers,
    writes PID file, and starts heartbeat. NOT called on simple import.
    """
    global SERVER_START_TIME
    SERVER_START_TIME = datetime.now()
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    atexit.register(remove_pid_file)
    process_mgr.write_heartbeat()
    write_pid_file()


def cleanup_stale_processes():
    """Clean up stale MCP server processes on startup - only if we have too many"""
    if not PSUTIL_AVAILABLE:
        logger.info("Skipping stale process cleanup (psutil not available)")
        return

    try:
        current_processes = []
        for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'create_time']):
            try:
                cmdline = proc.info.get('cmdline', [])
                if cmdline and any('mcp_server_std.py' in str(arg) for arg in cmdline):
                    pid = proc.info['pid']
                    if pid != CURRENT_PID:
                        create_time = proc.info.get('create_time', 0)
                        age_seconds = time.time() - create_time
                        heartbeat_file = Path(project_root) / "data" / "processes" / f"heartbeat_{pid}.txt"
                        has_recent_heartbeat = False
                        if heartbeat_file.exists():
                            try:
                                with open(heartbeat_file, 'r') as f:
                                    last_heartbeat = float(f.read())
                                heartbeat_age = time.time() - last_heartbeat
                                has_recent_heartbeat = heartbeat_age < 300
                            except (ValueError, IOError):
                                pass

                        current_processes.append({
                            'pid': pid,
                            'create_time': create_time,
                            'age_seconds': age_seconds,
                            'has_recent_heartbeat': has_recent_heartbeat
                        })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        current_processes.sort(key=lambda x: x['create_time'])

        stale_processes = []
        for proc_info in current_processes:
            if proc_info['age_seconds'] > 300 and not proc_info['has_recent_heartbeat']:
                stale_processes.append(proc_info)

        stale_pids = {p['pid'] for p in stale_processes}

        if len(current_processes) > MAX_KEEP_PROCESSES:
            processes_to_remove = current_processes[:-MAX_KEEP_PROCESSES]
            for proc_info in processes_to_remove:
                if proc_info['pid'] not in stale_pids:
                    stale_processes.append(proc_info)
                    stale_pids.add(proc_info['pid'])

        unique_stale_processes = stale_processes

        if unique_stale_processes:
            logger.info(f"Found {len(current_processes)} server processes, cleaning up {len(unique_stale_processes)} stale ones (keeping {MAX_KEEP_PROCESSES} most recent)...")

            for proc_info in unique_stale_processes:
                try:
                    proc = psutil.Process(proc_info['pid'])
                    age_minutes = int(proc_info['age_seconds'] / 60)
                    reason = "no heartbeat" if not proc_info['has_recent_heartbeat'] else "over limit"
                    logger.info(f"Killing stale process PID {proc_info['pid']} (age: {age_minutes}m, reason: {reason})")
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except psutil.TimeoutExpired:
                        proc.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                    logger.warning(f"Could not kill PID {proc_info['pid']}: {e}", exc_info=True)
    except Exception as e:
        logger.warning(f"Could not clean stale processes: {e}", exc_info=True)
