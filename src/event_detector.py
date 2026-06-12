"""
Event detection for governance dashboard.

Tracks previous state per agent and detects meaningful transitions:
- Verdict/action changes
- Risk threshold crossings
- Trajectory adjustments
- Drift alerts (with trend awareness: oscillating vs drifting)
- New agents
"""

import logging
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# Risk thresholds that trigger events when crossed
RISK_THRESHOLDS = [0.35, 0.60, 0.70]

# Drift configuration
DRIFT_ALERT_THRESHOLD = 0.1  # Alert when |drift| > this
DRIFT_HISTORY_SIZE = 5       # Number of samples to track for trend
DRIFT_STABLE_THRESHOLD = 0.05  # Below this is considered stable

# Axis names for drift
DRIFT_AXES = ["emotional", "epistemic", "behavioral"]

# Trend classifications
TREND_STABLE = "stable"           # Low magnitude, not moving
TREND_OSCILLATING = "oscillating" # Alternating direction, self-correcting
TREND_DRIFTING_UP = "drifting_up"     # Consistent positive movement
TREND_DRIFTING_DOWN = "drifting_down" # Consistent negative movement


def classify_drift_trend(history: List[float]) -> Tuple[str, float]:
    """
    Classify drift trend from history.

    Returns (trend_type, trend_strength) where:
    - trend_type: stable, oscillating, drifting_up, drifting_down
    - trend_strength: 0-1 indicating confidence in the classification
    """
    if not history or len(history) < 2:
        return TREND_STABLE, 0.0

    # Check if values are all small (stable)
    if all(abs(v) < DRIFT_STABLE_THRESHOLD for v in history):
        return TREND_STABLE, 1.0

    # Calculate deltas between consecutive values
    deltas = [history[i] - history[i-1] for i in range(1, len(history))]

    if not deltas:
        return TREND_STABLE, 0.0

    # Count direction changes (oscillation detection)
    direction_changes = 0
    for i in range(1, len(deltas)):
        if deltas[i] * deltas[i-1] < 0:  # Sign change
            direction_changes += 1

    # Calculate consistency of direction
    positive_deltas = sum(1 for d in deltas if d > 0.01)
    negative_deltas = sum(1 for d in deltas if d < -0.01)
    total_significant = positive_deltas + negative_deltas

    # Oscillation: frequent direction changes relative to samples
    oscillation_ratio = direction_changes / max(1, len(deltas) - 1)

    if oscillation_ratio >= 0.5 and total_significant >= 2:
        # More than half the deltas change direction = oscillating
        strength = min(1.0, oscillation_ratio)
        return TREND_OSCILLATING, strength

    # Check for consistent drift
    if total_significant > 0:
        if positive_deltas >= len(deltas) * 0.6:
            # Mostly positive movement
            strength = positive_deltas / total_significant
            return TREND_DRIFTING_UP, strength
        elif negative_deltas >= len(deltas) * 0.6:
            # Mostly negative movement
            strength = negative_deltas / total_significant
            return TREND_DRIFTING_DOWN, strength

    # Default to stable if no clear pattern
    return TREND_STABLE, 0.5


