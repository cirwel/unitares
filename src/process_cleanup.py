"""
Process Management for MCP Server Instances

Manages MCP server processes, prevents zombies, and tracks heartbeats.
Ensures only active processes remain running.
"""

import os
import time
from pathlib import Path
from typing import List, Dict

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False


class ProcessManager:
    """Manage MCP server processes and prevent zombies"""
    
    def __init__(self, pid_dir: Path = None):
        if pid_dir is None:
            project_root = Path(__file__).parent.parent
            pid_dir = project_root / "data" / "processes"
        self.pid_dir = pid_dir
        self.pid_dir.mkdir(parents=True, exist_ok=True)
        self.current_pid = os.getpid()
        self.heartbeat_file = self.pid_dir / f"heartbeat_{self.current_pid}.txt"
        
    def write_heartbeat(self):
        """Write current timestamp as heartbeat"""
        try:
            with open(self.heartbeat_file, 'w') as f:
                f.write(str(time.time()))
        except Exception as e:
            # Non-critical, don't fail if heartbeat can't be written
            pass
    
    def cleanup_zombies(self, max_age_seconds: int = 300, max_keep_processes: int = 72):
        """
        Remove processes with stale heartbeats or exceeding process limit.
        
        Args:
            max_age_seconds: Maximum age of heartbeat before considering stale
            max_keep_processes: Maximum number of processes to keep
        """
        if not PSUTIL_AVAILABLE:
            return []
        
        cleaned = []
        current_time = time.time()
        
        # Find all MCP server processes
        mcp_processes = []
        for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'create_time']):
            try:
                cmdline = proc.info.get('cmdline', [])
                if cmdline and any('mcp_server_std.py' in str(arg) for arg in cmdline):
                    pid = proc.info['pid']
                    if pid != self.current_pid:  # Don't kill ourselves
                        heartbeat_file = self.pid_dir / f"heartbeat_{pid}.txt"
                        age = current_time - proc.info.get('create_time', current_time)
                        
                        mcp_processes.append({
                            'pid': pid,
                            'age': age,
                            'create_time': proc.info.get('create_time', 0),
                            'heartbeat_file': heartbeat_file
                        })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        
        # Sort by creation time (oldest first)
        mcp_processes.sort(key=lambda x: x['create_time'])
        
        # Clean up stale processes (exceeding limit or old heartbeats)
        for proc_info in mcp_processes:
            pid = proc_info['pid']
            heartbeat_file = proc_info['heartbeat_file']
            
            # Check if heartbeat is stale
            heartbeat_stale = False
            if heartbeat_file.exists():
                try:
                    with open(heartbeat_file, 'r') as f:
                        last_heartbeat = float(f.read())
                    heartbeat_stale = (current_time - last_heartbeat) > max_age_seconds
                except (ValueError, IOError):
                    heartbeat_stale = True
            else:
                # No heartbeat file = stale
                heartbeat_stale = True
            
            # Clean up if stale or if we're over the limit
            should_clean = (
                heartbeat_stale or 
                len([p for p in mcp_processes if p['pid'] not in cleaned]) > max_keep_processes
            )
            
            if should_clean:
                try:
                    process = psutil.Process(pid)
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except psutil.TimeoutExpired:
                        process.kill()
                    cleaned.append(pid)
                    # Remove heartbeat file
                    if heartbeat_file.exists():
                        heartbeat_file.unlink()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    # Process already dead, clean up heartbeat file
                    if heartbeat_file.exists():
                        heartbeat_file.unlink()
        
        return cleaned
    
    def get_active_processes(self) -> List[Dict]:
        """Get list of active MCP processes with heartbeat status"""
        if not PSUTIL_AVAILABLE:
            return []
        
        processes = []
        current_time = time.time()
        
        for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'create_time', 'status']):
            try:
                cmdline = proc.info.get('cmdline', [])
                if cmdline and any('mcp_server_std.py' in str(arg) for arg in cmdline):
                    pid = proc.info['pid']
                    heartbeat_file = self.pid_dir / f"heartbeat_{pid}.txt"
                    
                    heartbeat_age = None
                    if heartbeat_file.exists():
                        try:
                            with open(heartbeat_file, 'r') as f:
                                last_heartbeat = float(f.read())
                            heartbeat_age = current_time - last_heartbeat
                        except (ValueError, IOError):
                            pass
                    
                    processes.append({
                        'pid': pid,
                        'uptime': current_time - proc.info.get('create_time', current_time),
                        'is_current': pid == self.current_pid,
                        'status': proc.info.get('status', 'unknown'),
                        'heartbeat_age_seconds': heartbeat_age,
                        'has_heartbeat': heartbeat_file.exists()
                    })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        
        return processes

