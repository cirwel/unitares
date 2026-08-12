"""
CIRS Protocol in-memory message storage.

Houses all buffer deques/dicts and CRUD helpers for CIRS messages.
"""

from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta, timezone
from collections import deque

from src.logging_utils import get_logger
from .types import (
    VoidAlert, VoidSeverity, StateAnnounce,
    ResonanceAlert, StabilityRestored,
    CoherenceReport, BoundaryContract,
    GovernanceAction,
)

logger = get_logger(__name__)

# =============================================================================
# Void Alert Storage
# =============================================================================

_void_alert_buffer: deque = deque(maxlen=1000)
ALERT_TTL_HOURS = 24


def _cleanup_old_alerts():
    """Remove alerts older than TTL"""
    cutoff = datetime.now() - timedelta(hours=ALERT_TTL_HOURS)
    cutoff_iso = cutoff.isoformat()
    while _void_alert_buffer and _void_alert_buffer[0]["timestamp"] < cutoff_iso:
        _void_alert_buffer.popleft()


def _store_void_alert(alert: VoidAlert):
    """Store a void alert in the buffer"""
    _cleanup_old_alerts()
    _void_alert_buffer.append(alert.to_dict())
    logger.info(f"[CIRS/VOID_ALERT] {alert.severity.value.upper()}: agent={alert.agent_id}, V={alert.V_snapshot:.4f}")


def _get_recent_void_alerts(
    agent_id: Optional[str] = None,
    severity: Optional[VoidSeverity] = None,
    since_hours: float = 1.0,
    limit: int = 50
) -> List[Dict[str, Any]]:
    """Query recent void alerts."""
    _cleanup_old_alerts()
    cutoff = datetime.now() - timedelta(hours=since_hours)
    cutoff_iso = cutoff.isoformat()

    results = []
    for alert in reversed(_void_alert_buffer):
        if alert["timestamp"] < cutoff_iso:
            continue
        if agent_id and alert["agent_id"] != agent_id:
            continue
        if severity and alert["severity"] != severity.value:
            continue
        results.append(alert)
        if len(results) >= limit:
            break
    return results


# =============================================================================
# State Announce Storage
# =============================================================================

_state_announce_buffer: Dict[str, Dict[str, Any]] = {}
STATE_ANNOUNCE_TTL_HOURS = 1


def _cleanup_old_state_announces():
    """Remove state announcements older than TTL"""
    cutoff = datetime.now() - timedelta(hours=STATE_ANNOUNCE_TTL_HOURS)
    cutoff_iso = cutoff.isoformat()
    stale_agents = [
        agent_id for agent_id, announce in _state_announce_buffer.items()
        if announce["timestamp"] < cutoff_iso
    ]
    for agent_id in stale_agents:
        del _state_announce_buffer[agent_id]


def _store_state_announce(announce: StateAnnounce):
    """Store a state announcement (overwrites previous for same agent)"""
    _cleanup_old_state_announces()
    _state_announce_buffer[announce.agent_id] = announce.to_dict()
    logger.debug(f"[CIRS/STATE_ANNOUNCE] agent={announce.agent_id}, regime={announce.regime}, phi={announce.phi:.3f}")


def _get_state_announces(
    agent_ids: Optional[List[str]] = None,
    regime: Optional[str] = None,
    max_risk: Optional[float] = None,
    limit: int = 50
) -> List[Dict[str, Any]]:
    """Query recent state announcements."""
    _cleanup_old_state_announces()

    results = []
    for agent_id, announce in _state_announce_buffer.items():
        if agent_ids and agent_id not in agent_ids:
            continue
        if regime and announce.get("regime") != regime:
            continue
        if max_risk is not None and announce.get("risk_score", 1.0) > max_risk:
            continue
        results.append(announce)

    results.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return results[:limit]


# =============================================================================
# Resonance Alert / Stability Restored Storage
# =============================================================================

_resonance_alert_buffer: deque = deque(maxlen=100)


def _emit_resonance_alert(alert: ResonanceAlert):
    """Store a resonance alert."""
    _resonance_alert_buffer.append(alert.to_dict())


def _emit_stability_restored(restored: StabilityRestored):
    """Store a stability restored signal."""
    _resonance_alert_buffer.append(restored.to_dict())


