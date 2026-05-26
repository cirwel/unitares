"""
CIRS Protocol data types — enums and dataclasses.

Leaf module — no imports from other cirs_* sub-modules.
"""

from typing import Dict, Any, Optional
from enum import Enum
from dataclasses import dataclass


class VoidSeverity(str, Enum):
    """Void alert severity levels per UARG spec"""
    WARNING = "warning"   # |V| > threshold, not yet critical
    CRITICAL = "critical"  # |V| significantly elevated, system at risk


class AgentRegime(str, Enum):
    """Agent operational regime per trajectory identity theory"""
    DIVERGENCE = "divergence"    # Exploring, high entropy acceptable
    TRANSITION = "transition"    # Moving between regimes
    CONVERGENCE = "convergence"  # Focusing, reducing entropy
    STABLE = "stable"            # At equilibrium


@dataclass
class VoidAlert:
    """
    VOID_ALERT message structure per UARG Whitepaper.

    Fields:
        agent_id: Source agent identifier
        timestamp: ISO timestamp of void event
        severity: warning | critical
        V_snapshot: V value at time of alert
        context_ref: Optional pointer to logs/traces for debugging
        coherence_at_event: System coherence when void detected (helpful for peers)
        risk_at_event: Risk score when void detected
    """
    agent_id: str
    timestamp: str
    severity: VoidSeverity
    V_snapshot: float
    context_ref: Optional[str] = None
    coherence_at_event: Optional[float] = None
    risk_at_event: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "timestamp": self.timestamp,
            "severity": self.severity.value,
            "V_snapshot": self.V_snapshot,
            "context_ref": self.context_ref,
            "coherence_at_event": self.coherence_at_event,
            "risk_at_event": self.risk_at_event,
        }


@dataclass
class StateAnnounce:
    """
    STATE_ANNOUNCE message structure per UARG Whitepaper.

    Broadcasts EISV + trajectory state to enable multi-agent coordination.
    This is the foundational heartbeat for CIRS resonance.
    """
    agent_id: str
    timestamp: str
    eisv: Dict[str, float]
    coherence: float
    regime: str
    phi: float
    verdict: str
    risk_score: float
    trajectory_signature: Optional[Dict[str, Any]] = None
    purpose: Optional[str] = None
    update_count: int = 0
    trust_tier: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "agent_id": self.agent_id,
            "timestamp": self.timestamp,
            "eisv": self.eisv,
            "coherence": self.coherence,
            "regime": self.regime,
            "phi": self.phi,
            "verdict": self.verdict,
            "risk_score": self.risk_score,
            "update_count": self.update_count,
        }
        if self.trajectory_signature:
            result["trajectory_signature"] = self.trajectory_signature
            from src.governance_glossary import annotate_trajectory_signature_terms
            signature_glossary = annotate_trajectory_signature_terms(self.trajectory_signature)
            if signature_glossary:
                result["trajectory_signature_glossary"] = signature_glossary
        if self.purpose:
            result["purpose"] = self.purpose
        if self.trust_tier:
            result["trust_tier"] = self.trust_tier
            from src.governance_glossary import explain_trust_tier
            result["trust_tier_meta"] = explain_trust_tier(self.trust_tier)
        return result


@dataclass
class ResonanceAlert:
    """RESONANCE_ALERT: Emitted when agent's governor detects sustained oscillation."""
    agent_id: str
    timestamp: str
    oi: float
    phase: str
    tau_current: float
    beta_current: float
    flips: int
    duration_updates: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "RESONANCE_ALERT",
            "agent_id": self.agent_id,
            "timestamp": self.timestamp,
            "oi": self.oi,
            "phase": self.phase,
            "tau_current": self.tau_current,
            "beta_current": self.beta_current,
            "flips": self.flips,
            "duration_updates": self.duration_updates,
        }


