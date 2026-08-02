"""Pure rule evaluation for agent update-loop detection.

The runtime adapter in :mod:`src.agent_loop_detection` owns metadata lookup,
cooldowns, and grace-period construction.  This module only evaluates an
immutable observation window, which keeps each detector independently testable
and makes rule ordering explicit.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence


PROCEED_LOOP_FRESHNESS_SECONDS = 600
PAUSE_LOOP_FRESHNESS_SECONDS = 3600

_PAUSE_DECISIONS = frozenset({"pause", "reject"})
_PROCEED_DECISIONS = frozenset({"proceed", "approve", "reflect", "revise"})


@dataclass(frozen=True)
class LoopWindow:
    """Normalized inputs shared by the ordered loop rules."""

    timestamps: Sequence[str]
    decisions: Sequence[str]
    rapid_timestamps: Sequence[str]
    now: datetime
    skip_rapid: bool
    in_recovery_grace: bool
    is_autonomous: bool


def _parsed(values: Sequence[str]) -> list[datetime]:
    return [datetime.fromisoformat(value) for value in values]


def _rapid_pair_reason(window: LoopWindow) -> str | None:
    if len(window.rapid_timestamps) >= 2:
        try:
            timestamps = _parsed(window.rapid_timestamps)
            rapid_pairs = []
            for index in range(len(timestamps) - 1):
                time_diff = (timestamps[index + 1] - timestamps[index]).total_seconds()
                if time_diff < 0.3:
                    rapid_pairs.append((index, index + 1, time_diff))
            if rapid_pairs:
                fastest_pair = min(rapid_pairs, key=lambda pair: pair[2])
                return (
                    "Rapid-fire updates detected "
                    f"({len(rapid_pairs)} pair(s) within 0.3s, fastest: "
                    f"{fastest_pair[2] * 1000:.1f}ms apart)"
                )
        except (ValueError, TypeError):
            pass
    return None


def _rapid_triple_reason(window: LoopWindow) -> str | None:
    if len(window.rapid_timestamps) >= 3:
        try:
            timestamps = _parsed(window.rapid_timestamps)
            for index in range(len(timestamps) - 2):
                if (timestamps[index + 2] - timestamps[index]).total_seconds() < 0.5:
                    return (
                        "Rapid-fire updates detected "
                        f"(3+ updates within 0.5 seconds, detected at positions "
                        f"{index}-{index + 2})"
                    )
        except (ValueError, TypeError):
            pass
    return None


def _rapid_quad_reason(window: LoopWindow) -> str | None:
    if len(window.rapid_timestamps) >= 4:
        try:
            timestamps = _parsed(window.rapid_timestamps)
            for index in range(len(timestamps) - 3):
                if (timestamps[index + 3] - timestamps[index]).total_seconds() < 1.0:
                    return (
                        "Rapid-fire updates detected "
                        f"(4+ updates within 1 second, detected at positions "
                        f"{index}-{index + 3})"
                    )
        except (ValueError, TypeError):
            pass
    return None


def _rapid_fire_reason(window: LoopWindow) -> str | None:
    """Evaluate the three rapid-fire variants, preserving legacy order."""
    if window.skip_rapid:
        return None
    for rule in (_rapid_pair_reason, _rapid_triple_reason, _rapid_quad_reason):
        if reason := rule(window):
            return reason
    return None


def _pause_burst_reason(window: LoopWindow) -> str | None:
    if window.in_recovery_grace or len(window.timestamps) < 3:
        return None
    try:
        timestamps = _parsed(window.timestamps[-3:])
        time_span = (timestamps[-1] - timestamps[0]).total_seconds()
        if time_span <= 10.0:
            pause_count = sum(
                1 for decision in window.decisions[-3:] if decision in _PAUSE_DECISIONS
            )
            if pause_count >= 2:
                return (
                    f"Recursive pause pattern: {pause_count} pause decisions "
                    f"within {time_span:.1f}s"
                )
    except (ValueError, TypeError):
        pass
    return None


def _concerning_burst_reason(window: LoopWindow) -> str | None:
    if len(window.timestamps) < 4:
        return None
    try:
        timestamps = _parsed(window.timestamps[-4:])
        time_span = (timestamps[-1] - timestamps[0]).total_seconds()
        if time_span <= 5.0:
            concerning_count = sum(
                1 for decision in window.decisions[-4:] if decision in _PAUSE_DECISIONS
            )
            if concerning_count >= 1:
                return (
                    f"Rapid update pattern: 4+ updates within {time_span:.1f}s "
                    f"with {concerning_count} pause/reject decision(s)"
                )
    except (ValueError, TypeError):
        pass
    return None


def _decision_loop_reason(window: LoopWindow) -> str | None:
    if window.is_autonomous or len(window.decisions) < 5:
        return None

    decision_window = (
        window.decisions[-10:] if len(window.decisions) >= 10 else window.decisions
    )
    decision_counts = Counter(decision_window)
    pause_count = sum(decision_counts.get(value, 0) for value in _PAUSE_DECISIONS)
    if pause_count >= 5:
        try:
            window_span = min(len(decision_window), len(window.timestamps))
            if window_span > 0:
                newest_pause_ts = datetime.fromisoformat(window.timestamps[-1])
                newest_age = (window.now - newest_pause_ts).total_seconds()
                if newest_age <= PAUSE_LOOP_FRESHNESS_SECONDS:
                    return (
                        f"Decision loop detected: {pause_count} 'pause' decisions "
                        "in recent history (stuck state)"
                    )
        except (ValueError, TypeError, IndexError):
            return (
                f"Decision loop detected: {pause_count} 'pause' decisions "
                "in recent history (stuck state)"
            )

    proceed_count = sum(decision_counts.get(value, 0) for value in _PROCEED_DECISIONS)
    if proceed_count >= 10:
        try:
            timestamps = _parsed(window.timestamps[-10:])
            time_span = (timestamps[-1] - timestamps[0]).total_seconds()
            newest_age = (window.now - timestamps[-1]).total_seconds()
            if time_span <= 300 and newest_age <= PROCEED_LOOP_FRESHNESS_SECONDS:
                return (
                    f"Decision loop detected: {proceed_count} 'proceed' decisions "
                    f"in {time_span:.0f}s (agent may be stuck in feedback loop)"
                )
        except (ValueError, TypeError, IndexError):
            pass
    return None


def _slow_stuck_reason(window: LoopWindow) -> str | None:
    if window.is_autonomous or window.in_recovery_grace or len(window.timestamps) < 3:
        return None
    try:
        timestamps = _parsed(window.timestamps[-3:])
        time_span = (timestamps[-1] - timestamps[0]).total_seconds()
        if time_span <= 60.0:
            pause_count = sum(
                1 for decision in window.decisions[-3:] if decision in _PAUSE_DECISIONS
            )
            if pause_count >= 2:
                return (
                    f"Slow-stuck pattern: {pause_count} pause(s) in 3 updates "
                    f"within {time_span:.1f}s"
                )
    except (ValueError, TypeError):
        pass
    return None


def _extended_rapid_reason(window: LoopWindow) -> str | None:
    if window.is_autonomous or window.in_recovery_grace or len(window.timestamps) < 5:
        return None
    try:
        timestamps = _parsed(window.timestamps[-5:])
        time_span = (timestamps[-1] - timestamps[0]).total_seconds()
        if time_span <= 120.0:
            concerning_count = sum(
                1 for decision in window.decisions[-5:] if decision in _PAUSE_DECISIONS
            )
            if concerning_count >= 3:
                return (
                    f"Extended rapid pattern: 5 updates within {time_span:.1f}s "
                    f"with {concerning_count} pause/reject decision(s)"
                )
    except (ValueError, TypeError):
        pass
    return None


def _slow_proceed_reason(window: LoopWindow) -> str | None:
    if window.is_autonomous or window.in_recovery_grace or len(window.timestamps) < 8:
        return None
    try:
        timestamps = _parsed(window.timestamps[-10:])
        time_span = (timestamps[-1] - timestamps[0]).total_seconds()
        newest_age = (window.now - timestamps[-1]).total_seconds()
        if time_span <= 300.0 and newest_age <= PROCEED_LOOP_FRESHNESS_SECONDS:
            proceed_count = sum(
                1
                for decision in window.decisions[-10:]
                if decision in _PROCEED_DECISIONS
            )
            if proceed_count >= 8:
                return (
                    f"Slow proceed loop: {proceed_count} proceed decisions within "
                    f"{time_span:.1f}s (agent may be repeating without progress)"
                )
    except (ValueError, TypeError):
        pass
    return None


_ORDERED_RULES = (
    _rapid_fire_reason,
    _pause_burst_reason,
    _concerning_burst_reason,
    _decision_loop_reason,
    _slow_stuck_reason,
    _extended_rapid_reason,
    _slow_proceed_reason,
)


def evaluate_loop_rules(window: LoopWindow) -> str | None:
    """Return the first matching rule reason, preserving runtime precedence."""
    for rule in _ORDERED_RULES:
        if reason := rule(window):
            return reason
    return None
