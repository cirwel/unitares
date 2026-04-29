"""
Shared test factories and utilities for governance-mcp tests.

Centralizes duplicated helpers (_parse, make_agent_meta, make_mock_server,
make_monitor) that were previously copy-pasted across 15+ test files.

Usage:
    from tests.helpers import parse_result, make_agent_meta, make_mock_server
    from tests.helpers import make_monitor, patch_lifecycle_server
"""

import json
import sys
from contextlib import contextmanager, asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock, AsyncMock

project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))


# ============================================================================
# Result parsing
# ============================================================================

def parse_result(result):
    """Parse MCP handler result (TextContent or list[TextContent]) into a dict.

    Handles both:
    - Sequence[TextContent] from success_response()
    - Bare TextContent from some error_response() calls
    """
    from mcp.types import TextContent

    if isinstance(result, TextContent):
        return json.loads(result.text)
    if isinstance(result, (list, tuple)):
        return json.loads(result[0].text)
    return json.loads(result.text)


# ============================================================================
# Agent metadata factory
# ============================================================================

def make_agent_meta(
    status="active",
    label=None,
    display_name=None,
    public_agent_id=None,
    purpose=None,
    total_updates=5,
    last_update=None,
    created_at=None,
    tags=None,
    notes="",
    trust_tier=None,
    preferences=None,
    parent_agent_id=None,
    spawn_reason=None,
    health_status=None,
    paused_at=None,
    structured_id=None,
    api_key=None,
    **kwargs,
):
    """Create a mock AgentMetadata SimpleNamespace.

    Superset of all variants previously scattered across test files.
    All parameters are optional with sensible defaults.
    """
    if last_update is None:
        last_update = datetime.now(timezone.utc).isoformat()
    if created_at is None:
        created_at = datetime.now(timezone.utc).isoformat()
    meta = SimpleNamespace(
        status=status,
        label=label,
        display_name=display_name,
        purpose=purpose,
        total_updates=total_updates,
        last_update=last_update,
        created_at=created_at,
        tags=tags or [],
        notes=notes,
        trust_tier=trust_tier,
        archived_at=None,
        lifecycle_events=[],
        preferences=preferences,
        parent_agent_id=parent_agent_id,
        spawn_reason=spawn_reason,
        health_status=health_status,
        paused_at=paused_at,
        public_agent_id=public_agent_id,
        structured_id=structured_id,
        api_key=api_key,
        last_response_at=None,
        response_completed=False,
        **kwargs,
    )
    meta.add_lifecycle_event = MagicMock()
    meta.to_dict = MagicMock(return_value={
        "status": status, "label": label, "tags": tags or [],
        "notes": notes, "purpose": purpose, "total_updates": total_updates,
        "last_update": last_update, "created_at": created_at,
    })
    return meta


# ============================================================================
# Mock MCP server factory
# ============================================================================

def make_mock_server(**overrides):
    """Create a mock MCP server with all commonly-used attributes.

    Superset of all variants. Unused mock attributes are harmless —
    simpler test files just ignore the extra fields.
    """
    server = MagicMock()
    server.agent_metadata = overrides.get("agent_metadata", {})
    server.monitors = overrides.get("monitors", {})
    server.load_metadata = MagicMock()
    server.load_metadata_async = AsyncMock()
    server.get_or_create_monitor = MagicMock()
    server.get_or_create_metadata = MagicMock()
    server.SERVER_VERSION = overrides.get("SERVER_VERSION", "test-1.0.0")
    server.project_root = overrides.get("project_root", str(project_root))
    server._metadata_cache_state = {"last_load_time": 0}
    server.load_monitor_state = MagicMock(return_value=None)

    # Lock manager with async context manager
    lock_mgr = MagicMock()

    @asynccontextmanager
    async def _fake_lock(*args, **kwargs):
        yield

    lock_mgr.acquire_agent_lock_async = MagicMock(side_effect=_fake_lock)
    server.lock_manager = lock_mgr

    # process_update_authenticated_async
    server.process_update_authenticated_async = AsyncMock(return_value={
        "status": "ok",
        "decision": {"action": "approve", "confidence": 0.8},
        "metrics": {
            "E": 0.7, "I": 0.6, "S": 0.2, "V": 0.0,
            "coherence": 0.52, "risk_score": 0.3,
            "verdict": "continue",
            "regime": "EXPLORATION",
            "phi": 0.0,
        },
        "guidance": "Continue current approach.",
    })

    # health_checker
    try:
        from src.health_thresholds import HealthStatus
        health_checker = MagicMock()
        health_checker.get_health_status.return_value = (HealthStatus.HEALTHY, "System healthy")
        server.health_checker = health_checker
    except ImportError:
        pass

    # process_mgr
    server.process_mgr = MagicMock()
    server.process_mgr.write_heartbeat = MagicMock()

    # check_agent_id_default
    server.check_agent_id_default = MagicMock(return_value=None)

    return server


# ============================================================================
# Monitor factory
# ============================================================================

