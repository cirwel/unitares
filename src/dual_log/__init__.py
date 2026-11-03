"""
UNITARES Dual-Log Architecture

Implements the patent's dual-log system for grounded EISV dynamics.

Architecture:
    Operational Log (server-derived) ──┐
                                       ├──► Continuity Layer ──► Grounded Metrics
    Reflective Log (agent-reported) ───┘

Usage:
    from src.dual_log import ContinuityLayer, RestorativeBalanceMonitor
    
    layer = ContinuityLayer(agent_id, redis_client)
    metrics = layer.process_update(
        response_text="...",
        self_complexity=0.5,
        self_confidence=0.8,
        client_session_id="abc123"
    )
    
    # metrics.derived_complexity - server-derived (grounded)
    # metrics.complexity_divergence - |derived - reported|
    # metrics.E_input, I_input, S_input - grounded EISV inputs
"""

from .operational import (
    OperationalEntry,
    analyze_response_text,
    create_operational_entry,
)

from .reflective import (
    ReflectiveEntry,
    create_reflective_entry,
)

from .continuity import (
    ContinuityMetrics,
    ContinuityLayer,
    derive_complexity,
    tool_usage_complexity,
    compute_continuity_metrics,
    is_automatic_or_rollup_checkin,
    should_exclude_template_divergence,
)

from .restorative import (
    RestorativeStatus,
    RestorativeBalanceMonitor,
)

__all__ = [
    # Operational
    'OperationalEntry',
    'analyze_response_text',
    'create_operational_entry',
    # Reflective
    'ReflectiveEntry', 
    'create_reflective_entry',
    # Continuity
    'ContinuityMetrics',
    'ContinuityLayer',
    'derive_complexity',
    'tool_usage_complexity',
    'compute_continuity_metrics',
    'is_automatic_or_rollup_checkin',
    'should_exclude_template_divergence',
    # Restorative
    'RestorativeStatus',
    'RestorativeBalanceMonitor',
]
