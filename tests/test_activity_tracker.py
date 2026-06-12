"""
Test activity tracker for mixed autonomy patterns
"""

import pytest
from src.activity_tracker import AgentActivity, HeartbeatConfig, ActivityTracker
from datetime import datetime, timedelta


def test_basic_activity_tracking():
    """Test basic activity counter increments"""
    activity = AgentActivity(agent_id="test_agent")

    assert activity.tool_calls == 0
    assert activity.conversation_turns == 0
    assert activity.cumulative_complexity == 0.0


def test_conversation_turn_threshold():
    """Test conversation turn triggers"""
    config = HeartbeatConfig(conversation_turn_threshold=3)
    activity = AgentActivity(agent_id="test_agent")

    # Below threshold
    activity.conversation_turns = 2
    should_trigger, reason = activity.should_trigger_update(config)
    assert not should_trigger

    # At threshold
    activity.conversation_turns = 3
    should_trigger, reason = activity.should_trigger_update(config)
    assert should_trigger
    assert reason == "conversation_turn_threshold"


def test_tool_call_threshold():
    """Test tool call triggers (for autonomous agents)"""
    config = HeartbeatConfig(tool_call_threshold=10)
    activity = AgentActivity(agent_id="test_agent")

    # Below threshold
    activity.tool_calls = 9
    should_trigger, reason = activity.should_trigger_update(config)
    assert not should_trigger

    # At threshold
    activity.tool_calls = 10
    should_trigger, reason = activity.should_trigger_update(config)
    assert should_trigger
    assert reason == "tool_call_threshold"


def test_time_threshold():
    """Test time-based trigger (safety net)"""
    config = HeartbeatConfig(time_threshold_minutes=15)
    activity = AgentActivity(agent_id="test_agent")

    # Set last update to 20 minutes ago
    past = datetime.now() - timedelta(minutes=20)
    activity.last_governance_update = past.isoformat()

    should_trigger, reason = activity.should_trigger_update(config)
    assert should_trigger
    assert reason == "time_threshold"


def test_complexity_threshold():
    """Test cumulative complexity trigger"""
    config = HeartbeatConfig(complexity_threshold=3.0)
    activity = AgentActivity(agent_id="test_agent")

    # Below threshold
    activity.add_complexity_sample(0.5)
    activity.add_complexity_sample(0.8)
    activity.add_complexity_sample(1.0)
    assert activity.cumulative_complexity == 2.3
    should_trigger, reason = activity.should_trigger_update(config)
    assert not should_trigger

    # At threshold
    activity.add_complexity_sample(0.7)
    assert activity.cumulative_complexity >= 3.0
    should_trigger, reason = activity.should_trigger_update(config)
    assert should_trigger
    assert reason == "complexity_threshold"


def test_file_modification_threshold():
    """Test high-impact action trigger"""
    config = HeartbeatConfig(file_modification_threshold=3)
    activity = AgentActivity(agent_id="test_agent")

    activity.files_modified = 3
    should_trigger, reason = activity.should_trigger_update(config)
    assert should_trigger
    assert reason == "high_impact_actions"


def test_activity_reset():
    """Test activity reset after governance update"""
    activity = AgentActivity(agent_id="test_agent")

    # Build up activity
    activity.conversation_turns = 5
    activity.tool_calls = 10
    activity.cumulative_complexity = 2.5
    activity.files_modified = 2

    # Reset
    activity.reset_after_update()

    # Check all counters reset
    assert activity.conversation_turns == 0
    assert activity.tool_calls == 0
    assert activity.cumulative_complexity == 0.0
    assert activity.files_modified == 0
    assert activity.last_governance_update is not None


def test_turn_inference():
    """Test conversation turn inference from timing"""
    activity = AgentActivity(agent_id="test_agent")

    # First activity = first turn
    assert activity.infer_conversation_turn()

    # Recent activity = same turn
    activity.last_activity = datetime.now().isoformat()
    assert not activity.infer_conversation_turn(turn_gap_seconds=30)

    # Old activity = new turn
    past = datetime.now() - timedelta(seconds=35)
    activity.last_activity = past.isoformat()
    assert activity.infer_conversation_turn(turn_gap_seconds=30)


def test_activity_tracker_integration(tmp_path):
    """Test full ActivityTracker workflow"""
    config = HeartbeatConfig(
        conversation_turn_threshold=3,
        tool_call_threshold=5,
        turn_gap_seconds=30
    )
    # Use temp directory for test isolation
    tracker = ActivityTracker(config=config, data_dir=tmp_path)

    agent_id = "integration_test_agent"

    # Simulate user-prompted agent (low autonomy)
    # User prompt 1
    should_trigger, reason = tracker.track_tool_call(agent_id, "read")
    assert not should_trigger  # Turn 1, tool 1

    # User prompt 2 (after gap)
    should_trigger, reason = tracker.track_tool_call(agent_id, "grep")
    assert not should_trigger  # Turn 2, tool 2

    # User prompt 3 (after gap)
    should_trigger, reason = tracker.track_tool_call(agent_id, "write")
    assert not should_trigger  # Turn 3, tool 3

    # Inspect tracked state
    activity = tracker.get_or_create(agent_id)
    assert activity.tool_calls == 3
    assert activity.files_modified == 1  # write is high-impact

    # Reset
    tracker.reset_after_governance_update(agent_id)
    assert tracker.get_or_create(agent_id).tool_calls == 0


def test_prompted_vs_autonomous_patterns(tmp_path):
    """Test different agent autonomy patterns"""
    config = HeartbeatConfig(
        conversation_turn_threshold=5,
        tool_call_threshold=10
    )
    # Use temp directory for test isolation
    tracker = ActivityTracker(config=config, data_dir=tmp_path)

    # Pattern 1: Prompted agent (low autonomy, high turn/tool ratio)
    prompted_id = "prompted_agent"
    for _ in range(5):
        tracker.get_or_create(prompted_id).conversation_turns += 1
        tracker.track_tool_call(prompted_id, "read")

    prompted = tracker.get_or_create(prompted_id)
    assert prompted.conversation_turns == 5
    assert prompted.tool_calls == 5
    # Should trigger on turns
    should_trigger, reason = tracker.get_or_create(prompted_id).should_trigger_update(config)
    assert should_trigger
    assert reason == "conversation_turn_threshold"

    # Pattern 2: Autonomous agent (high autonomy, low turn/tool ratio)
    autonomous_id = "autonomous_agent"
    tracker.get_or_create(autonomous_id).conversation_turns += 1  # One user request
    for i in range(15):
        tracker.track_tool_call(autonomous_id, "analyze" if i < 10 else "write")

    autonomous = tracker.get_or_create(autonomous_id)
    assert autonomous.conversation_turns == 1
    assert autonomous.tool_calls == 15
    # Should trigger on tools
    should_trigger, reason = tracker.get_or_create(autonomous_id).should_trigger_update(config)
    assert should_trigger
    assert reason == "tool_call_threshold"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