@dataclass
class StabilityRestored:
    """STABILITY_RESTORED: Emitted when agent exits resonance."""
    agent_id: str
    timestamp: str
    oi: float
    tau_settled: float
    beta_settled: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "STABILITY_RESTORED",
            "agent_id": self.agent_id,
            "timestamp": self.timestamp,
            "oi": self.oi,
            "tau_settled": self.tau_settled,
            "beta_settled": self.beta_settled,
        }


@dataclass
class CoherenceReport:
    """
    COHERENCE_REPORT message structure per UARG Whitepaper.

    Shares pairwise similarity metrics between agents for multi-agent coordination.
    """
    source_agent_id: str
    timestamp: str
    target_agent_id: str
    similarity_score: float
    eisv_similarity: Dict[str, float]
    regime_match: bool
    verdict_match: bool
    trajectory_similarity: Optional[Dict[str, float]] = None
    recommendation: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "source_agent_id": self.source_agent_id,
            "timestamp": self.timestamp,
            "target_agent_id": self.target_agent_id,
            "similarity_score": self.similarity_score,
            "eisv_similarity": self.eisv_similarity,
            "regime_match": self.regime_match,
            "verdict_match": self.verdict_match,
        }
        if self.trajectory_similarity:
            result["trajectory_similarity"] = self.trajectory_similarity
        if self.recommendation:
            result["recommendation"] = self.recommendation
        return result


class TrustLevel(str, Enum):
    """Trust levels for boundary contracts"""
    FULL = "full"           # Full trust - share all state, accept delegations
    PARTIAL = "partial"     # Partial trust - share EISV, limited delegation
    OBSERVE = "observe"     # Observe only - share state, no delegation
    NONE = "none"           # No trust - minimal interaction


class VoidResponsePolicy(str, Enum):
    """How to respond when peer enters void state"""
    NOTIFY = "notify"       # Send alert, continue operation
    ASSIST = "assist"       # Offer assistance, share resources
    ISOLATE = "isolate"     # Reduce interaction until void resolves
    COORDINATE = "coordinate"  # Active coordination to help resolve


@dataclass
class BoundaryContract:
    """
    BOUNDARY_CONTRACT message structure per UARG Whitepaper.

    Declares trust policies and void response rules between agents.
    """
    agent_id: str
    timestamp: str
    trust_default: TrustLevel
    trust_overrides: Dict[str, str]  # agent_id -> TrustLevel value
    void_response_policy: VoidResponsePolicy
    max_delegation_complexity: float
    accept_coherence_threshold: float
    boundary_violations: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "timestamp": self.timestamp,
            "trust_default": self.trust_default.value,
            "trust_overrides": self.trust_overrides,
            "void_response_policy": self.void_response_policy.value,
            "max_delegation_complexity": self.max_delegation_complexity,
            "accept_coherence_threshold": self.accept_coherence_threshold,
            "boundary_violations": self.boundary_violations,
        }


class GovernanceActionType(str, Enum):
    """Types of governance actions for multi-agent coordination"""
    VOID_INTERVENTION = "void_intervention"
    COHERENCE_BOOST = "coherence_boost"
    DELEGATION_REQUEST = "delegation_request"
    DELEGATION_RESPONSE = "delegation_response"
    COORDINATION_SYNC = "coordination_sync"


@dataclass
class GovernanceAction:
    """
    GOVERNANCE_ACTION message structure per UARG Whitepaper.

    Coordinates interventions across agents.
    """
    action_id: str
    timestamp: str
    action_type: GovernanceActionType
    initiator_agent_id: str
    target_agent_id: str
    payload: Dict[str, Any]
    status: str = "pending"
    response: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "action_id": self.action_id,
            "timestamp": self.timestamp,
            "action_type": self.action_type.value,
            "initiator_agent_id": self.initiator_agent_id,
            "target_agent_id": self.target_agent_id,
            "payload": self.payload,
            "status": self.status,
        }
        if self.response:
            result["response"] = self.response
        return result
