"""
Tool Usage Tracker - Monitor which tools are actually used

Tracks tool call frequency to identify:
- Most used tools
- Unused tools (candidates for deprecation)
- Usage patterns over time
"""

from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional
import json
import fcntl
import os

# Import structured logging
from src.logging_utils import get_logger
from src.audit_log import _iter_jsonl_reverse, _parse_ts_naive
logger = get_logger(__name__)


@dataclass
class ToolUsageEntry:
    """Single tool usage event"""
    timestamp: str
    tool_name: str
    agent_id: Optional[str] = None
    success: bool = True
    error_type: Optional[str] = None


class ToolUsageTracker:
    """Tracks tool usage across the system"""
    
    # Tools that have been removed/deprecated and should be filtered from stats
    # See: docs/archive/HARD_REMOVAL_SUMMARY_20251128.md for details
    REMOVED_TOOLS = {
        'store_knowledge',
        'retrieve_knowledge',
        'search_knowledge',
        'list_knowledge',
        'update_discovery_status',
        'update_discovery',
        'find_similar_discoveries'
    }
    
    def __init__(self, log_file: Optional[Path] = None):
        if log_file is None:
            # UNITARES_TOOL_USAGE_LOG lets test/integration harnesses redirect
            # the singleton to a tmp file. Without it, subprocess-spawned
            # mcp_server.py instances (e.g. CLI integration tests) read the
            # developer-machine data/tool_usage.jsonl on every process_update
            # call — observed at 1.09M lines / 177MB on dev box, dominating
            # any test that triggers a single update.
            env_path = os.environ.get("UNITARES_TOOL_USAGE_LOG")
            if env_path:
                log_file = Path(env_path)
            else:
                project_root = Path(__file__).parent.parent
                log_file = project_root / "data" / "tool_usage.jsonl"

        self.log_file = log_file
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
    
    def log_tool_call(self, tool_name: str, agent_id: Optional[str] = None, 
                     success: bool = True, error_type: Optional[str] = None):
        """Log a tool call"""
        entry = ToolUsageEntry(
            timestamp=datetime.now().isoformat(),
            tool_name=tool_name,
            agent_id=agent_id,
            success=success,
            error_type=error_type
        )
        
        self._write_entry(entry)
    
    def _write_entry(self, entry: ToolUsageEntry):
        """Write entry to log file with locking"""
        try:
            with open(self.log_file, 'a') as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    json.dump(asdict(entry), f)
                    f.write('\n')
                    f.flush()
                    os.fsync(f.fileno())
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except Exception as e:
            # Don't fail tool execution if logging fails
            logger.warning(f"Could not log tool usage: {e}", exc_info=True)
    
    def get_usage_stats(self, window_hours: int = 24 * 7,  # Default: 7 days
                       tool_name: Optional[str] = None,
                       agent_id: Optional[str] = None) -> Dict:
        """
        Get usage statistics for tools.

        Args:
            window_hours: Time window to analyze (default: 7 days)
            tool_name: Filter by specific tool
            agent_id: Filter by specific agent

        Returns:
            Dict with usage statistics
        """
        if not self.log_file.exists():
            return {
                "total_calls": 0,
                "unique_tools": 0,
                "window_hours": window_hours,
                "tools": {},
                "unused_tools": []
            }

        cutoff_time = datetime.now() - timedelta(hours=window_hours)

        tool_counts = {}
        tool_success_counts = {}
        tool_error_counts = {}
        agent_tool_counts = {}
        total_calls = 0

        try:
            for line in _iter_jsonl_reverse(self.log_file):
                try:
                    entry_dict = json.loads(line)
                    entry_time = _parse_ts_naive(entry_dict['timestamp'])

                    if entry_time < cutoff_time:
                        break

                    tool = entry_dict['tool_name']
                    entry_agent_id = entry_dict.get('agent_id')
                    success = entry_dict.get('success', True)

                    # Filter out removed/deprecated tools
                    if tool in self.REMOVED_TOOLS:
                        continue

                    # Apply filters
                    if tool_name and tool != tool_name:
                        continue
                    if agent_id and entry_agent_id != agent_id:
                        continue

                    # Count usage
                    tool_counts[tool] = tool_counts.get(tool, 0) + 1
                    total_calls += 1

                    if success:
                        tool_success_counts[tool] = tool_success_counts.get(tool, 0) + 1
                    else:
                        tool_error_counts[tool] = tool_error_counts.get(tool, 0) + 1
                        error_type = entry_dict.get('error_type')
                        if error_type:
                            tool_error_counts[f"{tool}:{error_type}"] = tool_error_counts.get(f"{tool}:{error_type}", 0) + 1

                    # Track per-agent usage
                    if entry_agent_id:
                        if entry_agent_id not in agent_tool_counts:
                            agent_tool_counts[entry_agent_id] = {}
                        agent_tool_counts[entry_agent_id][tool] = agent_tool_counts[entry_agent_id].get(tool, 0) + 1
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
        except Exception as e:
            logger.error(f"Error reading tool usage log: {e}")
            return {"error": str(e)}

        # Sort tools by usage
        sorted_tools = sorted(tool_counts.items(), key=lambda x: x[1], reverse=True)

        # Calculate success rates
        tool_stats = {}
        for tool, count in sorted_tools:
            success_count = tool_success_counts.get(tool, 0)
            error_count = tool_error_counts.get(tool, 0)
            success_rate = success_count / count if count > 0 else 0.0
            
            tool_stats[tool] = {
                "total_calls": count,
                "success_count": success_count,
                "error_count": error_count,
                "success_rate": success_rate,
                "percentage_of_total": (count / total_calls * 100) if total_calls > 0 else 0.0
            }
        
        return {
            "total_calls": total_calls,
            "unique_tools": len(tool_counts),
            "window_hours": window_hours,
            "tools": tool_stats,
            "most_used": [{"tool": tool, "calls": count} for tool, count in sorted_tools[:10]],
            "least_used": [{"tool": tool, "calls": count} for tool, count in sorted_tools[-10:]],
            "agent_usage": agent_tool_counts if agent_id else None
        }
    
    def rotate_log(self, max_age_days: int = 7):
        """Archive entries older than ``max_age_days`` and rewrite the live log.

        Mirrors :meth:`AuditLogger.rotate_log`. Returns ``(kept, archive_path)``
        on success, ``(None, None)`` on failure. Designed for periodic
        background-task rotation; safe to run while writers append.
        """
        if not self.log_file.exists():
            return None, None

        cutoff_time = datetime.now() - timedelta(days=max_age_days)
        archive_dir = self.log_file.parent / "tool_usage_archive"
        archive_dir.mkdir(exist_ok=True)
        archived_file = archive_dir / f"tool_usage_{datetime.now().strftime('%Y%m%d')}.jsonl"
        recent_lines: List[str] = []

        try:
            with open(self.log_file, 'r') as f:
                for line in f:
                    try:
                        entry_dict = json.loads(line.strip())
                        entry_time = _parse_ts_naive(entry_dict['timestamp'])
                        if entry_time < cutoff_time:
                            with open(archived_file, 'a') as af:
                                af.write(line)
                        else:
                            recent_lines.append(line)
                    except (json.JSONDecodeError, KeyError, ValueError):
                        continue

            with open(self.log_file, 'w') as f:
                f.writelines(recent_lines)

            return len(recent_lines), archived_file
        except Exception as e:
            logger.warning(f"Could not rotate tool_usage log: {e}", exc_info=True)
            return None, None

    def get_unused_tools(self, all_tools: List[str], window_hours: int = 24 * 30) -> List[str]:
        """
        Identify tools that haven't been used in the time window.
        
        Args:
            all_tools: List of all available tool names
            window_hours: Time window to check (default: 30 days)
        
        Returns:
            List of unused tool names
        """
        stats = self.get_usage_stats(window_hours=window_hours)
        used_tools = set(stats.get("tools", {}).keys())
        unused = [tool for tool in all_tools if tool not in used_tools]
        return unused


# Global instance
_tool_usage_tracker: Optional[ToolUsageTracker] = None


def get_tool_usage_tracker() -> ToolUsageTracker:
    """Get global tool usage tracker instance"""
    global _tool_usage_tracker
    if _tool_usage_tracker is None:
        _tool_usage_tracker = ToolUsageTracker()
    return _tool_usage_tracker

