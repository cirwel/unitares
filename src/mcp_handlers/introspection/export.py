"""
Export tool handlers.
"""

from typing import Dict, Any, Sequence
from mcp.types import TextContent
import os
import re
import stat
import json
from datetime import datetime
from ..utils import success_response, error_response, require_registered_agent
from ..decorators import mcp_tool
from src.governance_glossary import get_eisv_glossary
from src.logging_utils import get_logger
from src.mcp_handlers.shared import lazy_mcp_server as mcp_server

logger = get_logger(__name__)

_EXPORT_FORMATS = ("json", "csv")
# A bare file stem: no separators, no leading dot, so it cannot name a path
# outside the export directory (an absolute value makes os.path.join drop the
# base, and ".." walks out of it).
_EXPORT_STEM = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


def _invalid_export_stem(custom_filename: Any) -> bool:
    return (
        not isinstance(custom_filename, str)
        or _EXPORT_STEM.fullmatch(custom_filename) is None
        or ".." in custom_filename
    )


def _contained_export_path(export_dir: str, filename: str) -> str | None:
    """Resolve filename under export_dir, or None if it would land outside."""
    base = os.path.realpath(export_dir)
    file_path = os.path.realpath(os.path.join(base, filename))
    if os.path.dirname(file_path) != base:
        return None
    return file_path

