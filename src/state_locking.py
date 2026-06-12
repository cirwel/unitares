"""
State Locking Manager for Multi-Process Coordination

Ensures only one process can modify agent state at a time using file-based locking.
Prevents race conditions and state corruption in multi-process MCP environments.

Features:
- Automatic stale lock cleanup before acquisition attempts
- Exponential backoff retry with automatic recovery
- Process health checking to detect stale locks
- Async support for non-blocking lock acquisition in async contexts
"""

import fcntl
import os
import time
import json
from pathlib import Path
from contextlib import contextmanager, asynccontextmanager
from typing import Optional


def is_process_alive(pid: int) -> bool:
    """Check if a process with given PID is still running"""
    try:
        os.kill(pid, 0)  # Signal 0 doesn't kill, just checks if process exists
        return True
    except (OSError, ProcessLookupError):
        return False


class StateLockManager:
    """Ensures only one process can modify agent state at a time"""
    
    def __init__(
        self, 
        lock_dir: Optional[Path] = None, 
        auto_cleanup_stale: bool = True, 
        stale_threshold: float = 60.0
    ) -> None:
        if lock_dir is None:
            # Use project data directory for locks
            project_root = Path(__file__).parent.parent
            lock_dir = project_root / "data" / "locks"
        self.lock_dir = lock_dir
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        self.auto_cleanup_stale = auto_cleanup_stale
        self.stale_threshold = stale_threshold  # Seconds before considering lock stale
        
    def _check_and_clean_stale_lock(self, lock_file: Path) -> bool:
        """
        Check if lock file is stale and clean it if so.
        Returns True if lock was cleaned, False otherwise.
        
        Strategy:
        1. Try to acquire a non-blocking lock - if we can, the lock is stale
        2. If we can't acquire it, try to read the lock file to check process status
        3. Only delete if we're certain the process is dead
        """
        if not lock_file.exists():
            return False
        
        # First, try to acquire the lock non-blocking to see if it's actually held
        # If we can acquire it immediately, the lock is stale
        test_fd = None
        try:
            test_fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR)
            try:
                # Try non-blocking exclusive lock
                fcntl.flock(test_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                # If we got here, the lock was NOT held - it's stale!
                # Release our test lock and delete the file
                fcntl.flock(test_fd, fcntl.LOCK_UN)
                os.close(test_fd)
                test_fd = None
                lock_file.unlink(missing_ok=True)
                return True
            except IOError:
                # Lock is held by another process - check if that process is alive
                pass
            finally:
                if test_fd is not None:
                    try:
                        fcntl.flock(test_fd, fcntl.LOCK_UN)
                    except (IOError, OSError):
                        pass
                    os.close(test_fd)
        except (IOError, OSError):
            # Can't open lock file - might be actively locked or permission issue
            # Don't delete it
            return False
        
        # Lock is held - check if the holding process is still alive
        try:
            # Try to read lock info with a shared lock (non-blocking)
            read_fd = os.open(str(lock_file), os.O_RDONLY)
            try:
                # Try to acquire shared lock non-blocking
                fcntl.flock(read_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                # Got shared lock - can read the file
                os.lseek(read_fd, 0, os.SEEK_SET)
                content = os.read(read_fd, 4096).decode('utf-8', errors='ignore')
                if content:
                    try:
                        lock_data = json.loads(content)
                        pid = lock_data.get('pid')
                        timestamp = lock_data.get('timestamp', 0)
                        
                        if pid is None:
                            # No PID means stale/corrupted
                            fcntl.flock(read_fd, fcntl.LOCK_UN)
                            os.close(read_fd)
                            lock_file.unlink(missing_ok=True)
                            return True
                        
                        # Check if process is alive
                        if not is_process_alive(pid):
                            # Process is dead, lock is stale
                            fcntl.flock(read_fd, fcntl.LOCK_UN)
                            os.close(read_fd)
                            lock_file.unlink(missing_ok=True)
                            return True
                        
                        # Check if lock timestamp is too old
                        if timestamp > 0:
                            lock_age = time.time() - timestamp
                            if lock_age > self.stale_threshold:
                                # Lock is old - double-check process is actually dead
                                if not is_process_alive(pid):
                                    fcntl.flock(read_fd, fcntl.LOCK_UN)
                                    os.close(read_fd)
                                    lock_file.unlink(missing_ok=True)
                                    return True
                    except (json.JSONDecodeError, ValueError):
                        # Corrupted lock file
                        fcntl.flock(read_fd, fcntl.LOCK_UN)
                        os.close(read_fd)
                        lock_file.unlink(missing_ok=True)
                        return True
                fcntl.flock(read_fd, fcntl.LOCK_UN)
            except IOError:
                # Can't acquire shared lock - lock is actively held
                pass
            finally:
                os.close(read_fd)
        except (IOError, OSError):
            # Can't read lock file - might be actively locked
            # Don't delete it
            pass
        
        return False
    
    @contextmanager
    def acquire_agent_lock(self, agent_id: str, timeout: float = 5.0, max_retries: int = 3):
        """
        Acquire exclusive lock for agent state updates with automatic recovery.
        
        Args:
            agent_id: Agent identifier
            timeout: Timeout per retry attempt in seconds
            max_retries: Maximum number of retry attempts with cleanup
        """
        lock_file = self.lock_dir / f"{agent_id}.lock"
        lock_fd = None
        
        # Automatic stale lock cleanup before attempting acquisition
        if self.auto_cleanup_stale:
            try:
                self._check_and_clean_stale_lock(lock_file)
            except Exception:
                # Non-critical, continue with lock acquisition
                pass
        
        # Retry loop with exponential backoff and automatic cleanup
        last_error = None
        lock_fd = None  # Initialize outside loop to ensure cleanup on final failure
        
        try:
            for attempt in range(max_retries):
                start_time = time.time()
                lock_fd = None

                # Create lock file if doesn't exist
                lock_fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR)

                # Try to acquire lock with timeout
                while time.time() - start_time < timeout:
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        # Write PID and timestamp to lock file for debugging
                        lock_info = {
                            "pid": os.getpid(),
                            "timestamp": time.time(),
                            "agent_id": agent_id
                        }
                        os.ftruncate(lock_fd, 0)  # Clear file
                        os.write(lock_fd, json.dumps(lock_info).encode())
                        os.fsync(lock_fd)  # Ensure written to disk
                        
                        # Lock acquired successfully - yield control
                        try:
                            yield  # Lock acquired, allow operation
                        finally:
                            # Always release lock when exiting context
                            try:
                                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                            except (IOError, OSError):
                                pass
                            try:
                                os.close(lock_fd)
                            except (OSError, ValueError):
                                # File descriptor already closed or invalid
                                pass
                            lock_fd = None  # Mark as closed
                        return  # Success, exit retry loop
                    except IOError:
                        # Lock is held by another process, wait and retry
                        time.sleep(0.1)
                
                # Timeout reached - close file descriptor before retry
                if lock_fd:
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    except (IOError, OSError):
                        pass
                    try:
                        os.close(lock_fd)
                    except (OSError, ValueError):
                        # File descriptor already closed or invalid
                        pass
                    lock_fd = None
                
                # Before retrying, check if lock is stale and clean it
                if attempt < max_retries - 1:  # Don't clean on last attempt
                    if self.auto_cleanup_stale:
                        try:
                            cleaned = self._check_and_clean_stale_lock(lock_file)
                            if cleaned:
                                # Wait a bit before retrying after cleanup
                                time.sleep(0.2)
                        except Exception:
                            pass
                    
                    # Exponential backoff: wait longer on each retry
                    wait_time = 0.2 * (2 ** attempt)
                    time.sleep(wait_time)

        except Exception as e:
            # Close file descriptor on error (only if not already closed)
            if lock_fd is not None:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except (IOError, OSError):
                    pass
                try:
                    os.close(lock_fd)
                except (OSError, ValueError):
                    # File descriptor already closed or invalid - this is OK
                    pass
                lock_fd = None

            last_error = e
        finally:
            # Ensure lock_fd is closed even if we exit the loop early
            if lock_fd is not None:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except (IOError, OSError):
                    pass
                try:
                    os.close(lock_fd)
                except (IOError, OSError):
                    pass
        
        # All retries exhausted - raise appropriate error
        if last_error:
            raise last_error
        else:
            raise TimeoutError(
                f"Lock timeout for agent '{agent_id}' after {max_retries} attempts. "
                f"Another process may be updating this agent. Try: wait and retry, or use cleanup_stale_locks tool."
            )
    
    @asynccontextmanager
    async def acquire_agent_lock_async(self, agent_id: str, timeout: float = 5.0, max_retries: int = 3):
        """
        Async version of acquire_agent_lock - uses asyncio.sleep() instead of time.sleep()
        to avoid blocking the event loop.
        
        Use this in async handlers (like MCP handlers) to prevent blocking.
        
        Args:
            agent_id: Agent identifier
            timeout: Timeout per retry attempt in seconds
            max_retries: Maximum number of retry attempts with cleanup
        """
        lock_file = self.lock_dir / f"{agent_id}.lock"
        lock_fd = None
        
        # Automatic stale lock cleanup before attempting acquisition
        # Run in executor to avoid blocking event loop (file I/O operations)
        if self.auto_cleanup_stale:
            try:
                import asyncio
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._check_and_clean_stale_lock, lock_file)
            except Exception:
                # Non-critical, continue with lock acquisition
                pass
        
        # Retry loop with exponential backoff and automatic cleanup
        last_error = None
        lock_fd = None
        
        try:
            for attempt in range(max_retries):
                start_time = time.time()
                lock_fd = None

                # Create lock file if doesn't exist
                lock_fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR)

                # Try to acquire lock with timeout
                while time.time() - start_time < timeout:
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        # Write PID and timestamp to lock file for debugging
                        lock_info = {
                            "pid": os.getpid(),
                            "timestamp": time.time(),
                            "agent_id": agent_id
                        }
                        os.ftruncate(lock_fd, 0)  # Clear file
                        os.write(lock_fd, json.dumps(lock_info).encode())
                        os.fsync(lock_fd)  # Ensure written to disk
                        
                        # Lock acquired successfully - yield control
                        try:
                            yield  # Lock acquired, allow operation
                        finally:
                            # Always release lock when exiting context
                            try:
                                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                            except (IOError, OSError):
                                pass
                            try:
                                os.close(lock_fd)
                            except (OSError, ValueError):
                                # File descriptor already closed or invalid
                                pass
                            lock_fd = None  # Mark as closed
                        return  # Success, exit retry loop
                    except IOError:
                        # Lock is held by another process, wait and retry (NON-BLOCKING)
                        await asyncio.sleep(0.1)  # Use asyncio.sleep instead of time.sleep
                
                # Timeout reached - close file descriptor before retry
                if lock_fd:
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    except (IOError, OSError):
                        pass
                    try:
                        os.close(lock_fd)
                    except (OSError, ValueError):
                        # File descriptor already closed or invalid
                        pass
                    lock_fd = None
                
                # Before retrying, check if lock is stale and clean it
                # Run in executor to avoid blocking event loop (file I/O operations)
                if attempt < max_retries - 1:  # Don't clean on last attempt
                    if self.auto_cleanup_stale:
                        try:
                            import asyncio
                            loop = asyncio.get_running_loop()
                            cleaned = await loop.run_in_executor(None, self._check_and_clean_stale_lock, lock_file)
                            if cleaned:
                                # Wait a bit before retrying after cleanup (NON-BLOCKING)
                                await asyncio.sleep(0.2)  # Use asyncio.sleep instead of time.sleep
                        except Exception:
                            pass
                    
                    # Exponential backoff: wait longer on each retry (NON-BLOCKING)
                    wait_time = 0.2 * (2 ** attempt)
                    await asyncio.sleep(wait_time)  # Use asyncio.sleep instead of time.sleep

        except Exception as e:
            # Close file descriptor on error (only if not already closed)
            if lock_fd is not None:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except (IOError, OSError):
                    pass
                try:
                    os.close(lock_fd)
                except (OSError, ValueError):
                    # File descriptor already closed or invalid - this is OK
                    pass
                lock_fd = None

            last_error = e
        finally:
            # Ensure lock_fd is closed even if we exit the loop early
            if lock_fd is not None:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except (IOError, OSError):
                    pass
                try:
                    os.close(lock_fd)
                except (IOError, OSError):
                    pass
        
        # All retries exhausted - raise appropriate error
        if last_error:
            raise last_error
        else:
            raise TimeoutError(
                f"Lock timeout for agent '{agent_id}' after {max_retries} attempts. "
                f"Another process may be updating this agent. Try: wait and retry, or use cleanup_stale_locks tool."
            )