def predict_drift_crossing(
    history: List[float],
    threshold: float = DRIFT_ALERT_THRESHOLD,
    alpha: float = 0.3,
    forecast_steps: int = 10,
) -> Dict[str, Any]:
    """
    Predict when drift will cross a threshold using EWMA forecasting.

    Args:
        history: Recent drift values for one axis
        threshold: Drift threshold to predict crossing for
        alpha: EWMA smoothing factor (higher = more weight on recent)
        forecast_steps: How many steps ahead to project

    Returns:
        Dict with ewma_current, ewma_slope, predicted_crossing_steps, confidence
    """
    if not history or len(history) < 3:
        return {
            "ewma_current": 0.0,
            "ewma_slope": 0.0,
            "predicted_crossing_steps": None,
            "confidence": 0.0,
        }

    # Compute EWMA series
    ewma = [history[0]]
    for val in history[1:]:
        ewma.append(alpha * val + (1 - alpha) * ewma[-1])

    ewma_current = ewma[-1]

    # Estimate slope from last few EWMA values
    n_slope = min(5, len(ewma))
    if n_slope >= 2:
        recent = ewma[-n_slope:]
        # Simple linear regression slope
        x_mean = (n_slope - 1) / 2.0
        y_mean = sum(recent) / n_slope
        num = sum((i - x_mean) * (recent[i] - y_mean) for i in range(n_slope))
        den = sum((i - x_mean) ** 2 for i in range(n_slope))
        ewma_slope = num / den if den > 1e-10 else 0.0
    else:
        ewma_slope = 0.0

    # Project forward to find crossing
    predicted_crossing_steps = None
    if abs(ewma_slope) > 1e-6:
        # Will absolute EWMA cross the threshold?
        current_abs = abs(ewma_current)
        if current_abs < threshold and ewma_slope != 0:
            # Steps to cross going up (absolute value increasing)
            if ewma_current >= 0 and ewma_slope > 0:
                steps = (threshold - ewma_current) / ewma_slope
            elif ewma_current <= 0 and ewma_slope < 0:
                steps = (-threshold - ewma_current) / ewma_slope
            else:
                steps = None

            if steps is not None and 0 < steps <= forecast_steps:
                predicted_crossing_steps = int(steps) + 1

    # Confidence based on history length and slope consistency
    confidence = min(1.0, len(history) / 10.0)
    if abs(ewma_slope) < 1e-4:
        confidence *= 0.3  # Low confidence when nearly flat

    return {
        "ewma_current": round(ewma_current, 6),
        "ewma_slope": round(ewma_slope, 6),
        "predicted_crossing_steps": predicted_crossing_steps,
        "confidence": round(confidence, 3),
    }