def _get_recent_resonance_signals(
    max_age_minutes: int = 30,
    agent_id: Optional[str] = None,
    signal_type: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Get recent resonance-related signals."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)).isoformat()
    results = []
    for signal in reversed(_resonance_alert_buffer):
        if signal["timestamp"] < cutoff:
            break
        if agent_id and signal["agent_id"] != agent_id:
            continue
        if signal_type and signal["type"] != signal_type:
            continue
        results.append(signal)
    return results


# =============================================================================
# Coherence Report Storage
# =============================================================================

_coherence_report_buffer: Dict[str, Dict[str, Any]] = {}
COHERENCE_REPORT_TTL_HOURS = 1


def _cleanup_old_coherence_reports():
    """Remove coherence reports older than TTL"""
    cutoff = datetime.now() - timedelta(hours=COHERENCE_REPORT_TTL_HOURS)
    cutoff_iso = cutoff.isoformat()
    stale_keys = [
        key for key, report in _coherence_report_buffer.items()
        if report["timestamp"] < cutoff_iso
    ]
    for key in stale_keys:
        del _coherence_report_buffer[key]


def _store_coherence_report(report: CoherenceReport):
    """Store a coherence report (overwrites previous for same pair)"""
    _cleanup_old_coherence_reports()
    key = f"{report.source_agent_id}:{report.target_agent_id}"
    _coherence_report_buffer[key] = report.to_dict()
    logger.debug(f"[CIRS/COHERENCE_REPORT] {report.source_agent_id} -> {report.target_agent_id}: similarity={report.similarity_score:.3f}")


def _get_coherence_reports(
    source_agent_id: Optional[str] = None,
    target_agent_id: Optional[str] = None,
    min_similarity: Optional[float] = None,
    limit: int = 50
) -> List[Dict[str, Any]]:
    """Query recent coherence reports."""
    _cleanup_old_coherence_reports()

    results = []
    for key, report in _coherence_report_buffer.items():
        if source_agent_id and report["source_agent_id"] != source_agent_id:
            continue
        if target_agent_id and report["target_agent_id"] != target_agent_id:
            continue
        if min_similarity is not None and report.get("similarity_score", 0) < min_similarity:
            continue
        results.append(report)

    results.sort(key=lambda x: x.get("similarity_score", 0), reverse=True)
    return results[:limit]


# =============================================================================
# Boundary Contract Storage
# =============================================================================

_boundary_contract_buffer: Dict[str, Dict[str, Any]] = {}


def _store_boundary_contract(contract: BoundaryContract):
    """Store a boundary contract (overwrites previous for same agent)"""
    _boundary_contract_buffer[contract.agent_id] = contract.to_dict()
    logger.debug(f"[CIRS/BOUNDARY_CONTRACT] agent={contract.agent_id}, trust_default={contract.trust_default.value}")


def _get_boundary_contract(agent_id: str) -> Optional[Dict[str, Any]]:
    """Get boundary contract for an agent"""
    return _boundary_contract_buffer.get(agent_id)


def _get_all_boundary_contracts() -> List[Dict[str, Any]]:
    """Get all boundary contracts"""
    return list(_boundary_contract_buffer.values())


# =============================================================================
# Governance Action Storage
# =============================================================================

_governance_action_buffer: Dict[str, Dict[str, Any]] = {}
GOVERNANCE_ACTION_TTL_HOURS = 24


def _cleanup_old_governance_actions():
    """Remove old governance actions"""
    cutoff = datetime.now() - timedelta(hours=GOVERNANCE_ACTION_TTL_HOURS)
    cutoff_iso = cutoff.isoformat()
    stale_ids = [
        aid for aid, action in _governance_action_buffer.items()
        if action["timestamp"] < cutoff_iso
    ]
    for aid in stale_ids:
        del _governance_action_buffer[aid]


def _store_governance_action(action: GovernanceAction):
    """Store a governance action"""
    _cleanup_old_governance_actions()
    _governance_action_buffer[action.action_id] = action.to_dict()
    logger.info(f"[CIRS/GOVERNANCE_ACTION] {action.action_type.value}: {action.initiator_agent_id} -> {action.target_agent_id}")


def _get_governance_action(action_id: str) -> Optional[Dict[str, Any]]:
    """Get a governance action by ID"""
    return _governance_action_buffer.get(action_id)


def _get_governance_actions_for_agent(
    agent_id: str,
    as_initiator: bool = True,
    as_target: bool = True,
    status: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Get governance actions involving an agent"""
    _cleanup_old_governance_actions()

    results = []
    for action in _governance_action_buffer.values():
        is_initiator = action["initiator_agent_id"] == agent_id
        is_target = action["target_agent_id"] == agent_id

        if (as_initiator and is_initiator) or (as_target and is_target):
            if status is None or action["status"] == status:
                results.append(action)

    results.sort(key=lambda x: x["timestamp"], reverse=True)
    return results
