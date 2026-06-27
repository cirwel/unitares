"""Shared response builders for dialectic handlers.

Keep user-facing response text in one place so submit handlers stay consistent
as flows evolve.
"""

from __future__ import annotations

from typing import Any, Dict, List


def missing_session_id_recovery() -> Dict[str, Any]:
    """Recovery payload for submit handlers missing `session_id`."""
    return {
        "action": "Provide session_id",
        "related_tools": ["dialectic", "identity"],
        "note": (
            "Use dialectic(action='list') to find a session or "
            "dialectic(action='get', session_id='...') to inspect one."
        ),
    }


def session_not_found_recovery() -> Dict[str, Any]:
    """Recovery payload when a dialectic session cannot be loaded."""
    return {
        "action": "Session may have expired or been resolved",
        "related_tools": ["dialectic"],
        "note": (
            "Use dialectic(action='list') to browse sessions or "
            "dialectic(action='request', ...) to open a new review."
        ),
    }


def get_session_timeout_recovery(timeout_reason: str) -> Dict[str, Any]:
    """Recovery payload for timed-out dialectic sessions."""
    return {
        "action": "Session timed out - automatic resolution",
        "what_happened": timeout_reason,
        "what_you_can_do": [
            "1. Check your state with get_governance_metrics",
            "2. Use self_recovery(action='quick') if you believe you can proceed safely",
            "3. Leave a note about what happened with knowledge(action='note', summary='...')",
        ],
        "related_tools": ["get_governance_metrics", "self_recovery", "knowledge"],
        "note": "Session is no longer active. Inspect the transcript and current state before retrying.",
    }


def get_reviewer_stuck_recovery(reviewer_agent_id: str | None) -> Dict[str, Any]:
    """Recovery payload when reviewer fails to respond in time."""
    return {
        "action": "Session aborted because reviewer didn't respond within timeout",
        "what_happened": (
            f"Reviewer '{reviewer_agent_id}' was assigned but didn't submit antithesis within 2 hours"
        ),
        "what_you_can_do": [
            "1. Check your state with get_governance_metrics",
            "2. Use self_recovery(action='quick') if you believe you can proceed safely",
            "3. Leave a note about what happened with knowledge(action='note', summary='...')",
        ],
        "related_tools": ["get_governance_metrics", "self_recovery", "knowledge"],
        "note": "The session is no longer active. Start a new review only if the issue still stands.",
    }


def get_reviewer_reassigned_recovery(
    old_reviewer_id: str | None,
    new_reviewer_id: str,
    *,
    reason: str | None = None,
) -> Dict[str, Any]:
    """Recovery payload after successful reviewer reassignment."""
    if old_reviewer_id:
        happened = f"Reviewer changed from '{old_reviewer_id}' to '{new_reviewer_id}'."
    else:
        happened = f"Reviewer '{new_reviewer_id}' assigned."
    if reason:
        happened = f"{happened} Reason: {reason}"
    return {
        "action": "Reviewer reassigned - session continues",
        "what_happened": happened,
        "next_steps": [
            f"New reviewer '{new_reviewer_id}' should submit antithesis",
            "Session phase and transcript are preserved",
        ],
        "related_tools": ["dialectic"],
    }


def get_awaiting_facilitation_recovery(session_id: str) -> Dict[str, Any]:
    """Recovery payload when no auto-reviewer is available and human facilitation is needed."""
    return {
        "action": "Human facilitation required - no eligible reviewer found",
        "what_happened": (
            "Reviewer went stale and no eligible replacement agent is available for auto-assignment."
        ),
        "what_you_can_do": [
            f"1. Use dialectic(action='reassign', session_id='{session_id}', new_reviewer_id='<agent_id>') to assign a reviewer manually",
            "2. Use agent(action='list') to find available agents",
            "3. Or let your bound session answer directly with dialectic(action='antithesis', session_id='...', reasoning='...', take_over_if_requested=true)",
        ],
        "related_tools": ["dialectic", "agent", "identity"],
        "note": "Session is paused, not failed. It will auto-fail after 4 hours total if no reviewer is assigned.",
    }


def get_agent_not_found_recovery() -> Dict[str, Any]:
    """Recovery payload when querying sessions for an unknown agent."""
    return {
        "action": "Agent must be registered first",
        "related_tools": ["identity", "agent"],
    }


