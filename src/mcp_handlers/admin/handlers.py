"""
Admin tool handlers.
"""

from typing import Dict, Any, Sequence, Optional
from mcp.types import TextContent
import sys
from datetime import datetime
from pathlib import Path
from ..utils import success_response, error_response, require_registered_agent
from ..decorators import mcp_tool
from ..validators import validate_file_path_policy
from src.logging_utils import get_logger
from src.mcp_handlers.shared import lazy_mcp_server as mcp_server

logger = get_logger(__name__)

def build_server_info_payload() -> Dict[str, Any]:
    """Build the ``get_server_info`` response payload.

    Single source of truth for the payload shape, shared by the MCP handler
    below and the Wave 3a probe endpoint
    (``src/mcp_handlers/wave3a_probe.py::_server_info``). The Wave 3a §2.6
    parity contract pins this key set via the golden fixture at
    ``tests/fixtures/wave3a_response_golden/get_server_info.json`` — shape
    changes here require regenerating that fixture and auditing the BEAM
    pass-through handler
    (``elixir/wave3a_handlers/lib/wave3a_handlers/handlers/get_server_info.ex``).

    FIND-R3 / RFC §6 Q2: ``current_pid``, ``is_current``, and ``transport``
    describe THIS Python process. When served via the Wave 3a BEAM proxy the
    values still refer to the Python backend, not the BEAM listener — Q2
    resolved to accepting Python-PID semantics rather than injecting
    BEAM-side overrides.
    """
    import time
    import os

    # Detect transport from current process args (HTTP vs stdio).
    # This prevents HTTP from accidentally reporting stdio processes (and vice versa).
    argv = [str(a) for a in getattr(sys, "argv", [])]
    is_http = any("mcp_server.py" in a for a in argv)
    is_stdio = any("mcp_server_std.py" in a for a in argv)
    transport = "HTTP" if is_http else ("STDIO" if is_stdio else "unknown")
    target_script = "mcp_server.py" if is_http else ("mcp_server_std.py" if is_stdio else None)

    # Current pid should always be the live process hosting this handler.
    current_pid = os.getpid()

    # Prefer shared constants if available, fallback to local defaults.
    server_version = getattr(mcp_server, "SERVER_VERSION", None) or "unknown"
    server_build_date = getattr(mcp_server, "SERVER_BUILD_DATE", None) or "unknown"

    if mcp_server.PSUTIL_AVAILABLE:
        import psutil
        
        # Get all MCP server processes
        server_processes = []
        try:
            for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'create_time', 'status']):
                try:
                    cmdline = proc.info.get('cmdline', [])
                    if not cmdline:
                        continue

                    # Only include processes matching the current transport when detectable.
                    if target_script:
                        if not any(target_script in str(arg) for arg in cmdline):
                            continue
                    else:
                        # Unknown transport: include either server type if present.
                        if not any(('mcp_server_std.py' in str(arg) or 'mcp_server.py' in str(arg)) for arg in cmdline):
                            continue

                    pid = proc.info['pid']
                    create_time = proc.info.get('create_time', 0)
                    uptime_seconds = time.time() - create_time
                    uptime_minutes = int(uptime_seconds / 60)
                    uptime_hours = int(uptime_minutes / 60)

                    server_processes.append({
                        "pid": pid,
                        "is_current": pid == current_pid,
                        "uptime_seconds": int(uptime_seconds),
                        "uptime_formatted": f"{uptime_hours}h {uptime_minutes % 60}m",
                        "status": proc.info.get('status', 'unknown')
                    })
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except Exception as e:
            server_processes = [{"error": f"Could not enumerate processes: {e}"}]
        
        # Calculate current process uptime
        try:
            current_proc = psutil.Process(current_pid)
            current_uptime = time.time() - current_proc.create_time()
            # If process enumeration didn't find anything (e.g., uvicorn spawn cmdline quirks),
            # always include the current process so get_server_info is never empty.
            if not server_processes:
                uptime_minutes = int(current_uptime / 60)
                uptime_hours = int(uptime_minutes / 60)
                server_processes.append({
                    "pid": current_pid,
                    "is_current": True,
                    "uptime_seconds": int(current_uptime),
                    "uptime_formatted": f"{uptime_hours}h {uptime_minutes % 60}m",
                    "status": getattr(current_proc, "status", lambda: "unknown")()
                })
        except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
            current_uptime = 0
    else:
        server_processes = [{"error": "psutil not available - cannot enumerate processes"}]
        current_uptime = 0
    
    current_uptime_minutes = int(current_uptime / 60)
    current_uptime_hours = int(current_uptime_minutes / 60)

    # Get tool count (tool mode filtering removed - all tools always available)
    from src.mcp_handlers import TOOL_HANDLERS
    tool_count = len(TOOL_HANDLERS)

    # PID file differs by transport.
    project_root = Path(__file__).resolve().parent.parent.parent
    pid_file = (project_root / "data" / ".mcp_server.pid") if is_http else (project_root / "data" / ".mcp_server_std.pid")

    return {
        "transport": transport,
        "server_version": server_version,
        "version": server_version,  # Alias for consistency
        "build_date": server_build_date,
        "tool_count": tool_count,
        "current_pid": current_pid,
        "current_uptime_seconds": int(current_uptime),
        "current_uptime_formatted": f"{current_uptime_hours}h {current_uptime_minutes % 60}m",
        "total_server_processes": len([p for p in server_processes if "error" not in p]),
        "server_processes": server_processes,
        "pid_file_exists": pid_file.exists(),
        "pid_file": str(pid_file),
        "max_keep_processes": getattr(mcp_server, "MAX_KEEP_PROCESSES", None),
        "health": "healthy"
    }


