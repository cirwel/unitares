"""
Pattern Tracker - Detects cognitive loops and unproductive behavior patterns.

Tracks:
- Tool call patterns (repeated similar actions)
- Time-boxing (investigation time limits)
- Hypothesis tracking (code changes → test prompts)
"""

from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
import hashlib
import json
import logging

logger = logging.getLogger(__name__)


@dataclass
class ToolCallPattern:
    """Represents a tool call pattern for loop detection."""
    tool_name: str
    args_hash: str  # Hash of normalized args (ignores timestamps, IDs)
    timestamp: datetime
    agent_id: str
    
    def __post_init__(self):
        if isinstance(self.timestamp, str):
            self.timestamp = datetime.fromisoformat(
                self.timestamp.replace('Z', '+00:00') if 'Z' in self.timestamp else self.timestamp
            )
        if self.timestamp.tzinfo is None:
            self.timestamp = self.timestamp.replace(tzinfo=timezone.utc)


@dataclass
class InvestigationSession:
    """Tracks an investigation session (time-boxing)."""
    agent_id: str
    start_time: datetime
    last_progress_time: datetime
    problem_description: Optional[str] = None
    approach: Optional[str] = None
    tool_calls: int = 0
    
    def __post_init__(self):
        if isinstance(self.start_time, str):
            self.start_time = datetime.fromisoformat(
                self.start_time.replace('Z', '+00:00') if 'Z' in self.start_time else self.start_time
            )
        if self.start_time.tzinfo is None:
            self.start_time = self.start_time.replace(tzinfo=timezone.utc)
        
        if isinstance(self.last_progress_time, str):
            self.last_progress_time = datetime.fromisoformat(
                self.last_progress_time.replace('Z', '+00:00') if 'Z' in self.last_progress_time else self.last_progress_time
            )
        if self.last_progress_time.tzinfo is None:
            self.last_progress_time = self.last_progress_time.replace(tzinfo=timezone.utc)


@dataclass
class Hypothesis:
    """Tracks a hypothesis/test cycle."""
    agent_id: str
    created_time: datetime
    change_type: str  # "code_edit", "config_change", "dependency_add", etc.
    files_changed: List[str]
    hypothesis: Optional[str] = None
    tested: bool = False
    test_time: Optional[datetime] = None
    
    def __post_init__(self):
        if isinstance(self.created_time, str):
            self.created_time = datetime.fromisoformat(
                self.created_time.replace('Z', '+00:00') if 'Z' in self.created_time else self.created_time
            )
        if self.created_time.tzinfo is None:
            self.created_time = self.created_time.replace(tzinfo=timezone.utc)