@mcp_tool("get_system_history", timeout=20.0, register=False)
async def handle_get_system_history(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Export complete governance history for an agent"""
    # Try to get agent_id, but don't fail if not registered - use context agent_id
    from ..context import get_context_agent_id
    agent_id = arguments.get("agent_id")
    context_agent_id = get_context_agent_id()
    
    # Use context agent_id if no explicit agent_id provided
    if not agent_id and context_agent_id:
        agent_id = context_agent_id
    
    # If still no agent_id, try require_registered_agent for onboarding guidance
    if not agent_id:
        agent_id, error = require_registered_agent(arguments)
        if error:
            return [error]  # Returns onboarding guidance if not registered
    
    format_type = arguments.get("format", "json")
    
    # Load monitor state from disk if not in memory (consistent with get_governance_metrics)
    monitor = mcp_server.get_or_create_monitor(agent_id)
    from src.agent_monitor_state import ensure_hydrated
    await ensure_hydrated(monitor, agent_id)

    # Check if monitor has any history
    if not monitor.state.E_history and not monitor.state.timestamp_history:
        return success_response({
            "format": format_type,
            "history": [],
            "agent_id": agent_id,
            "eisv_labels": get_eisv_glossary(),
            "empty": True,
            "message": "No history available yet for this agent",
            "next_step": "Call sync_state() to generate history",
        })
    
    history_data = monitor.export_history(format=format_type)
    
    # If JSON format, parse it back to a dict to avoid double-encoding in success_response
    if format_type == "json":
        try:
            history_data = json.loads(history_data)
        except Exception as e:
            logger.warning(f"Could not parse history JSON: {e}")
            return [error_response(
                "Export produced malformed JSON",
                error_code="EXPORT_MALFORMED",
                error_category="system_error",
                details={"agent_id": agent_id, "cause": str(e)},
            )]

    return success_response({
        "format": format_type,
        "history": history_data,
        "agent_id": agent_id,
        "eisv_labels": get_eisv_glossary(),
    })

@mcp_tool("export_to_file", timeout=45.0, register=False)
async def handle_export_to_file(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Export governance history to a file in the server's data directory"""
    # PROACTIVE GATE: Require agent to be registered
    agent_id, error = require_registered_agent(arguments)
    if error:
        return [error]  # Returns onboarding guidance if not registered
    
    format_type = arguments.get("format", "json")
    custom_filename = arguments.get("filename")
    complete_package = arguments.get("complete_package", False)  # New: export all layers

    if format_type not in _EXPORT_FORMATS:
        return [error_response(
            f"Unsupported export format. Use one of: {', '.join(_EXPORT_FORMATS)}.",
            {"format": format_type},
        )]
    if custom_filename is not None and _invalid_export_stem(custom_filename):
        return [error_response(
            "filename must be a bare name (letters, digits, '_', '-', '.'; no "
            "path separators or '..'). Files are always written to the "
            "server's export directory.",
            {"filename": custom_filename},
        )]
    
    # Load monitor state from disk if not in memory (consistent with get_governance_metrics)
    monitor = mcp_server.get_or_create_monitor(agent_id)
    from src.agent_monitor_state import ensure_hydrated
    await ensure_hydrated(monitor, agent_id)

    if complete_package:
        # Export complete package: metadata + history + validation
        # NOTE: Knowledge layer removed November 28, 2025
        
        # Get metadata
        meta = mcp_server.agent_metadata.get(agent_id)
        metadata_dict = meta.to_dict() if meta else {}
        
        # Get history (parse JSON to dict)
        history_json = monitor.export_history(format="json")
        history_dict = json.loads(history_json)
        
        # Validation checks
        # Check if state exists (monitor is loaded, state file exists, or monitor has history)
        # Check if state exists (non-blocking)
        import asyncio
        loop = asyncio.get_running_loop()
        persisted_state = await loop.run_in_executor(None, mcp_server.load_monitor_state, agent_id)
        state_exists = monitor is not None and (
            len(monitor.state.V_history) > 0 or 
            persisted_state is not None
        )
        
        # Validate metadata consistency
        metadata_consistency_valid = True
        metadata_consistency_errors = []
        if meta:
            metadata_consistency_valid, metadata_consistency_errors = meta.validate_consistency()
        
        validation_checks = {
            "metadata_exists": meta is not None,
            "history_exists": state_exists,
            "metadata_history_sync": (
                meta.total_updates == len(history_dict.get("E_history", [])) 
                if meta and history_dict else False
            ),
            "metadata_consistency": metadata_consistency_valid,
            "metadata_consistency_errors": metadata_consistency_errors if metadata_consistency_errors else None
        }
        
        # Package everything
        package = {
            "agent_id": agent_id,
            "exported_at": datetime.now().isoformat(),
            "export_type": "complete_package",
            "layers": {
                "metadata": metadata_dict,
                "history": history_dict
            },
            "validation": validation_checks
        }
        
        # Determine filename
        if custom_filename:
            filename = f"{custom_filename}_complete.{format_type}"
        else:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{agent_id}_complete_package_{timestamp}.{format_type}"
        
        # Use data/exports/ for complete packages
        export_dir = os.path.join(mcp_server.project_root, "data", "exports")
        
        # Convert to requested format - wrap in executor to avoid blocking
        if format_type == "json":
            # JSON serialization is CPU-bound and can block for large packages
            import asyncio
            loop = asyncio.get_running_loop()
            export_data = await loop.run_in_executor(None, lambda: json.dumps(package, indent=2))
        else:
            # CSV not supported for complete package (too complex)
            return [error_response(
                "CSV format not supported for complete package export. Use 'json' format.",
                {"format": format_type, "complete_package": True}
            )]
    else:
        # Original behavior: export history only (backward compatible)
        history_data = monitor.export_history(format=format_type)
        export_data = history_data
        
        # Determine filename
        if custom_filename:
            filename = f"{custom_filename}.{format_type}"
        else:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{agent_id}_history_{timestamp}.{format_type}"
        
        # Use data/history/ for history-only exports
        export_dir = os.path.join(mcp_server.project_root, "data", "history")
    
    # Write file (non-blocking - run in executor)
    file_path = _contained_export_path(export_dir, filename)
    if file_path is None:
        return [error_response(
            "Export path resolves outside the export directory.",
            {"filename": filename},
        )]
    try:
        import asyncio
        loop = asyncio.get_running_loop()
        
        def _write_file_sync():
            """Synchronous file write function - runs in executor to avoid blocking event loop"""
            # Create directory if needed (inside executor to avoid blocking)
            os.makedirs(export_dir, exist_ok=True)

            # Open relative to the directory we validated, refusing symlinks
            # at both steps (a path check followed by open(path) leaves a
            # window to swap in a symlink), and truncate only after fstat
            # shows a plain file with a single link: O_TRUNC through a planted
            # hard link would rewrite a file outside this directory.
            dir_fd = os.open(
                os.path.dirname(file_path),
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
            try:
                fd = os.open(
                    os.path.basename(file_path),
                    os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW,
                    0o644,
                    dir_fd=dir_fd,
                )
            finally:
                os.close(dir_fd)
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                st = os.fstat(f.fileno())
                if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                    raise PermissionError("export target is not a plain, singly linked file")
                os.ftruncate(f.fileno(), 0)
                f.write(export_data)
                f.flush()  # Ensure buffered data written
                os.fsync(f.fileno())  # Ensure written to disk
                return os.fstat(f.fileno()).st_size
        
        # Run file I/O in executor to avoid blocking event loop
        file_size = await loop.run_in_executor(None, _write_file_sync)
        
        return success_response({
            "message": "Complete package exported successfully" if complete_package else "History exported successfully",
            "file_path": file_path,
            "filename": filename,
            "format": format_type,
            "agent_id": agent_id,
            "file_size_bytes": file_size,
            "complete_package": complete_package,
            "layers_included": ["metadata", "history", "validation"] if complete_package else ["history"]
        })
    except Exception as e:
        return [error_response(f"Failed to write file: {str(e)}", {"file_path": str(file_path)})]
