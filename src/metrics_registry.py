"""
Prometheus metrics definitions for the governance MCP server.

Extracted from mcp_server.py. All metric objects are module-level singletons.
Import individual metrics or use `from src.metrics_registry import *`.
"""

from prometheus_client import Counter, Gauge, Histogram

# Tool call metrics
TOOL_CALLS_TOTAL = Counter(
    'unitares_tool_calls_total',
    'Total tool calls',
    ['tool_name', 'status']
)

TOOL_CALL_DURATION = Histogram(
    'unitares_tool_call_duration_seconds',
    'Tool call duration in seconds',
    ['tool_name'],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
)

# Agent metrics
AGENTS_TOTAL = Gauge(
    'unitares_agents_total',
    'Total agents by status',
    ['status']
)

# Governance metrics
GOVERNANCE_DECISIONS = Counter(
    'unitares_governance_decisions_total',
    'Total governance decisions',
    ['action']
)

GOVERNANCE_ENERGY = Gauge(
    'unitares_governance_energy',
    'Current governance energy level',
    ['agent_id']
)

GOVERNANCE_COHERENCE = Gauge(
    'unitares_governance_coherence',
    'Current governance coherence',
    ['agent_id']
)

# Knowledge graph metrics. Set by the KG lifecycle background task
# (src/knowledge_graph_lifecycle.py::run_kg_lifecycle_cleanup) at startup and on
# its periodic sweep — an absolute COUNT(*), not inc-on-store, so it survives
# restarts and reflects deletes/archival. NOT set in the /metrics handler, which
# is deliberately DB-free (must not await asyncpg on the scrape path).
KNOWLEDGE_NODES_TOTAL = Gauge(
    'unitares_knowledge_nodes_total',
    'Total knowledge graph discoveries (refreshed by the KG lifecycle task)'
)

# Dialectic metrics
DIALECTIC_SESSIONS_ACTIVE = Gauge(
    'unitares_dialectic_sessions_active',
    'Number of active dialectic sessions'
)

# Server info (static)
SERVER_INFO = Gauge(
    'unitares_server_info',
    'Server version info',
    ['version']
)

# Server uptime and health metrics
SERVER_UPTIME = Gauge(
    'unitares_server_uptime_seconds',
    'Server uptime in seconds'
)

SERVER_ERRORS_TOTAL = Counter(
    'unitares_server_errors_total',
    'Total server errors',
    ['error_type']
)

REQUEST_DURATION = Histogram(
    'unitares_request_duration_seconds',
    'HTTP request duration',
    ['method', 'endpoint'],
    buckets=(0.01, 0.05, 0.1, 0.5, 1.0, 2.5, 5.0, 10.0)
)

REQUEST_TOTAL = Counter(
    'unitares_requests_total',
    'Total HTTP requests',
    ['method', 'endpoint', 'status_code']
)
