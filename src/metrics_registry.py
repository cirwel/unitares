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

# Knowledge graph metrics
KNOWLEDGE_NODES_TOTAL = Gauge(
    'unitares_knowledge_nodes_total',
    'Total knowledge graph nodes'
)

# Dialectic metrics
DIALECTIC_SESSIONS_ACTIVE = Gauge(
    'unitares_dialectic_sessions_active',
    'Number of active dialectic sessions'
)

# Wave 3a BEAM-outbound proxy metrics (governance MCP -> BEAM listener).
# The DB-backed coordination events + measurement rows in
# src/wave3a_beam_proxy.py are the durable §4.2 audit channel; these two are
# the live /metrics surface (Grafana) the audit channel does not expose:
# per-tool call counts, latency, and fallback reason in real time.
# `outcome` is "ok" on success or the ProxyResult.fallback_reason on failure
# (timeout / non_200 / connect_error / decode_error / envelope_invalid / other).
BEAM_PROXY_CALLS_TOTAL = Counter(
    'unitares_beam_proxy_calls_total',
    'Total BEAM proxy dispatch attempts by tool and outcome',
    ['tool_name', 'outcome']
)

BEAM_PROXY_LATENCY = Histogram(
    'unitares_beam_proxy_latency_seconds',
    'BEAM proxy outbound call latency in seconds by tool and outcome',
    ['tool_name', 'outcome'],
    # Bucketed tight around the 500ms (§3.2) hard timeout budget so the
    # timeout cliff and the sub-budget distribution are both legible.
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.4, 0.5, 0.75, 1.0)
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
