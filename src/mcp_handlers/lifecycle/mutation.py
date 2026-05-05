"""
Lifecycle mutation handlers — write operations for agent metadata, archiving, and deletion.

Extracted from handlers.py for maintainability.
"""

from typing import Dict, Any, Sequence
from mcp.types import TextContent
from datetime import datetime, timezone

from src import agent_storage
from src.mcp_handlers.shared import lazy_mcp_server as mcp_server
from ..utils import (
    require_registered_agent,
    success_response,
    error_response,
)
from ..error_helpers import (
    agent_not_found_error,
    ownership_error,
)
from ..decorators import mcp_tool
from ..support.coerce import resolve_agent_uuid
from src.logging_utils import get_logger

from .helpers import _invalidate_agent_cache

logger = get_logger(__name__)


@mcp_tool("update_agent_metadata", timeout=10.0, register=False)
async def handle_update_agent_metadata(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Update agent tags and notes

    SECURITY: Requires API key authentication and ownership verification.
    Agents can only update their own metadata.
    """
    # === KWARGS STRING UNWRAPPING ===
    if arguments and "kwargs" in arguments and isinstance(arguments["kwargs"], str):
        try:
            import json
            kwargs_parsed = json.loads(arguments["kwargs"])
            if isinstance(kwargs_parsed, dict):
                del arguments["kwargs"]
                arguments.update(kwargs_parsed)
        except (json.JSONDecodeError, TypeError):
            pass

    # Check write permission (bound=true required for writes)
    from ..identity.shared import require_write_permission
    allowed, write_error = require_write_permission(arguments=arguments)
    if not allowed:
        return [write_error]

    # SECURITY FIX: Require registered agent_id (prevents phantom agent_ids)
    agent_id, error = require_registered_agent(arguments)
    if error:
        return [error]

    # Wave 2 audit: force=True dropped per PR #350 precedent. Pre-mutation
    # existence check; in-memory cache is fresh enough.
    await mcp_server.load_metadata_async()

    if agent_id not in mcp_server.agent_metadata:
        return agent_not_found_error(agent_id)

    meta = mcp_server.agent_metadata[agent_id]

    # SECURITY: Verify ownership via session binding (UUID-based auth, Dec 2025)
    from ..utils import verify_agent_ownership
    from ..identity.shared import get_bound_agent_id
    if not verify_agent_ownership(agent_id, arguments):
        caller_id = get_bound_agent_id(arguments) or "unknown"
        return ownership_error(
            resource_type="agent_metadata",
            resource_id=agent_id,
            owner_agent_id=agent_id,
            caller_agent_id=caller_id,
        )

    # Update status if provided (reactivation from archived)
    if "status" in arguments:
        new_status = arguments["status"]
        if new_status != "active":
            return [error_response(
                f"Only status='active' is supported (to reactivate archived agents). Got '{new_status}'.",
                error_code="INVALID_STATUS_TRANSITION",
            )]
        if getattr(meta, "status", None) != "archived":
            return [error_response(
                f"Agent is already '{getattr(meta, 'status', 'unknown')}', no status change needed.",
                error_code="INVALID_STATUS_TRANSITION",
            )]
        meta.status = "active"
        meta.archived_at = None

    # Update tags if provided
    if "tags" in arguments:
        meta.tags = arguments["tags"]

    # Update notes if provided
    if "notes" in arguments:
        append_notes = arguments.get("append_notes", False)
        if append_notes:
            timestamp = datetime.now(timezone.utc).isoformat()
            meta.notes = f"{meta.notes}\n[{timestamp}] {arguments['notes']}" if meta.notes else f"[{timestamp}] {arguments['notes']}"
        else:
            meta.notes = arguments["notes"]

    # Update purpose if provided
    if "purpose" in arguments:
        purpose = arguments.get("purpose")
        if purpose is None:
            # Allow explicit null to clear purpose
            meta.purpose = None
        elif isinstance(purpose, str):
            purpose_str = purpose.strip()
            meta.purpose = purpose_str if purpose_str else None

    # Update preferences if provided (v2.5.0+)
    if "preferences" in arguments:
        prefs = arguments.get("preferences")
        if prefs is None:
            meta.preferences = None
        elif isinstance(prefs, dict):
            # Validate verbosity if present
            if "verbosity" in prefs:
                valid_verbosity = {"minimal", "compact", "standard", "full", "auto"}
                if prefs["verbosity"] not in valid_verbosity:
                    return [error_response(
                        f"Invalid verbosity '{prefs['verbosity']}'. Valid options: {', '.join(valid_verbosity)}",
                        error_code="INVALID_PREFERENCE"
                    )]
            meta.preferences = prefs

    # PostgreSQL: Update metadata (single source of truth)
    try:
        await agent_storage.update_agent(
            agent_id=agent_id,
            status=getattr(meta, "status", None),
            tags=meta.tags,
            notes=meta.notes,
            purpose=getattr(meta, "purpose", None),
            parent_agent_id=getattr(meta, "parent_agent_id", None),
            spawn_reason=getattr(meta, "spawn_reason", None),
        )
        logger.debug("PostgreSQL: Updated metadata")

        await _invalidate_agent_cache(agent_id)
    except Exception as e:
        logger.warning(f"PostgreSQL update_agent failed: {e}", exc_info=True)

    return success_response({
        "success": True,
        "message": "Agent metadata updated",
        "agent_id": agent_id,
        "tags": meta.tags,
        "notes": meta.notes,
        "purpose": getattr(meta, "purpose", None),
        "preferences": getattr(meta, "preferences", None),
        "updated_at": datetime.now(timezone.utc).isoformat()
    })

@mcp_tool("archive_agent", timeout=15.0, register=False)
async def handle_archive_agent(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Archive an agent for long-term storage.

    No ownership check -- dashboard and operator agents need to archive
    other agents. HTTP Bearer token auth is sufficient for admin actions.
    Mirrors handle_resume_agent pattern.
    """
    # SECURITY FIX: Require registered agent_id (prevents phantom agent_ids)
    agent_id, error = require_registered_agent(arguments)
    if error:
        return [error]

    # Use authoritative UUID for internal lookups (agent_id might be a label)
    # require_registered_agent sets this after validating registration
    agent_uuid = resolve_agent_uuid(arguments, agent_id)

    # Wave 2 audit: force=True dropped per PR #350 precedent. Pre-mutation
    # existence check; in-memory cache is fresh enough.
    await mcp_server.load_metadata_async()

    if agent_uuid not in mcp_server.agent_metadata:
        return agent_not_found_error(agent_id)

    meta = mcp_server.agent_metadata[agent_uuid]

    if meta.status == "archived":
        return [error_response(
            f"Agent '{agent_id}' is already archived",
            error_code="AGENT_ALREADY_ARCHIVED",
            error_category="validation_error",
            details={"error_type": "agent_already_archived", "agent_id": agent_id, "status": meta.status},
            recovery={
                "action": "Agent is already archived",
                "related_tools": ["get_agent_metadata", "list_agents"],
                "workflow": ["1. Check agent status with get_agent_metadata", "2. Archived agents cannot be archived again"]
            }
        )]

    reason = arguments.get("reason", "Manual archive")
    keep_in_memory = arguments.get("keep_in_memory", False)

    # Stamp notes with the sticky-archive marker so phases.py auto-resume
    # gate refuses to resurrect this identity on a later process_agent_update.
    # Must persist via update_agent() — an in-memory-only mutation would be
    # clobbered on the next load_metadata_async reload (P011). Orphan-sweep
    # archives go through a different code path and are intentionally NOT
    # stamped — they remain non-sticky so legitimate residents falsely
    # sweeped can recover after the cooldown window expires.
    existing_notes = (getattr(meta, "notes", "") or "").strip()
    if "user requested" not in existing_notes.lower():
        marker = f"user requested archive: {reason}"
        new_notes = f"{existing_notes}\n{marker}".strip() if existing_notes else marker
        try:
            await agent_storage.update_agent(agent_uuid, notes=new_notes)
            # Mirror in-memory ONLY on successful persist — otherwise the next
            # load_metadata_async reload would clobber a divergent meta.notes
            # and the marker would silently vanish (P011).
            meta.notes = new_notes
        except Exception as e:
            logger.warning(
                "Could not persist sticky-archive marker: %s. "
                "Cooldown window still protects against immediate resurrection.",
                type(e).__name__,
            )

    # Persist-first: write to Postgres before mutating in-memory state
    from .helpers import _archive_one_agent
    monitors = None if keep_in_memory else mcp_server.monitors
    ok = await _archive_one_agent(agent_uuid, meta, reason, monitors=monitors)
    if not ok:
        return [error_response(
            f"Failed to persist archival for '{agent_id}' — database write failed",
            error_code="ARCHIVE_PERSIST_FAILED",
        )]

    await _invalidate_agent_cache(agent_id)

    # Audit trail for archive operation
    try:
        from src.audit_log import audit_logger, AuditEntry
        from ..identity.shared import get_bound_agent_id
        caller_id = get_bound_agent_id(arguments) or "unknown"
        audit_logger._write_entry(AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            agent_id=agent_id,
            event_type="agent_archived",
            confidence=1.0,
            details={"reason": reason, "caller": caller_id, "keep_in_memory": keep_in_memory},
        ))
    except Exception:
        pass  # Audit logging is best-effort

    return success_response({
        "success": True,
        "message": f"Agent '{agent_id}' archived successfully",
        "agent_id": agent_id,
        "lifecycle_status": "archived",
        "archived_at": meta.archived_at,
        "reason": reason,
        "kept_in_memory": keep_in_memory
    })

