"""
Activity Tracker for Mixed Autonomy Patterns

Tracks agent activity between governance updates to trigger heartbeats.
Handles both user-prompted agents (low autonomy) and autonomous agents.
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Dict, Tuple, Optional, List
from pathlib import Path
import json


@dataclass
class AgentActivity:
    """Track agent activity between governance updates"""
    agent_id: str

    # Counters (reset after governance update)
    conversation_turns: int = 0          # User prompts + agent responses
    tool_calls: int = 0                  # MCP tool invocations
    tokens_generated: int = 0            # Approximate cognitive effort
    files_modified: int = 0              # High-impact actions

    # Timestamps
    last_governance_update: Optional[str] = None
    last_activity: Optional[str] = None
    session_start: Optional[str] = None

    # Complexity tracking
    cumulative_complexity: float = 0.0   # Sum of complexity estimates
    complexity_samples: List[float] = field(default_factory=list)

    # Tool call history (for turn inference)
    recent_tool_timestamps: List[str] = field(default_factory=list)

    def should_trigger_update(self, config: 'HeartbeatConfig') -> Tuple[bool, Optional[str]]:
        """
        Determine if governance heartbeat should trigger.

        Returns:
            (should_trigger: bool, reason: str or None)
        """

        # Rule 1: Every N conversation turns (for prompted agents)
        if config.track_conversation_turns and self.conversation_turns >= config.conversation_turn_threshold:
            return True, "conversation_turn_threshold"

        # Rule 2: Every N tool calls (for autonomous agents)
        if config.track_tool_calls and self.tool_calls >= config.tool_call_threshold:
            return True, "tool_call_threshold"

        # Rule 3: Every N minutes (time-based safety net)
        if self.last_governance_update:
            try:
                last_update = datetime.fromisoformat(self.last_governance_update)
                elapsed_minutes = (datetime.now() - last_update).total_seconds() / 60
                if elapsed_minutes >= config.time_threshold_minutes:
                    return True, "time_threshold"
            except (ValueError, TypeError):
                pass

        # Rule 4: High cumulative complexity (cognitive load)
        if config.track_complexity and self.cumulative_complexity >= config.complexity_threshold:
            return True, "complexity_threshold"

        # Rule 5: File modifications (high impact actions)
        if self.files_modified >= config.file_modification_threshold:
            return True, "high_impact_actions"

        return False, None

    def reset_after_update(self):
        """Reset activity counters after governance update"""
        self.conversation_turns = 0
        self.tool_calls = 0
        self.tokens_generated = 0
        self.files_modified = 0
        self.cumulative_complexity = 0.0
        self.complexity_samples = []
        self.last_governance_update = datetime.now().isoformat()

    def infer_conversation_turn(self, turn_gap_seconds: float = 30.0) -> bool:
        """
        Infer if a new conversation turn has started.

        Heuristic: If gap since last activity > threshold, likely new user prompt.

        Args:
            turn_gap_seconds: Minimum gap to consider a new turn

        Returns:
            True if new turn detected
        """
        if not self.last_activity:
            return True  # First activity = first turn

        try:
            last = datetime.fromisoformat(self.last_activity)
            gap = (datetime.now() - last).total_seconds()
            return gap >= turn_gap_seconds
        except (ValueError, TypeError):
            return False

    def add_complexity_sample(self, complexity: float):
        """Add complexity sample and update cumulative"""
        self.cumulative_complexity += complexity
        self.complexity_samples.append(complexity)

    def to_dict(self) -> Dict:
        """Convert to dictionary for JSON serialization"""
        data = asdict(self)
        # Convert lists to avoid mutation issues
        data['complexity_samples'] = list(self.complexity_samples)
        data['recent_tool_timestamps'] = list(self.recent_tool_timestamps)
        return data

    @classmethod
    def from_dict(cls, data: Dict) -> 'AgentActivity':
        """Load from dictionary"""
        return cls(**data)


@dataclass
class HeartbeatConfig:
    """Configuration for automatic governance heartbeats"""

    # Trigger thresholds
    conversation_turn_threshold: int = 5       # Every 5 user-agent exchanges
    tool_call_threshold: int = 10             # Every 10 MCP tool calls
    time_threshold_minutes: int = 15          # Every 15 minutes
    complexity_threshold: float = 3.0         # Cumulative complexity
    file_modification_threshold: int = 3      # File writes/edits

    # Heartbeat behavior
    enabled: bool = True                      # Master switch
    track_conversation_turns: bool = True     # Infer from tool timing
    track_tool_calls: bool = True            # Count MCP calls
    track_complexity: bool = True            # Sum complexity estimates

    # Turn inference
    turn_gap_seconds: float = 30.0           # Gap to consider new turn

    # High-impact tools (always track modifications)
    high_impact_tools: List[str] = field(default_factory=lambda: [
        'write', 'edit', 'bash',
        'export_to_file', 'request_dialectic_review'
    ])


class ActivityTracker:
    """Manages activity tracking for all agents"""

    def __init__(self, config: Optional[HeartbeatConfig] = None, data_dir: Optional[Path] = None):
        self.config = config or HeartbeatConfig()
        self.data_dir = data_dir or Path(__file__).parent.parent / "data" / "activity"
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # In-memory activity tracking
        self.activities: Dict[str, AgentActivity] = {}

    def get_or_create(self, agent_id: str) -> AgentActivity:
        """Get or create activity tracker for agent"""
        if agent_id not in self.activities:
            # Try to load from disk
            loaded = self._load_activity(agent_id)
            if loaded:
                self.activities[agent_id] = loaded
            else:
                self.activities[agent_id] = AgentActivity(
                    agent_id=agent_id,
                    session_start=datetime.now().isoformat()
                )
        return self.activities[agent_id]

    def track_tool_call(self, agent_id: str, tool_name: str) -> Tuple[bool, Optional[str]]:
        """
        Track a tool call and check if governance should trigger.

        Returns:
            (should_trigger: bool, reason: str or None)
        """
        activity = self.get_or_create(agent_id)

        # Increment tool counter
        activity.tool_calls += 1
        activity.last_activity = datetime.now().isoformat()
        activity.recent_tool_timestamps.append(activity.last_activity)

        # Keep only recent timestamps (for turn inference)
        if len(activity.recent_tool_timestamps) > 20:
            activity.recent_tool_timestamps = activity.recent_tool_timestamps[-20:]

        # Infer conversation turn
        if self.config.track_conversation_turns:
            if activity.infer_conversation_turn(self.config.turn_gap_seconds):
                activity.conversation_turns += 1

        # Track file modifications for high-impact tools
        if tool_name in self.config.high_impact_tools:
            activity.files_modified += 1

        # Check if should trigger
        should_trigger, reason = activity.should_trigger_update(self.config)

        # Persist activity
        self._save_activity(activity)

        return should_trigger, reason

    def track_complexity(self, agent_id: str, complexity: float) -> Tuple[bool, Optional[str]]:
        """
        Track complexity and check if should trigger.

        Returns:
            (should_trigger: bool, reason: str or None)
        """
        activity = self.get_or_create(agent_id)
        activity.add_complexity_sample(complexity)
        activity.last_activity = datetime.now().isoformat()

        should_trigger, reason = activity.should_trigger_update(self.config)
        self._save_activity(activity)

        return should_trigger, reason

    def reset_after_governance_update(self, agent_id: str):
        """Reset activity counters after governance update"""
        activity = self.get_or_create(agent_id)
        activity.reset_after_update()
        self._save_activity(activity)

    def _load_activity(self, agent_id: str) -> Optional[AgentActivity]:
        """Load activity from disk"""
        activity_file = self.data_dir / f"{agent_id}_activity.json"
        if activity_file.exists():
            try:
                data = json.load(open(activity_file))
                return AgentActivity.from_dict(data)
            except (json.JSONDecodeError, TypeError, KeyError):
                return None
        return None

    def _save_activity(self, activity: AgentActivity):
        """Save activity to disk"""
        activity_file = self.data_dir / f"{activity.agent_id}_activity.json"
        try:
            with open(activity_file, 'w') as f:
                json.dump(activity.to_dict(), f, indent=2)
        except Exception:
            pass  # Silent fail for activity tracking


# Global instance (can be configured)
_default_tracker: Optional[ActivityTracker] = None


def get_activity_tracker(config: Optional[HeartbeatConfig] = None) -> ActivityTracker:
    """Get or create default activity tracker"""
    global _default_tracker
    if _default_tracker is None:
        _default_tracker = ActivityTracker(config=config)
    return _default_tracker