def make_monitor(
    coherence=0.52,
    void_active=False,
    E=0.7,
    I=0.6,
    S=0.2,
    V=0.0,
    lambda1=0.1,
    regime="EXPLORATION",
    regime_duration=1,
    risk_history=None,
    coherence_history=None,
    E_history=None,
    timestamp_history=None,
    V_history=None,
    mean_risk=0.3,
    unitaires_state=None,
    unitaires_theta=None,
):
    """Create a mock UNITARESMonitor with realistic state.

    Superset of all _make_monitor variants across test files.
    All parameters optional with sensible defaults.
    """
    state = SimpleNamespace(
        coherence=coherence,
        void_active=void_active,
        E=E,
        I=I,
        S=S,
        V=V,
        lambda1=lambda1,
        regime=regime,
        regime_duration=regime_duration,
        risk_history=risk_history or [],
        coherence_history=coherence_history or [],
        E_history=E_history or [],
        timestamp_history=timestamp_history or [],
        V_history=V_history or [],
        interpret_state=MagicMock(return_value={
            "health": "healthy",
            "mode": "convergent",
            "basin": "stable",
        }),
        unitaires_state=unitaires_state,
        unitaires_theta=unitaires_theta,
    )
    m = MagicMock()
    m.state = state
    m.get_metrics.return_value = {
        "E": E, "I": I, "S": S, "V": V,
        "coherence": coherence, "risk_score": mean_risk,
        "mean_risk": mean_risk,
        "initialized": True, "status": "ok",
        "complexity": 0.5,
    }
    m.simulate_update.return_value = {
        "status": "ok",
        "decision": {"action": "approve", "confidence": 0.8},
        "metrics": {
            "E": E, "I": I, "S": S, "V": V,
            "coherence": coherence, "risk_score": mean_risk,
        },
        "guidance": "Continue current approach.",
    }
    m.export_history.return_value = json.dumps({
        "E_history": [0.7, 0.75],
        "I_history": [0.6, 0.65],
        "S_history": [0.2, 0.15],
        "V_history": [0.0, 0.0],
    })
    return m


# ============================================================================
# Lifecycle patch context manager
# ============================================================================

# All lifecycle submodules that import mcp_server
_LIFECYCLE_MODULES = [
    "src.mcp_handlers.lifecycle.handlers",
    "src.mcp_handlers.lifecycle.query",
    "src.mcp_handlers.lifecycle.mutation",
    "src.mcp_handlers.lifecycle.operations",
    "src.mcp_handlers.lifecycle.stuck",
    "src.mcp_handlers.lifecycle.resume",
    "src.mcp_handlers.lifecycle.self_recovery",
]


@contextmanager
def patch_agent_storage():
    """Patch agent_storage across all lifecycle submodules with a shared mock.

    The lifecycle submodules ``handlers``, ``mutation``, and ``operations`` each
    import ``agent_storage`` at module load time, so a bare
    ``patch("...handlers.agent_storage")`` leaves the other two modules pointing
    at the real object. Tests previously worked around this with manual
    ``_lm.agent_storage = mock_storage`` rebinds, but ``patch()`` does not track
    those and never restores them — leaving live AsyncMock references in
    later-loaded test modules and producing "coroutine was never awaited"
    warnings (bug 2026-04-10T06:27:12.501426).

    This helper installs a single shared ``MagicMock`` at all three call sites
    via ``patch()``, which is tracked and restored on exit.

    Usage::

        with patch_agent_storage() as mock_storage:
            mock_storage.update_agent = AsyncMock()
            ...
    """
    shared = MagicMock()
    with patch("src.mcp_handlers.lifecycle.handlers.agent_storage", shared), \
         patch("src.mcp_handlers.lifecycle.mutation.agent_storage", shared), \
         patch("src.mcp_handlers.lifecycle.operations.agent_storage", shared), \
         patch("src.mcp_handlers.lifecycle.self_recovery.agent_storage", shared), \
         patch("src.mcp_handlers.lifecycle.helpers.agent_storage", shared):
        yield shared


@contextmanager
def patch_lifecycle_server(server, require_registered=None, **extra_patches):
    """Patch all lifecycle submodule mcp_server references in one call.

    Replaces the 6-11 line ``with patch(...)`` blocks that are repeated
    across 120+ test methods in lifecycle test files.

    Args:
        server: The mock MCP server to inject.
        require_registered: If set, also patches require_registered_agent
            across lifecycle submodules. Pass a tuple like ("agent-1", None).
        **extra_patches: Additional patch targets as {dotted.path: value}.

    Usage::

        with patch_lifecycle_server(server):
            result = await handle_list_agents({"lite": True})

        with patch_lifecycle_server(server, require_registered=("agent-1", None)):
            result = await handle_direct_resume_if_safe({"agent_id": "agent-1"})
    """
    from contextlib import ExitStack

    with ExitStack() as stack:
        # Patch mcp_server in all lifecycle submodules
        for mod in _LIFECYCLE_MODULES:
            stack.enter_context(patch(f"{mod}.mcp_server", server))

        # Optionally patch require_registered_agent
        if require_registered is not None:
            for mod in _LIFECYCLE_MODULES:
                try:
                    stack.enter_context(
                        patch(f"{mod}.require_registered_agent",
                              return_value=require_registered)
                    )
                except AttributeError:
                    pass  # Not all submodules import require_registered_agent

        # Apply any extra patches
        for target, value in extra_patches.items():
            stack.enter_context(patch(target, value))

        yield