class PatternTracker:
    """Tracks agent behavior patterns to detect loops and unproductive behavior."""
    
    def __init__(self, window_minutes: int = 30, loop_threshold: int = 3):
        """
        Args:
            window_minutes: Time window for pattern detection
            loop_threshold: Number of similar calls to trigger loop detection
        """
        self.window_minutes = window_minutes
        self.loop_threshold = loop_threshold
        self._max_agents = 500  # Evict stale agents beyond this count

        # Per-agent pattern history (rolling window)
        self.pattern_history: Dict[str, deque] = {}

        # Per-agent investigation sessions
        self.investigations: Dict[str, InvestigationSession] = {}

        # Per-agent hypotheses (untested changes)
        self.hypotheses: Dict[str, List[Hypothesis]] = {}

        # Track last-seen time per agent for eviction
        self._agent_last_seen: Dict[str, 'datetime'] = {}
    
    def normalize_args(self, tool_name: str, args: Dict[str, Any]) -> str:
        """Normalize args for pattern matching (ignore timestamps, IDs, etc.)."""
        normalized = {}
        for key, value in args.items():
            # Skip fields that change every call
            if key in ['timestamp', 'agent_id', 'client_session_id', '_tool_name']:
                continue
            
            # Normalize file paths (keep structure, ignore specific files)
            if key in ['path', 'file_path', 'target_file'] and isinstance(value, str):
                # Keep just the directory structure pattern
                parts = value.split('/')
                if len(parts) > 2:
                    normalized[key] = f"{parts[-2]}/{parts[-1]}"  # Keep last 2 parts
                else:
                    normalized[key] = value
            elif isinstance(value, (str, int, float, bool)):
                normalized[key] = value
            elif isinstance(value, list) and len(value) > 0:
                # For lists, keep type and length
                normalized[key] = f"list[{len(value)}]"
            elif isinstance(value, dict):
                # For dicts, keep keys
                normalized[key] = f"dict[{','.join(sorted(value.keys()))}]"
            else:
                normalized[key] = str(type(value).__name__)
        
        # Create hash
        hash_input = json.dumps({"tool": tool_name, "args": normalized}, sort_keys=True)
        return hashlib.md5(hash_input.encode()).hexdigest()[:16]
    
    def record_tool_call(self, agent_id: str, tool_name: str, args: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Record a tool call and check for loops.
        
        Returns:
            Loop detection result if detected, None otherwise
        """
        now = datetime.now(timezone.utc)
        self._agent_last_seen[agent_id] = now

        # Periodic eviction of stale agents
        if len(self._agent_last_seen) > self._max_agents:
            self._evict_stale_agents(now)

        # Initialize history if needed
        if agent_id not in self.pattern_history:
            self.pattern_history[agent_id] = deque(maxlen=100)
        
        # Normalize and record
        args_hash = self.normalize_args(tool_name, args)
        pattern = ToolCallPattern(
            tool_name=tool_name,
            args_hash=args_hash,
            timestamp=now,
            agent_id=agent_id
        )
        
        # Add to history
        history = self.pattern_history[agent_id]
        history.append(pattern)
        
        # Check for loops (same tool + similar args within window)
        window_start = now - timedelta(minutes=self.window_minutes)
        recent_patterns = [p for p in history if p.timestamp >= window_start]
        
        # Count similar patterns
        similar_count = sum(1 for p in recent_patterns 
                           if p.tool_name == tool_name and p.args_hash == args_hash)
        
        if similar_count >= self.loop_threshold:
            return {
                "detected": True,
                "type": "loop",
                "tool_name": tool_name,
                "count": similar_count,
                "window_minutes": self.window_minutes,
                "message": f"You've called {tool_name} with similar arguments {similar_count} times in the last {self.window_minutes} minutes. Consider trying a different approach."
            }
        
        return None
    
    def start_investigation(self, agent_id: str, problem_description: Optional[str] = None, 
                           approach: Optional[str] = None) -> None:
        """Start tracking an investigation session."""
        now = datetime.now(timezone.utc)
        self.investigations[agent_id] = InvestigationSession(
            agent_id=agent_id,
            start_time=now,
            last_progress_time=now,
            problem_description=problem_description,
            approach=approach
        )
    
    def record_progress(self, agent_id: str) -> None:
        """Record that progress was made (reset time-boxing timer)."""
        if agent_id in self.investigations:
            self.investigations[agent_id].last_progress_time = datetime.now(timezone.utc)
            self.investigations[agent_id].tool_calls += 1
    
    def check_time_box(self, agent_id: str, max_minutes: int = 10) -> Optional[Dict[str, Any]]:
        """
        Check if investigation has exceeded time limit.
        
        Args:
            agent_id: Agent to check
            max_minutes: Maximum minutes without progress
        
        Returns:
            Time-box warning if exceeded, None otherwise
        """
        if agent_id not in self.investigations:
            return None
        
        investigation = self.investigations[agent_id]
        now = datetime.now(timezone.utc)
        
        # Time since last progress
        minutes_since_progress = (now - investigation.last_progress_time).total_seconds() / 60
        
        if minutes_since_progress >= max_minutes:
            total_minutes = (now - investigation.start_time).total_seconds() / 60
            return {
                "detected": True,
                "type": "time_box",
                "minutes_since_progress": round(minutes_since_progress, 1),
                "total_minutes": round(total_minutes, 1),
                "tool_calls": investigation.tool_calls,
                "message": f"You've been investigating for {total_minutes:.1f} minutes without progress. Consider trying a different approach or escalating."
            }
        
        return None
    
    def record_hypothesis(self, agent_id: str, change_type: str, files_changed: List[str], 
                        hypothesis: Optional[str] = None) -> None:
        """Record a hypothesis (code change that needs testing)."""
        if agent_id not in self.hypotheses:
            self.hypotheses[agent_id] = []
        
        hypothesis_obj = Hypothesis(
            agent_id=agent_id,
            created_time=datetime.now(timezone.utc),
            change_type=change_type,
            files_changed=files_changed,
            hypothesis=hypothesis
        )
        
        self.hypotheses[agent_id].append(hypothesis_obj)
    
    def check_untested_hypotheses(self, agent_id: str, max_minutes: int = 5) -> Optional[Dict[str, Any]]:
        """
        Check if there are untested hypotheses older than threshold.
        
        Returns:
            Warning if untested hypotheses exist, None otherwise
        """
        if agent_id not in self.hypotheses:
            return None
        
        now = datetime.now(timezone.utc)
        untested = [h for h in self.hypotheses[agent_id] if not h.tested]
        
        if not untested:
            return None
        
        # Check oldest untested hypothesis
        oldest = min(untested, key=lambda h: h.created_time)
        age_minutes = (now - oldest.created_time).total_seconds() / 60
        
        if age_minutes >= max_minutes:
            return {
                "detected": True,
                "type": "untested_hypothesis",
                "age_minutes": round(age_minutes, 1),
                "change_type": oldest.change_type,
                "files_changed": oldest.files_changed,
                "message": f"You made {oldest.change_type} changes {age_minutes:.1f} minutes ago but haven't tested them. Test your changes before continuing."
            }
        
        return None
    
    def mark_hypothesis_tested(self, agent_id: str, files_changed: List[str]) -> None:
        """Mark hypotheses as tested."""
        if agent_id not in self.hypotheses:
            return
        
        now = datetime.now(timezone.utc)
        for hypothesis in self.hypotheses[agent_id]:
            if not hypothesis.tested and any(f in hypothesis.files_changed for f in files_changed):
                hypothesis.tested = True
                hypothesis.test_time = now
    
    def _evict_stale_agents(self, now: datetime, max_age_minutes: int = 60) -> None:
        """Remove agents not seen in the last max_age_minutes."""
        cutoff = now - timedelta(minutes=max_age_minutes)
        stale = [aid for aid, ts in self._agent_last_seen.items() if ts < cutoff]
        for aid in stale:
            self.pattern_history.pop(aid, None)
            self.investigations.pop(aid, None)
            self.hypotheses.pop(aid, None)
            del self._agent_last_seen[aid]

    def get_patterns(self, agent_id: str) -> Dict[str, Any]:
        """Get all detected patterns for an agent."""
        patterns = []
        
        # Check loop detection (already done in record_tool_call)
        # Check time-boxing
        time_box = self.check_time_box(agent_id)
        if time_box:
            patterns.append(time_box)
        
        # Check untested hypotheses
        hypothesis_warning = self.check_untested_hypotheses(agent_id)
        if hypothesis_warning:
            patterns.append(hypothesis_warning)
        
        return {
            "agent_id": agent_id,
            "patterns": patterns,
            "has_investigation": agent_id in self.investigations,
            "untested_hypotheses": len([h for h in self.hypotheses.get(agent_id, []) if not h.tested])
        }


# Global instance
_pattern_tracker = PatternTracker()


def get_pattern_tracker() -> PatternTracker:
    """Get the global pattern tracker instance."""
    return _pattern_tracker

