"""
UNITARES Governance - Agent State Management

Re-export facade — all functions have moved to focused modules.
Existing imports continue to work unchanged.

Modules:
  agent_metadata_model      — AgentMetadata dataclass, agent_metadata dict, constants
  agent_process_mgmt        — PID/lock files, process cleanup, signal handling
  agent_monitor_state       — Monitor instances, state file I/O
  agent_identity_auth       — ID validation, API keys, ownership verification
  agent_metadata_persistence— Loading/saving metadata (PostgreSQL, JSON snapshots)
  agent_lifecycle           — Monitor creation, archival, standardized info
  agent_loop_detection      — Loop detection, authenticated updates, auto-recovery
"""

from __future__ import annotations

# --- agent_metadata_model (leaf) ---
from src.agent_metadata_model import (
    project_root,
    _PROJECT_ROOT,
    AgentMetadata,
    agent_metadata,
    TextContent,
    _load_version,
    SERVER_VERSION,
    SERVER_BUILD_DATE,
    _normalize_http_proxy_base,
    _metadata_loading_lock,
    _metadata_loading,
    _metadata_loaded,
    _metadata_loaded_event,
    _metadata_cache_state,
    _metadata_batch_state,
    EXPLORATION_CACHE_TTL,
)

# --- agent_process_mgmt ---
from src.agent_process_mgmt import (
    AIOFILES_AVAILABLE,
    PSUTIL_AVAILABLE,
    PID_FILE,
    LOCK_FILE,
    MAX_KEEP_PROCESSES,
    CURRENT_PID,
    lock_manager,
    health_checker,
    process_mgr,
    SERVER_START_TIME,
    _shutdown_requested,
    signal_handler,
    write_pid_file,
    remove_pid_file,
    init_server_process,
    cleanup_stale_processes,
)

# --- agent_monitor_state ---
from src.agent_monitor_state import (
    monitors,
    get_state_file,
    _write_state_file,
    save_monitor_state_async,
    save_monitor_state,
    load_monitor_state,
)

# --- agent_identity_auth ---
from src.agent_identity_auth import (
    check_agent_status,
    check_agent_id_default,
    _detect_ci_status,
    validate_agent_id_format,
    require_explicit_agent_id,
    generate_api_key,
    verify_agent_ownership,
    require_agent_auth,
)

# --- agent_metadata_persistence ---
from src.agent_metadata_persistence import (
    METADATA_FILE,
    UNITARES_METADATA_BACKEND,
    UNITARES_METADATA_WRITE_JSON_SNAPSHOT,
    _metadata_backend_resolved,
    _resolve_metadata_backend,
    _write_metadata_snapshot_json_sync,
    _load_metadata_from_postgres_async,
    _parse_metadata_dict,
    _acquire_metadata_read_lock,
    load_metadata_async,
    ensure_metadata_loaded,
    load_metadata,
    get_or_create_metadata,
    register_agent,
)

# --- agent_lifecycle ---
from src.agent_lifecycle import (
    get_or_create_monitor,
    auto_archive_orphan_agents,
    get_agent_or_error,
    build_standardized_agent_info,
)

# --- agent_loop_detection ---
from src.agent_loop_detection import (
    detect_loop_pattern,
    process_update_authenticated,
    update_agent_auth,
    process_update_authenticated_async,
    _auto_initiate_dialectic_recovery,
)

# Re-export governance_monitor types used by consumers
from src.governance_monitor import UNITARESMonitor
from src.health_thresholds import HealthThresholds, HealthStatus
from src.state_locking import StateLockManager
from src.process_cleanup import ProcessManager
from src.pattern_analysis import analyze_agent_patterns
from src.runtime_config import get_thresholds
from src.lock_cleanup import cleanup_stale_state_locks

__all__ = [
    # Constants & config
    "project_root",
    "SERVER_VERSION", "SERVER_BUILD_DATE",
    "PID_FILE", "LOCK_FILE", "MAX_KEEP_PROCESSES", "CURRENT_PID",
    "AIOFILES_AVAILABLE", "PSUTIL_AVAILABLE",
    "METADATA_FILE", "UNITARES_METADATA_BACKEND", "UNITARES_METADATA_WRITE_JSON_SNAPSHOT",
    "SERVER_START_TIME", "EXPLORATION_CACHE_TTL",
    # Managers
    "lock_manager", "health_checker", "process_mgr",
    # Data model
    "AgentMetadata", "TextContent",
    # Shared state (mutable dicts)
    "agent_metadata", "monitors",
    # Functions
    "load_metadata_async", "ensure_metadata_loaded", "load_metadata",
    "get_or_create_metadata", "register_agent",
    "get_state_file",
    "save_monitor_state_async", "save_monitor_state", "load_monitor_state",
    "check_agent_status", "check_agent_id_default",
    "validate_agent_id_format", "require_explicit_agent_id",
    "generate_api_key", "verify_agent_ownership", "require_agent_auth",
    "cleanup_stale_processes", "write_pid_file", "remove_pid_file",
    "signal_handler", "init_server_process",
    "get_or_create_monitor",
    "auto_archive_orphan_agents",
    "get_agent_or_error", "build_standardized_agent_info",
    "detect_loop_pattern",
    "process_update_authenticated", "update_agent_auth",
    "process_update_authenticated_async",
    # Re-exported types
    "UNITARESMonitor", "HealthThresholds", "HealthStatus",
    "StateLockManager", "ProcessManager",
    "analyze_agent_patterns", "get_thresholds", "cleanup_stale_state_locks",
]
