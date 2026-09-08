"""Lifecycle — agent management, resume, stuck detection, self-recovery."""

from .handlers import (
    handle_list_agents,
    handle_get_agent_metadata,
    handle_update_agent_metadata,
    handle_archive_agent,
    handle_resume_agent,
    handle_delete_agent,
    handle_archive_old_test_agents,
    handle_archive_orphan_agents,
    handle_mark_response_complete,
    handle_self_recovery_review,
    handle_detect_stuck_agents,
    handle_ping_agent,
)
from .self_recovery import (
    handle_self_recovery,
    handle_quick_resume,
    handle_check_recovery_options,
    handle_operator_resume_agent,
)

__all__ = [
    "handle_list_agents",
    "handle_get_agent_metadata",
    "handle_update_agent_metadata",
    "handle_archive_agent",
    "handle_resume_agent",
    "handle_delete_agent",
    "handle_archive_old_test_agents",
    "handle_archive_orphan_agents",
    "handle_mark_response_complete",
    "handle_self_recovery_review",
    "handle_detect_stuck_agents",
    "handle_ping_agent",
    "handle_self_recovery",
    "handle_quick_resume",
    "handle_check_recovery_options",
    "handle_operator_resume_agent",
]