class GovernanceEventDetector:
    """Detects governance events by comparing current state to previous state."""

    def __init__(self, max_stored_events: int = 500):
        # Previous state per agent: {agent_id: {action, risk, drift, ...}}
        self._prev_state: Dict[str, Dict[str, Any]] = {}
        # Recent events for API retrieval (ring buffer)
        self._recent_events: List[Dict[str, Any]] = []
        self._max_stored_events = max_stored_events
        # Monotonically increasing event ID counter
        self._event_counter: int = 0
        # Fingerprint-based dedup for externally recorded findings.
        # Key: fingerprint string. Value: datetime of last emit.
        self._recent_fingerprints: Dict[str, datetime] = {}
        self._dedup_window_seconds: int = 1800  # 30 minutes
        # Change-token dedup for *persisting* conditions (emit-on-change).
        # Key: fingerprint string. Value: last-emitted change token.
        # Unlike the time window above, this suppression does NOT lapse with
        # elapsed time — only when the underlying data changes. This is the
        # correct dedup for a frozen condition: an idle agent's one-time
        # historical risk_spike must not re-fire every sweep once the 30-min
        # window elapses (see project_stale-history-riskspike-refire). Bounded
        # by (#agents x #condition types) ever seen; capped below.
        self._change_tokens: Dict[str, str] = {}
        self._max_change_tokens: int = 5000

    def seed_known_agents(self, agents: list[tuple[str, str]]) -> int:
        """Pre-populate _prev_state so known agents don't fire agent_new after restart.

        Args:
            agents: list of (agent_id, agent_name) tuples from the database.

        Returns:
            Number of agents seeded.
        """
        seeded = 0
        for agent_id, agent_name in agents:
            if agent_id not in self._prev_state:
                self._prev_state[agent_id] = {
                    "action": "proceed",
                    "risk": 0.0,
                    "risk_adjustment": 0.0,
                    "drift": [0, 0, 0],
                    "drift_history": {axis: [] for axis in DRIFT_AXES},
                    "drift_trends": {},
                    "verdict": "proceed",
                    "last_seen": datetime.now(timezone.utc).isoformat(),
                    "agent_name": agent_name,
                }
                seeded += 1
        return seeded

    def detect_events(
        self,
        agent_id: str,
        agent_name: str,
        action: str,
        risk: float,
        risk_raw: float,
        risk_adjustment: float,
        risk_reason: str,
        drift: List[float],
        verdict: str,
    ) -> List[Dict[str, Any]]:
        """
        Compare current state to previous and return list of events.

        Returns list of event dicts with: type, severity, message, details
        """
        events = []
        prev = self._prev_state.get(agent_id)
        now = datetime.now(timezone.utc).isoformat()

        # New agent event
        if prev is None:
            # Only fire event if this isn't the first agent ever (avoid noise on startup)
            if len(self._prev_state) > 0:
                events.append({
                    "type": "agent_new",
                    "severity": "info",
                    "message": f"New agent: {agent_name}",
                    "agent_id": agent_id,
                    "agent_name": agent_name,
                    "timestamp": now
                })
        else:
            # Action/verdict change
            prev_action = prev.get("action")
            if prev_action and prev_action != action:
                severity = "critical" if action in ["pause", "critical", "reject"] else "warning"
                events.append({
                    "type": "verdict_change",
                    "severity": severity,
                    "message": f"{agent_name}: {prev_action.upper()} → {action.upper()}",
                    "agent_id": agent_id,
                    "agent_name": agent_name,
                    "from": prev_action,
                    "to": action,
                    "timestamp": now
                })

            # Risk threshold crossing
            prev_risk = prev.get("risk", 0)
            for threshold in RISK_THRESHOLDS:
                crossed_up = prev_risk < threshold <= risk
                crossed_down = risk < threshold <= prev_risk
                if crossed_up or crossed_down:
                    direction = "up" if crossed_up else "down"
                    severity = "critical" if threshold >= 0.70 else ("warning" if threshold >= 0.60 else "info")
                    pct = int(threshold * 100)
                    events.append({
                        "type": "risk_threshold",
                        "severity": severity,
                        "message": f"{agent_name}: risk {'crossed' if crossed_up else 'dropped below'} {pct}% (now {risk*100:.1f}%)",
                        "agent_id": agent_id,
                        "agent_name": agent_name,
                        "threshold": threshold,
                        "direction": direction,
                        "value": risk,
                        "timestamp": now
                    })

            # Drift alerts - check each axis with trend awareness
            prev_drift = prev.get("drift", [0, 0, 0])
            drift_history = prev.get("drift_history", {axis: [] for axis in DRIFT_AXES})
            drift_trends = {}

            if drift and len(drift) >= 3:
                for i, axis in enumerate(DRIFT_AXES):
                    curr_val = drift[i] if i < len(drift) else 0
                    prev_val = prev_drift[i] if i < len(prev_drift) else 0

                    # Update history for this axis
                    axis_history = drift_history.get(axis, [])
                    axis_history.append(curr_val)
                    if len(axis_history) > DRIFT_HISTORY_SIZE:
                        axis_history = axis_history[-DRIFT_HISTORY_SIZE:]
                    drift_history[axis] = axis_history

                    # Classify trend
                    trend_type, trend_strength = classify_drift_trend(axis_history)
                    drift_trends[axis] = {"trend": trend_type, "strength": trend_strength}

                    # Only alert on sustained drift, not oscillation
                    threshold_crossed = abs(curr_val) > DRIFT_ALERT_THRESHOLD and abs(prev_val) <= DRIFT_ALERT_THRESHOLD
                    is_concerning = trend_type in [TREND_DRIFTING_UP, TREND_DRIFTING_DOWN]

                    if threshold_crossed and is_concerning:
                        sign = "+" if curr_val > 0 else ""
                        direction = "↑" if trend_type == TREND_DRIFTING_UP else "↓"
                        events.append({
                            "type": "drift_alert",
                            "severity": "warning",
                            "message": f"{agent_name}: {axis} drift {sign}{curr_val:.2f} {direction}",
                            "agent_id": agent_id,
                            "agent_name": agent_name,
                            "axis": axis,
                            "value": curr_val,
                            "trend": trend_type,
                            "trend_strength": trend_strength,
                            "timestamp": now
                        })
                    elif threshold_crossed and trend_type == TREND_OSCILLATING:
                        # Log oscillation at info level, not warning - it's self-correcting
                        events.append({
                            "type": "drift_oscillation",
                            "severity": "info",
                            "message": f"{agent_name}: {axis} oscillating ±{abs(curr_val):.2f}",
                            "agent_id": agent_id,
                            "agent_name": agent_name,
                            "axis": axis,
                            "value": curr_val,
                            "trend": trend_type,
                            "timestamp": now
                        })

        # Trajectory adjustment — only on meaningful shifts, not per-tick noise
        if risk_adjustment != 0:
            prev_adj = prev.get("risk_adjustment", 0) if prev else 0
            if abs(risk_adjustment - prev_adj) > 0.05:  # Changed by more than 5%
                sign = "+" if risk_adjustment > 0 else ""
                severity = "warning" if risk_adjustment > 0.1 else "info"
                events.append({
                    "type": "trajectory_adjustment",
                    "severity": severity,
                    "message": f"{agent_name}: {sign}{risk_adjustment*100:.0f}% trajectory adjustment",
                    "agent_id": agent_id,
                    "agent_name": agent_name,
                    "delta": risk_adjustment,
                    "reason": risk_reason,
                    "timestamp": now
                })

        # Initialize drift tracking if not already set (for new agents or first update)
        if 'drift_history' not in locals():
            drift_history = prev.get("drift_history", {axis: [] for axis in DRIFT_AXES}) if prev else {axis: [] for axis in DRIFT_AXES}
        if 'drift_trends' not in locals():
            drift_trends = {}

        # Update stored state
        self._prev_state[agent_id] = {
            "action": action,
            "risk": risk,
            "risk_adjustment": risk_adjustment,
            "drift": drift if drift else [0, 0, 0],
            "drift_history": drift_history,
            "drift_trends": drift_trends,
            "verdict": verdict,
            "last_seen": now,
            "agent_name": agent_name
        }

        # Store events for API retrieval, assigning sequential IDs
        if events:
            for event in events:
                self._event_counter += 1
                event["event_id"] = self._event_counter
            self._recent_events.extend(events)
            # Trim to max size
            if len(self._recent_events) > self._max_stored_events:
                self._recent_events = self._recent_events[-self._max_stored_events:]

        return events

    def record_event(self, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Append an externally-sourced event (e.g. a finding) to the ring buffer.

        Requires a caller-supplied ``fingerprint`` string. Duplicate fingerprints
        seen inside ``_dedup_window_seconds`` are dropped (returns None) so
        periodic re-emitters like Sentinel do not flood Discord.

        If the caller supplies a ``change_token``, dedup switches to
        emit-on-change: the event is dropped whenever the token equals the last
        token emitted for this fingerprint, with NO time component. This is the
        correct behavior for persisting/frozen conditions — e.g. an idle agent
        whose one-time historical risk_spike would otherwise re-fire every sweep
        once the 30-minute window lapses. A token-bearing event emits exactly
        once per distinct underlying condition, however many sweeps observe it.

        Stamps ``event_id`` and ``timestamp`` in-place and returns the stored dict.
        """
        fingerprint = event.get("fingerprint")
        if not fingerprint or not isinstance(fingerprint, str):
            return None

        now = datetime.now(timezone.utc)
        change_token = event.get("change_token")
        if change_token is not None:
            # Emit-on-change path: time-independent suppression of an unchanged
            # persisting condition. Keyed purely on whether the data moved.
            if self._change_tokens.get(fingerprint) == change_token:
                return None
            self._change_tokens[fingerprint] = change_token
            if len(self._change_tokens) > self._max_change_tokens:
                # Drop ~10% oldest by insertion order to bound memory.
                drop = max(1, self._max_change_tokens // 10)
                for stale_fp in list(self._change_tokens)[:drop]:
                    del self._change_tokens[stale_fp]
        else:
            last_seen = self._recent_fingerprints.get(fingerprint)
            if last_seen is not None:
                age_seconds = (now - last_seen).total_seconds()
                if age_seconds < self._dedup_window_seconds:
                    return None

            # Sweep fingerprints older than 2x window so the dict does not grow forever
            cutoff = now - timedelta(seconds=2 * self._dedup_window_seconds)
            self._recent_fingerprints = {
                fp: ts for fp, ts in self._recent_fingerprints.items() if ts > cutoff
            }
            self._recent_fingerprints[fingerprint] = now

        if "timestamp" not in event:
            event["timestamp"] = now.isoformat()

        self._event_counter += 1
        event["event_id"] = self._event_counter
        self._recent_events.append(event)
        if len(self._recent_events) > self._max_stored_events:
            self._recent_events = self._recent_events[-self._max_stored_events:]

        return event

    def check_idle_agents(self, idle_threshold_minutes: float = 5.0) -> List[Dict[str, Any]]:
        """
        Check for agents that haven't checked in recently.
        Call this periodically (e.g., every minute).

        Returns list of idle agent events.
        """
        events = []
        now = datetime.now(timezone.utc)

        for agent_id, state in list(self._prev_state.items()):
            last_seen_str = state.get("last_seen")
            if not last_seen_str:
                continue

            try:
                last_seen = datetime.fromisoformat(last_seen_str.replace('Z', '+00:00'))
                idle_minutes = (now - last_seen).total_seconds() / 60

                # Check if newly idle (crossed threshold)
                was_idle = state.get("_idle_alerted", False)
                is_idle = idle_minutes >= idle_threshold_minutes

                if is_idle and not was_idle:
                    agent_name = state.get("agent_name", agent_id[:8])
                    events.append({
                        "type": "agent_idle",
                        "severity": "warning",
                        "message": f"{agent_name} idle ({int(idle_minutes)}m)",
                        "agent_id": agent_id,
                        "agent_name": agent_name,
                        "duration_minutes": idle_minutes,
                        "timestamp": now.isoformat()
                    })
                    state["_idle_alerted"] = True
                elif not is_idle and was_idle:
                    # Agent came back - clear the flag
                    state["_idle_alerted"] = False

            except Exception as e:
                logger.debug(f"Error checking idle for {agent_id}: {e}")

        return events

    def get_recent_events_for_agent(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """Get the last known state for an agent."""
        return self._prev_state.get(agent_id)

    def get_drift_trends(self, agent_id: str) -> Dict[str, Dict[str, Any]]:
        """
        Get current drift trends for an agent.

        Returns dict of {axis: {trend, strength, current_value}} for each axis.
        """
        state = self._prev_state.get(agent_id)
        if not state:
            return {}

        drift = state.get("drift", [0, 0, 0])
        drift_trends = state.get("drift_trends", {})

        result = {}
        for i, axis in enumerate(DRIFT_AXES):
            curr_val = drift[i] if i < len(drift) else 0
            trend_info = drift_trends.get(axis, {"trend": TREND_STABLE, "strength": 0.0})
            result[axis] = {
                "trend": trend_info.get("trend", TREND_STABLE),
                "strength": trend_info.get("strength", 0.0),
                "value": curr_val
            }

        return result

    def get_recent_events(
        self,
        limit: int = 50,
        agent_id: Optional[str] = None,
        event_type: Optional[str] = None,
        since: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Get recent events, optionally filtered.

        Args:
            limit: Maximum number of events to return
            agent_id: Filter to specific agent
            event_type: Filter to specific event type
            since: Only return events with event_id > since (cursor for resumption)

        Returns:
            List of events, newest first
        """
        events = self._recent_events.copy()

        # Apply filters
        if since is not None:
            events = [e for e in events if e.get("event_id", 0) > since]
        if agent_id:
            events = [e for e in events if e.get("agent_id") == agent_id]
        if event_type:
            events = [e for e in events if e.get("type") == event_type]

        # Return newest first, limited
        return list(reversed(events))[:limit]

    def clear_events(self):
        """Clear stored events (for testing)."""
        self._recent_events.clear()


# Singleton instance
event_detector = GovernanceEventDetector()