@mcp_tool("get_server_info", timeout=10.0, requires_identity="pre_onboard")
async def handle_get_server_info(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Get MCP server version, process information, and health status"""
    return success_response(build_server_info_payload())

@mcp_tool("check_continuity_health", timeout=15.0, register=False)
async def handle_check_continuity_health(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """
    Check the health of agent persistence and provenance continuity features.

    Verifies that agent states, metadata, knowledge graph, and provenance
    information are being properly persisted across sessions.

    Args:
        agent_id: Specific agent to check (optional)
        deep_check: Run comprehensive checks including data integrity (default: False)

    Returns:
        Continuity health assessment with recommendations
    """
    agent_id = arguments.get("agent_id")
    deep_check = arguments.get("deep_check", False)

    try:
        # Refresh metadata so agents exploring get fresh agent/system info (not stale)
        import time
        try:
            cache_age = time.time() - mcp_server._metadata_cache_state.get("last_load_time", 0)
            if cache_age > mcp_server.EXPLORATION_CACHE_TTL:
                # Wave 2 audit: force=True KEPT here. This is the one TTL-gated
                # cache-refresh use case where force=True is structurally required
                # — load_metadata_async() returns early if metadata is already
                # loaded, so without force the admin handler can never refresh
                # the cache against external writes. Cost (3221 sequential
                # cache.set awaits) is bounded because the gate fires only when
                # cache_age > EXPLORATION_CACHE_TTL, not per call.
                await mcp_server.load_metadata_async(force=True)
        except (AttributeError, TypeError):
            pass

        health_report = {
            "timestamp": datetime.now().isoformat(),
            "checks": {},
            "recommendations": []
        }

        # Check agent metadata persistence
        metadata_count = len(mcp_server.agent_metadata) if hasattr(mcp_server, 'agent_metadata') else 0

        # Check knowledge graph persistence
        from src.mcp_handlers.knowledge.handlers import get_knowledge_graph
        graph = await get_knowledge_graph()
        graph_stats = await graph.get_stats()

        # Check for active agents
        active_agents = [aid for aid, meta in mcp_server.agent_metadata.items()
                        if meta.status in ['active', 'waiting_input']] if hasattr(mcp_server, 'agent_metadata') else []

        health_report["checks"]["agent_metadata"] = {
            "status": "healthy" if metadata_count > 0 else "warning",
            "count": metadata_count,
            "active_agents": len(active_agents)
        }

        health_report["checks"]["knowledge_graph"] = {
            "status": "healthy" if graph_stats.get("total_discoveries", 0) > 0 else "warning",
            "total_discoveries": graph_stats.get("total_discoveries", 0),
            "total_agents": graph_stats.get("total_agents", 0)
        }

        # Check provenance tracking
        provenance_count = 0
        if deep_check:
            # Sample some discoveries to check provenance
            discoveries = await graph.query({}, limit=10)
            for discovery in discoveries:
                if discovery.provenance:
                    provenance_count += 1

        health_report["checks"]["provenance_tracking"] = {
            "status": "healthy" if provenance_count > 0 else "info",
            "sample_provenance_count": provenance_count,
            "note": "Provenance captured on discovery creation"
        }

        # Check lineage tracking for specific agent
        if agent_id:
            lineage_info = {}
            if hasattr(mcp_server, 'agent_metadata') and agent_id in mcp_server.agent_metadata:
                meta = mcp_server.agent_metadata[agent_id]
                from src.mcp_handlers.identity.shared import _get_lineage
                lineage_info = {
                    "has_parent": meta.parent_agent_id is not None,
                    "spawn_reason": meta.spawn_reason,
                    "lineage_depth": len(_get_lineage(agent_id))
                }

            health_report["checks"]["agent_lineage"] = {
                "agent_id": agent_id,
                "lineage_info": lineage_info
            }

        # Generate recommendations
        if metadata_count == 0:
            health_report["recommendations"].append("No agent metadata found - ensure process_agent_update is being called")
        if graph_stats.get("total_discoveries", 0) == 0:
            health_report["recommendations"].append("No discoveries in knowledge graph - ensure store_knowledge_graph is working")
        if provenance_count == 0 and deep_check:
            health_report["recommendations"].append("No provenance data found - check that provenance capture is enabled")

        return success_response(health_report)

    except Exception as e:
        logger.error(f"Continuity health check failed: {e}", exc_info=True)
        return [error_response(f"Continuity health check failed: {e}")]

@mcp_tool("get_tool_usage_stats", timeout=15.0)
async def handle_get_tool_usage_stats(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Get tool usage statistics to identify which tools are actually used vs unused"""
    window_hours = arguments.get("window_hours", 24 * 7)  # Default: 7 days
    tool_name = arguments.get("tool_name")
    agent_id = arguments.get("agent_id")

    # Prefer the authoritative DB sink (audit.tool_usage); the legacy JSONL sink
    # is best-effort and has drifted stale. Fall back to JSONL only when the DB
    # is unavailable (degraded/local mode).
    from src.audit_db import get_tool_usage_stats_async
    stats = await get_tool_usage_stats_async(
        window_hours=window_hours,
        tool_name=tool_name,
        agent_id=agent_id,
    )
    if stats is None:
        from src.tool_usage_tracker import get_tool_usage_tracker
        stats = get_tool_usage_tracker().get_usage_stats(
            window_hours=window_hours,
            tool_name=tool_name,
            agent_id=agent_id,
        )
        if isinstance(stats, dict):
            stats["source"] = "jsonl_fallback"

    return success_response(stats)

def get_workspace_last_agent_file(mcp_server) -> Path:
    """Get the file path for storing last active agent."""
    return Path(mcp_server.project_root) / "data" / ".last_active_agent"

def get_workspace_last_agent(mcp_server) -> Optional[str]:
    """Get the last active agent for this workspace."""
    try:
        last_agent_file = get_workspace_last_agent_file(mcp_server)
        if last_agent_file.exists():
            agent_id = last_agent_file.read_text().strip()
            # Verify it still exists
            if agent_id in mcp_server.agent_metadata:
                return agent_id
    except Exception:
        pass
    return None

def set_workspace_last_agent(mcp_server, agent_id: str) -> None:
    """Set the last active agent for this workspace."""
    try:
        last_agent_file = get_workspace_last_agent_file(mcp_server)
        last_agent_file.parent.mkdir(parents=True, exist_ok=True)
        last_agent_file.write_text(agent_id)
    except Exception:
        pass  # Non-critical

@mcp_tool("health_check", timeout=5.0, requires_identity="pre_onboard")
async def handle_health_check(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Read the most recent cached health snapshot.

    Does NOT call the DB, Redis, or Pi at request time. That is intentional:
    calling get_health_check_data from inside an MCP tool handler awaits
    asyncpg inside the SDK's anyio task group and deadlocks. The snapshot
    is refreshed every 30s by the deep_health_probe_task background task
    running on the main event loop.


    Args:
        lite: If True (default), strip per-check detail and return a compact
              status summary. If False, return the full cached snapshot.
    """
    from src.services.health_snapshot import (
        get_snapshot,
        is_stale,
        PROBE_INTERVAL_SECONDS,
        STALENESS_THRESHOLD_SECONDS,
    )

    snapshot, age_seconds, produced_at = get_snapshot()

    if snapshot is None:
        return error_response(
            "Health snapshot not yet available — the deep health probe has "
            "not run. Try again in a few seconds.",
        )

    # Lite filter operates on the cached copy — does not re-run checks
    lite = arguments.get("lite", True)
    if lite:
        response = {
            "status": snapshot.get("status"),
            "version": snapshot.get("version"),
            "redis_present": snapshot.get("redis_present"),
            "identity_continuity_mode": snapshot.get("identity_continuity_mode"),
            "status_breakdown": snapshot.get("status_breakdown"),
            "operator_summary": snapshot.get("operator_summary"),
            "timestamp": snapshot.get("timestamp"),
        }
        full_checks = snapshot.get("checks", {})
        lite_checks = {}
        for name, check in full_checks.items():
            if not isinstance(check, dict):
                lite_checks[name] = check
                continue
            entry = {"status": check.get("status", "unknown")}
            for key in ("mode", "redis_present", "present", "source_of_truth", "session_binding_backend"):
                if key in check:
                    entry[key] = check[key]
            if "warning" in check:
                entry["warning"] = check["warning"]
            if "note" in check:
                entry["note"] = check["note"]
            lite_checks[name] = entry
        response["checks"] = lite_checks
        response["_note"] = "Use lite=false for full diagnostic detail"
    else:
        response = dict(snapshot)

    response["_cache"] = {
        "age_seconds": round(age_seconds, 1) if age_seconds is not None else None,
        "produced_at": produced_at,
        "stale": is_stale(age_seconds),
        "probe_interval_seconds": PROBE_INTERVAL_SECONDS,
        "staleness_threshold_seconds": STALENESS_THRESHOLD_SECONDS,
    }
    return success_response(response)

@mcp_tool("get_telemetry_metrics", timeout=15.0)
async def handle_get_telemetry_metrics(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Get comprehensive telemetry metrics: skip rates, confidence distributions, calibration status
    
    Note: Calibration data is system-wide and can be large. Use include_calibration=False to reduce response size.
    """
    import asyncio
    from src.telemetry import TelemetryCollector
    
    telemetry = TelemetryCollector()
    
    agent_id = arguments.get("agent_id")
    window_hours = arguments.get("window_hours", 24)
    include_calibration = arguments.get("include_calibration", False)  # Default False to reduce context bloat
    
    # Run blocking I/O operations in executor to prevent hanging
    loop = asyncio.get_running_loop()  # Use get_running_loop() instead of deprecated get_event_loop()
    
    try:
        # Always fetch skip metrics and confidence distribution (agent-specific, small)
        skip_metrics, conf_dist, suspicious = await asyncio.gather(
            loop.run_in_executor(None, telemetry.get_skip_rate_metrics, agent_id, window_hours),
            loop.run_in_executor(None, telemetry.get_confidence_distribution, agent_id, window_hours),
            loop.run_in_executor(None, telemetry.detect_suspicious_patterns, agent_id)
        )
        
        response = {
            "agent_id": agent_id or "all_agents",
            "window_hours": window_hours,
            "skip_rate_metrics": skip_metrics,
            "confidence_distribution": conf_dist,
            "suspicious_patterns": suspicious
        }

        # Include lightweight knowledge-graph performance stats (in-process, low overhead).
        try:
            from src.perf_monitor import snapshot as perf_snapshot
            response["knowledge_graph_perf"] = perf_snapshot()
        except Exception:
            response["knowledge_graph_perf"] = {"note": "perf snapshot unavailable"}
        
        # Only include calibration if explicitly requested (reduces context bloat)
        if include_calibration:
            calibration_metrics = await loop.run_in_executor(
                None, telemetry.get_calibration_metrics
            )
            response["calibration"] = calibration_metrics
        else:
            # Provide summary instead of full calibration data
            response["calibration"] = {
                "note": "Calibration data excluded to reduce response size. Set include_calibration=true to get full calibration metrics.",
                "related_tool": "check_calibration"
            }
        
        return success_response(response)
    except Exception as e:
        logger.error(f"Error in get_telemetry_metrics: {e}")
        return [error_response(f"Error collecting telemetry: {str(e)}")]

@mcp_tool("reset_monitor", timeout=10.0)
async def handle_reset_monitor(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Reset governance state for an agent"""
    # PROACTIVE GATE: Require agent to be registered
    agent_id, error = require_registered_agent(arguments)
    if error:
        return [error]  # Returns onboarding guidance if not registered
    
    if agent_id in mcp_server.monitors:
        del mcp_server.monitors[agent_id]
        message = f"Monitor reset for agent: {agent_id}"
    else:
        message = f"Monitor not found for agent: {agent_id} (may not be loaded)"
    
    return success_response({
        "message": message,
        "agent_id": agent_id
    })

@mcp_tool("cleanup_stale_locks", timeout=15.0)
async def handle_cleanup_stale_locks(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Clean up stale lock files that are no longer held by active processes"""
    try:
        from src.lock_cleanup import cleanup_stale_state_locks
        
        max_age = arguments.get('max_age_seconds', 300.0)
        dry_run = arguments.get('dry_run', False)
        
        project_root = Path(__file__).parent.parent.parent
        result = cleanup_stale_state_locks(project_root=project_root, max_age_seconds=max_age, dry_run=dry_run)
        
        return success_response({
            "cleaned": result['cleaned'],
            "kept": result['kept'],
            "errors": result['errors'],
            "dry_run": dry_run,
            "max_age_seconds": max_age,
            "cleaned_locks": result.get('cleaned_locks', []),
            "kept_locks": result.get('kept_locks', []),
            "message": f"Cleaned {result['cleaned']} stale lock(s), kept {result['kept']} active lock(s)"
        })
    except Exception as e:
        return [error_response(f"Error cleaning stale locks: {str(e)}")]

@mcp_tool("get_workspace_health", timeout=20.0)
async def handle_get_workspace_health(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Handle get_workspace_health tool - comprehensive workspace health status"""
    from src.workspace_health import get_workspace_health
    
    try:
        health_data = get_workspace_health()
        return success_response(health_data)
    except Exception as e:
        # SECURITY: Log full traceback internally but sanitize for client
        logger.error(f"Error checking workspace health: {e}", exc_info=True)
        return [error_response(
            f"Error checking workspace health: {str(e)}",
            recovery={
                "action": "Check system configuration and try again",
                "related_tools": ["health_check", "get_server_info"]
            }
        )]

@mcp_tool("debug_request_context", timeout=5.0)
async def handle_debug_request_context(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """
    Debug request context - shows raw diagnostic info about session, identity, and bindings.
    
    SIMPLIFIED: Just shows what's in memory - no complex logic, no guessing.
    Use this to understand what the server sees, not to determine your identity.
    For identity, use identity() instead.
    """
    import hashlib
    from datetime import datetime
    from src.mcp_handlers import TOOL_HANDLERS
    from ..context import get_context_agent_id, get_context_session_key, get_session_signals
    from ..identity.handlers import derive_session_key

    # Get raw diagnostic info - no complex logic
    # NOTE (Dec 2025): identity_v2 is the AUTHORITATIVE source of truth for identity.
    # Context agent_id was resolved via identity_v2.resolve_session_identity() at dispatch entry.
    context_agent_id = get_context_agent_id()  # Authoritative (from identity_v2)
    context_session_key = get_context_session_key()
    signals = get_session_signals()
    session_key = context_session_key or (await derive_session_key(signals, arguments or {}))

    # Get tool registry info
    tool_names = sorted(TOOL_HANDLERS.keys())
    tool_count = len(tool_names)
    registry_hash = hashlib.md5(",".join(tool_names).encode()).hexdigest()[:8]

    # Detect transport
    import sys
    argv = [str(a) for a in getattr(sys, "argv", [])]
    is_http = any("mcp_server.py" in a for a in argv)
    is_stdio = any("mcp_server_std.py" in a for a in argv)
    transport = "http" if is_http else ("stdio" if is_stdio else "unknown")

    # Get validator info
    validator_version = "1.0.0"
    try:
        from ..validators import VALIDATOR_VERSION
        validator_version = VALIDATOR_VERSION
    except (ImportError, AttributeError):
        pass

    # Diagnostic: Check what bindings exist in LEGACY identity module (for debugging)
    # NOTE: identity_v2 is now authoritative - legacy bindings shown for diagnostic purposes only
    legacy_bindings = {}
    legacy_bindings_count = 0
    uuid_prefix_keys = []
    uuid_prefix_mappings = {}
    try:
        from ..identity.shared import _session_identities, _uuid_prefix_index
        for k, v in list(_session_identities.items())[:10]:  # Show first 10
            agent_id = v.get("bound_agent_id")
            if agent_id:
                legacy_bindings[k] = agent_id[:8] + "..."
            else:
                legacy_bindings[k] = "None"
        uuid_prefix_keys = list(_uuid_prefix_index.keys())[:10]  # Show first 10
        uuid_prefix_mappings = {k: _uuid_prefix_index[k][:8] + "..." for k in uuid_prefix_keys}
        legacy_bindings_count = len(_session_identities)
    except Exception as e:
        import traceback
        legacy_bindings = {"error": str(e), "traceback": traceback.format_exc()}

    # SIMPLIFIED: Just show raw diagnostics - no complex logic
    result = {
        "success": True,
        "timestamp": datetime.now().isoformat(),
        "transport": transport,
        "session": {
            "session_key": session_key,
            "context_session_key": context_session_key,
            "context_agent_id": context_agent_id,
            "note": "context_agent_id is AUTHORITATIVE (from identity_v2). Use identity() to check your identity."
        },
        "diagnostics": {
            "legacy_bindings_in_memory": legacy_bindings,
            "legacy_bindings_count": legacy_bindings_count,
            "legacy_uuid_prefix_index": {
                "keys": uuid_prefix_keys,
                "mappings": uuid_prefix_mappings,
                "most_recent": uuid_prefix_keys[-1] if uuid_prefix_keys else None
            },
            "note": "Legacy identity.py bindings shown for debugging. identity_v2 is authoritative (via context)."
        },
        "identity_injection": {
            "enabled": True,
            "injection_point": "dispatch_tool (before validation)",
            "auto_create_enabled": True,
        },
        "tool_registry": {
            "count": tool_count,
            "sample_tools": tool_names[:10],
            "registry_hash": registry_hash,
        },
        "validator": {
            "version": validator_version,
        },
        "server": {
            "version": getattr(mcp_server, "SERVER_VERSION", "unknown"),
        },
        "recommendation": "For identity, use identity() instead. This tool is for debugging session/context issues."
    }

    return success_response(result)

@mcp_tool("validate_file_path", timeout=5.0)
async def handle_validate_file_path(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """
    Validate file path against project policies (anti-proliferation).
    
    Use this tool BEFORE creating files to check if they violate policy.
    
    Policies checked:
    - Test scripts (test_*.py, demo_*.py) must be in tests/ directory
    - Markdown files in docs/analysis/, docs/fixes/, etc. should use store_knowledge_graph() instead
    - New markdown files should be on approved list or ≥500 words
    
    Returns:
    - "valid": Path is OK
    - "warning": Path violates policy (non-blocking, but should be reconsidered)
    """
    file_path = arguments.get("file_path")
    
    if not file_path:
        return [error_response(
            "file_path parameter is required",
            details={"error_type": "missing_parameter", "parameter": "file_path"},
            recovery={
                "action": "Provide file_path parameter",
                "workflow": ["1. Call validate_file_path with file_path parameter", "2. Review response before creating file"]
            }
        )]
    
    # Validate using policy checker
    warning, error = validate_file_path_policy(file_path)
    
    if error:
        return [error]
    
    if warning:
        # FRICTION FIX: Provide clearer guidance about when to use knowledge graph vs markdown
        guidance = {
            "use_knowledge_graph_for": [
                "Insights and discoveries",
                "Bug findings and security issues",
                "Pattern observations",
                "Questions and answers",
                "Quick notes and learnings"
            ],
            "use_markdown_for": [
                "Reference documentation (guides, API docs)",
                "Project README files",
                "Changelogs and version history",
                "Approved documentation files"
            ],
            "decision_heuristic": "If it's an insight/discovery → knowledge graph. If it's reference docs → markdown (and must be on approved list)."
        }
        
        return success_response({
            "valid": False,
            "status": "warning",
            "warning": warning,
            "file_path": file_path,
            "recommendation": "Consider using store_knowledge_graph() for insights/discoveries, or consolidate into existing approved docs",
            "guidance": guidance,
            "related_tools": ["store_knowledge_graph", "list_knowledge_graph", "search_knowledge_graph"],
            "quick_action": "For insights/discoveries, use: store_knowledge_graph(discovery_type='insight', summary='...', tags=[...])"
        })
    
    return success_response({
        "valid": True,
        "status": "ok",
        "file_path": file_path,
        "message": "File path complies with project policies"
    })

@mcp_tool("get_connection_status", timeout=5.0)
async def handle_get_connection_status(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """
    Get MCP connection status and tool availability.
    
    Helps agents verify they're connected to the MCP server and can use tools.
    Especially useful for detecting when tools are not available (e.g., wrong chatbox in Mac ChatGPT).
    """
    
    # Check if we can access server
    server_available = mcp_server is not None
    
    # Check transport type
    import sys
    argv = [str(a) for a in getattr(sys, "argv", [])]
    is_http = any("mcp_server.py" in a for a in argv)
    is_stdio = any("mcp_server_std.py" in a for a in argv)
    transport = "HTTP" if is_http else ("STDIO" if is_stdio else "unknown")
    
    # Check if tools are available
    import src.mcp_handlers as _handlers
    tools_available = len(getattr(_handlers, 'TOOL_HANDLERS', {})) > 0
    
    # Get current session identity if available
    session_bound = False
    resolved_agent_id = None
    resolved_uuid = None
    try:
        from ..context import get_context_agent_id
        context_id = get_context_agent_id()
        if context_id:
            session_bound = True
            resolved_uuid = context_id
            # Try to get display name
            if context_id in mcp_server.agent_metadata:
                meta = mcp_server.agent_metadata[context_id]
                resolved_agent_id = getattr(meta, 'structured_id', None) or getattr(meta, 'label', None)
    except Exception:
        pass
    
    status = "connected" if (server_available and tools_available) else "disconnected"
    
    return success_response({
        "status": status,
        "server_available": server_available,
        "tools_available": tools_available,
        "transport": transport,
        "session_bound": session_bound,
        "resolved_agent_id": resolved_agent_id,
        "resolved_uuid": (resolved_uuid[:8] + "...") if resolved_uuid else None,
        "message": "✅ Tools Connected" if status == "connected" else "❌ Tools Not Available",
        "recommendation": "You can use MCP tools" if status == "connected" else "Check MCP server connection and configuration"
    }, arguments=arguments)

# REMOVED: quick_start - deprecated Dec 2025, identity auto-binds on first tool call
# Use identity(name="...") to set display name, or just call any tool (identity auto-creates)