@mcp_tool("delete_agent", timeout=15.0, register=False)
async def handle_delete_agent(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Delete agent and archive data (protected: cannot delete pioneer agents).

    No ownership check -- dashboard and operator agents need to manage
    other agents. HTTP Bearer token auth is sufficient for admin actions.
    Still requires confirm=true and pioneer protection.
    """
    # SECURITY FIX: Require registered agent_id (prevents phantom agent_ids)
    agent_id, error = require_registered_agent(arguments)
    if error:
        return [error]

    confirm = arguments.get("confirm", False)
    if not confirm:
        return [error_response("Deletion requires explicit confirmation (confirm=true)")]

    # Use authoritative UUID for internal lookups
    agent_uuid = resolve_agent_uuid(arguments, agent_id)

    # Wave 2 audit: force=True dropped per PR #350 precedent. Pre-mutation
    # existence check; in-memory cache is fresh enough.
    await mcp_server.load_metadata_async()

    if agent_uuid not in mcp_server.agent_metadata:
        return agent_not_found_error(agent_id)

    meta = mcp_server.agent_metadata[agent_uuid]

    # Check if agent is a pioneer (protected)
    if "pioneer" in meta.tags:
        return [error_response(
            f"Cannot delete pioneer agent '{agent_id}'",
            recovery={
                "action": "Pioneer agents are protected from deletion. Use archive_agent instead.",
                "related_tools": ["archive_agent"],
                "workflow": ["1. Call archive_agent to archive instead of delete", "2. Pioneer agents preserve system history"]
            }
        )]

    backup_first = arguments.get("backup_first", True)

    # Backup if requested
    backup_path = None
    if backup_first:
        try:
            import json
            import asyncio
            from pathlib import Path
            backup_dir = Path(mcp_server.project_root) / "data" / "archives"
            backup_file = backup_dir / f"{agent_id}_backup_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
            backup_data = {
                "agent_id": agent_id,
                "metadata": meta.to_dict(),
                "backed_up_at": datetime.now(timezone.utc).isoformat()
            }

            # Write backup file in executor to avoid blocking event loop
            loop = asyncio.get_running_loop()
            def _write_backup_sync():
                """Synchronous backup write - runs in executor"""
                # Create directory if needed (inside executor to avoid blocking)
                backup_dir.mkdir(parents=True, exist_ok=True)

                # Write backup file
                with open(backup_file, 'w', encoding='utf-8') as f:
                    json.dump(backup_data, f, indent=2)

            await loop.run_in_executor(None, _write_backup_sync)
            backup_path = str(backup_file)
        except Exception as e:
            logger.warning(f"Could not backup agent before deletion: {e}")

    # Delete agent
    meta.status = "deleted"
    meta.add_lifecycle_event("deleted", "Manual deletion")

    # Remove from monitors
    if agent_id in mcp_server.monitors:
        del mcp_server.monitors[agent_id]

    # PostgreSQL: Delete agent (single source of truth)
    try:
        await agent_storage.delete_agent(agent_id)
        logger.debug("PostgreSQL: Deleted agent")

        await _invalidate_agent_cache(agent_id)
    except Exception as e:
        logger.warning(f"PostgreSQL delete_agent failed: {e}", exc_info=True)

    # Audit trail for delete operation
    try:
        from src.audit_log import audit_logger, AuditEntry
        from ..identity.shared import get_bound_agent_id
        caller_id = get_bound_agent_id(arguments) or "unknown"
        audit_logger._write_entry(AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            agent_id=agent_id,
            event_type="agent_deleted",
            confidence=1.0,
            details={"caller": caller_id, "backup_path": backup_path},
        ))
    except Exception:
        pass  # Audit logging is best-effort

    return success_response({
        "success": True,
        "message": f"Agent '{agent_id}' deleted successfully",
        "agent_id": agent_id,
        "archived": backup_path is not None,
        "backup_path": backup_path
    })
