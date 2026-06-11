"""
Type definitions for MCP tool handlers.

Provides TypedDict definitions for better type safety and IDE support.
"""

from typing import TypedDict, Optional, List, Dict, Any


class AgentMetadataDict(TypedDict, total=False):
    """Type definition for agent metadata dictionary"""
    agent_id: str
    status: str  # "active", "waiting_input", "paused", "archived", "deleted"
    created_at: str
    last_update_at: str
    paused_at: Optional[str]
    tags: List[str]
    notes: Optional[str]
    api_key: Optional[str]
    risk_score: float
    coherence: float
    void_active: bool
    lifecycle_events: List[Dict[str, Any]]


class GovernanceMetricsDict(TypedDict, total=False):
    """Type definition for governance metrics response"""
    agent_id: str
    timestamp: str
    E: float  # Energy
    I: float  # Information Integrity
    S: float  # Entropy
    V: float  # Void Integral
    coherence: float
    risk_score: float
    attention_score: float
    void_active: bool
    health_status: str  # "healthy", "moderate", "critical", "unknown"


class DialecticSessionDict(TypedDict, total=False):
    """Type definition for dialectic session data"""
    session_id: str
    paused_agent_id: str
    reviewer_agent_id: str
    phase: str  # "thesis", "antithesis", "synthesis", "resolved", "escalated", "failed"
    created_at: str
    dispute_type: Optional[str]  # "dispute", "correction", "verification", None
    discovery_id: Optional[str]
    transcript: List[Dict[str, Any]]
    resolution: Optional[Dict[str, Any]]


class ResolutionDict(TypedDict, total=False):
    """Type definition for resolution data"""
    action: str  # "resume", "block", "escalate", "cooldown"
    root_cause: str
    conditions: List[str]
    reasoning: str
    signatures: Dict[str, str]  # agent_id -> signature


class ErrorResponseDict(TypedDict, total=False):
    """Type definition for error response"""
    success: bool  # Always False
    error: str
    error_code: Optional[str]
    error_category: Optional[str]  # "validation_error", "auth_error", "system_error"
    details: Optional[Dict[str, Any]]
    recovery: Optional[Dict[str, Any]]
    context: Optional[Dict[str, Any]]


class SuccessResponseDict(TypedDict, total=False):
    """Type definition for success response"""
    success: bool  # Always True
    data: Dict[str, Any]


class ToolArgumentsDict(TypedDict, total=False):
    """Type definition for common tool arguments"""
    agent_id: Optional[str]
    api_key: Optional[str]
    session_id: Optional[str]
    discovery_id: Optional[str]


class CalibrationUpdateDict(TypedDict, total=False):
    """Type definition for calibration update data"""
    agent_id: str
    confidence: float
    predicted_correct: bool
    actual_correct: bool
    complexity_discrepancy: Optional[float]
    source: str  # "ground_truth", "dialectic_peer_review", etc.
    timestamp: str