def no_sessions_found_recovery() -> Dict[str, Any]:
    """Recovery payload when an agent has no dialectic sessions."""
    return {
        "action": "No dialectic sessions found for that agent.",
        "related_tools": ["get_governance_metrics", "search_knowledge_graph"],
        "note": "Use get_governance_metrics for live state and request_dialectic_review only when a review is actually needed.",
    }


def missing_session_or_agent_recovery() -> Dict[str, Any]:
    """Recovery payload when neither session_id nor agent_id is provided."""
    return {
        "action": "Provide session_id or agent_id to inspect a dialectic session",
        "related_tools": ["agent", "get_governance_metrics"],
        "note": "Use get_governance_metrics for live state when you are not targeting a dialectic session.",
    }


def get_session_exception_recovery() -> Dict[str, Any]:
    """Recovery payload for unexpected get_dialectic_session errors."""
    return {
        "action": "Check session_id or agent_id and try again",
        "related_tools": ["agent", "get_governance_metrics"],
    }


def llm_unavailable_recovery() -> Dict[str, Any]:
    """Recovery payload when local Ollama is not available."""
    return {
        "action": "Start Ollama: `ollama serve` or use dialectic(action='request') for peer review",
        "related_tools": ["dialectic", "health_check"],
        "workflow": [
            "1. Check Ollama: curl http://localhost:11434/api/tags",
            "2. Start if needed: ollama serve",
            "3. Retry this tool",
        ],
    }


def llm_missing_root_cause_recovery() -> Dict[str, Any]:
    """Recovery payload when root_cause is missing for llm_assisted_dialectic."""
    return {
        "action": "Provide root_cause: your understanding of what went wrong",
        "example": {
            "root_cause": "High complexity task without sufficient planning",
            "proposed_conditions": ["Reduce task complexity", "Add progress checkpoints"],
            "reasoning": "The task scope exceeded my capacity to maintain coherence",
        },
    }


def llm_failed_recovery() -> Dict[str, Any]:
    """Recovery payload when dialectic LLM call returns no result."""
    return {
        "action": "Check Ollama status and retry",
        "related_tools": ["health_check", "call_model"],
    }


def llm_incomplete_recovery(partial_result: Dict[str, Any]) -> Dict[str, Any]:
    """Recovery payload when dialectic completes with an unsuccessful result."""
    return {
        "action": "Review partial result and retry with clearer thesis",
        "partial_result": partial_result,
    }


def next_step_submit_antithesis(reviewer_agent_id: str | None) -> str:
    """Next-step guidance after successful thesis submission."""
    if reviewer_agent_id:
        return f"Reviewer '{reviewer_agent_id}' should submit antithesis"
    return "An eligible reviewer should claim the session by submitting antithesis"


def next_step_negotiate_synthesis() -> str:
    """Next-step guidance after successful antithesis submission."""
    return "Both agents should negotiate via submit_synthesis() until convergence"


def next_step_resumed() -> str:
    """Guidance when resolution execution resumed the paused agent."""
    return "Agent resumed successfully with agreed conditions"


def next_step_resume_not_applied(warning: str | None) -> str:
    """Guidance when synthesis converges but no resume transition executes."""
    detail = warning or "No lifecycle transition was applied."
    return f"Resolution recorded, but no resume action applied: {detail}"


def next_step_execution_failed(error: Exception) -> str:
    """Guidance when resolution execution raises an exception."""
    return f"Failed to execute resolution: {error}"


def next_step_no_consensus() -> str:
    """Guidance when synthesis reaches conservative no-consensus path."""
    return "Peers could not reach consensus. Maintaining current state."


def default_resume_steps() -> List[str]:
    return [
        "You can resume work with the agreed conditions",
        "Call process_agent_update() to log your next action",
        "Monitor your coherence with get_governance_metrics()",
    ]


def default_cooldown_steps() -> List[str]:
    return [
        "Take a brief pause before resuming",
        "Review the synthesis reasoning",
        "When ready, call process_agent_update() with lower complexity",
    ]


def default_escalate_steps() -> List[str]:
    return [
        "The dialectic suggests human review may be needed",
        "Consider simplifying your approach",
        "Use dialectic(action='request', ...) for peer review if available",
    ]
