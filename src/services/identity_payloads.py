"""Transport-neutral payload builders for identity-related tools."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional


def build_identity_response_data(
    *,
    agent_uuid: str,
    agent_id: str,
    display_name: Optional[str],
    client_session_id: str,
    continuity_source: Optional[str],
    continuity_support: Dict[str, Any],
    continuity_token: Optional[str],
    identity_status: str,
    model_type: Optional[str],
    resumed: Optional[bool],
    session_continuity: Optional[Dict[str, Any]],
    verbose: bool,
    identity_resolution_outcome: Optional[str] = None,
    provisional_lineage: bool = False,
    lineage_state: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the identity() response payload."""
    response_data = {
        "uuid": agent_uuid,
        "agent_id": agent_id,
        "display_name": display_name,
        "client_session_id": client_session_id,
        "session_resolution_source": continuity_source,
        "continuity_token_supported": continuity_support.get("enabled", False),
        "identity_status": identity_status,
        "bound_identity": {
            "uuid": agent_uuid,
            "agent_id": agent_id,
            "display_name": display_name,
        },
    }
    if identity_resolution_outcome:
        response_data["identity_resolution_outcome"] = identity_resolution_outcome
    # R2 PR 3: surface persisted lineage flag at top level so callers
    # don't need a follow-up read to detect provisional edges. See
    # docs/ontology/r2-honest-memory-integration.md.
    response_data["provisional_lineage"] = bool(provisional_lineage)
    # R2 PR 3 council fix: surface response-facing lineage_state when
    # the caller has derived it (slow paths). Fast paths default to
    # None (field omitted) — surfacing a stale value would be worse
    # than absence.
    if lineage_state is not None:
        response_data["lineage_state"] = lineage_state
    # S1-a: surface ownership_proof_version at top level (see build_onboard_response_data).
    if continuity_support.get("ownership_proof_version") is not None:
        response_data["ownership_proof_version"] = continuity_support["ownership_proof_version"]
    if model_type:
        response_data["model_type"] = model_type
    if continuity_token:
        response_data["continuity_token"] = continuity_token
    if resumed is not None:
        response_data["resumed"] = resumed

    if verbose:
        # Doctrine: `display_name` is cosmetic (name-claim resolution removed
        # 2026-04-17). KG queries key on `agent_id`; canonical identity is
        # `agent_uuid`. Do not fall back to display_name for any functional key.
        response_data["quick_reference"] = {
            "for_knowledge_graph": agent_id,
            "for_session_continuity": client_session_id,
            "for_internal_lookup": agent_uuid,
            "to_set_display_name": "identity(name='YourName')",
        }
        if continuity_token:
            response_data["quick_reference"]["for_strong_resume"] = continuity_token

        if session_continuity:
            response_data["session_continuity"] = dict(session_continuity)
        else:
            response_data["session_continuity"] = {
                "client_session_id": client_session_id,
                "instruction": "Your session is auto-bound. You only need client_session_id if tools don't recognize you.",
            }
            if continuity_token:
                response_data["session_continuity"]["continuity_token"] = continuity_token
                response_data["session_continuity"]["instruction"] = (
                    "Prefer continuity_token for robust resume. "
                    "Use client_session_id when token support is unavailable."
                )
        response_data["session_continuity"]["resolution_source"] = continuity_source
        response_data["session_continuity"]["token_support"] = continuity_support

    return response_data


def build_identity_diag_payload(
    *,
    agent_uuid: str,
    agent_id: str,
    display_name: Optional[str],
    client_session_id: str,
    continuity_source: Optional[str],
    continuity_support: Dict[str, Any],
    continuity_token: Optional[str],
    identity_status: str,
    identity_resolution_outcome: Optional[str] = None,
    provisional_lineage: bool = False,
    lineage_state: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the lightweight identity diagnostic payload used by fast-return paths."""
    payload = {
        "uuid": agent_uuid,
        "agent_id": agent_id,
        "display_name": display_name,
        "client_session_id": client_session_id,
        "session_resolution_source": continuity_source,
        "continuity_token_supported": continuity_support.get("enabled", False),
        "identity_status": identity_status,
        "bound_identity": {
            "uuid": agent_uuid,
            "agent_id": agent_id,
            "display_name": display_name,
        },
    }
    if identity_resolution_outcome:
        payload["identity_resolution_outcome"] = identity_resolution_outcome
    # R2 PR 3: persisted-lineage flag (see build_identity_response_data).
    payload["provisional_lineage"] = bool(provisional_lineage)
    # R2 PR 3 council fix: surface lineage_state on diag payloads when
    # the caller derived it. Fast paths (monitor cache, archived
    # warning) leave it None — they don't read the row.
    if lineage_state is not None:
        payload["lineage_state"] = lineage_state
    if continuity_token:
        payload["continuity_token"] = continuity_token
    return payload


def build_onboard_response_data(
    *,
    agent_uuid: str,
    structured_agent_id: str,
    agent_label: Optional[str],
    stable_session_id: str,
    is_new: bool,
    force_new: bool,
    client_hint: str,
    was_archived: bool,
    trajectory_result: Optional[dict],
    parent_agent_id: Optional[str],
    thread_context: Optional[dict],
    verbose: bool,
    continuity_source: Optional[str],
    continuity_support: Dict[str, Any],
    continuity_token: Optional[str],
    system_activity: Optional[dict],
    tool_mode_info: Optional[dict],
    identity_resolution_outcome: Optional[str] = None,
    lineage_state: Optional[str] = None,
    provisional_lineage: bool = False,
) -> dict:
    """Build the onboard() response payload."""
    next_calls = [
        {
            "tool": "process_agent_update",
            "why": "Log your work. Call after completing tasks.",
            "args_min": {"response_text": "...", "complexity": 0.5},
            "args_full": {
                "client_session_id": stable_session_id,
                "response_text": "Summary of what you did",
                "complexity": 0.5,
                "confidence": 0.8,
            },
        },
        {
            "tool": "get_governance_metrics",
            "why": "Check your state (energy, coherence, etc.)",
            "args_min": {},
            "args_full": {"client_session_id": stable_session_id},
        },
        {
            "tool": "identity",
            "why": "Rename yourself or check identity later",
            "args_min": {},
            "args_full": {"client_session_id": stable_session_id, "name": "YourName"},
        },
    ]
    if continuity_token:
        for call in next_calls:
            args_full = call.get("args_full")
            if isinstance(args_full, dict):
                args_full["continuity_token"] = continuity_token

    client_tips = {
        "chatgpt": "ChatGPT loses session state. ALWAYS include client_session_id in every call.",
        "cursor": "Cursor maintains sessions well. client_session_id optional but recommended.",
        "claude_code": "Claude Code CLI maintains sessions via the governance hook chain. client_session_id optional.",
        "claude_desktop": "Claude Desktop has stable sessions. client_session_id optional.",
        "claude": "Anthropic-family client. For best continuity, include client_session_id in all tool calls.",
        "unknown": "For best session continuity, include client_session_id in all tool calls.",
    }

    friendly_name = agent_label or structured_agent_id
    if thread_context:
        if thread_context["is_root"]:
            welcome = (
                f"Your session ID is `{stable_session_id}`. "
                f"You are node 1 in thread {thread_context['thread_id'][:12]}."
            )
        else:
            pred = thread_context.get("predecessor")
            pred_desc = f" (position {pred['position']})" if pred and pred.get("position") else ""
            welcome = (
                f"Your session ID is `{stable_session_id}`. "
                f"You are node {thread_context['position']} in thread {thread_context['thread_id'][:12]}. "
                f"A predecessor exists{pred_desc}."
            )
        welcome_message = thread_context["honest_message"]
    elif is_new:
        welcome = f"Welcome! Your session ID is `{stable_session_id}`. Pass this as `client_session_id` in all calls."
        welcome_message = "Your identity is created. Use the templates below to get started."
    elif was_archived:
        welcome = f"Reactivated '{friendly_name}'. Session: `{stable_session_id}`."
        welcome_message = (
            f"Your agent was archived and has been reactivated with the same identity. "
            f"Pass `client_session_id: \"{stable_session_id}\"` in all tool calls for attribution."
        )
    else:
        welcome = f"Resumed identity '{friendly_name}'. Session: `{stable_session_id}`."
        welcome_message = (
            "Existing identity reused. "
            f"Pass `client_session_id: \"{stable_session_id}\"` in all tool calls for consistent attribution."
        )

    result = {
        "success": True,
        "welcome": welcome,
        "uuid": agent_uuid,
        "agent_id": structured_agent_id,
        "display_name": agent_label,
        "is_new": is_new,
        "client_session_id": stable_session_id,
        "session_resolution_source": continuity_source,
        "continuity_token_supported": continuity_support.get("enabled", False),
        "date_context": {"date": datetime.now().strftime("%Y-%m-%d"), "source": "mcp-server"},
        "next_step": "Call process_agent_update with response_text describing your work",
    }
    if identity_resolution_outcome:
        result["identity_resolution_outcome"] = identity_resolution_outcome
    # S1-a (2026-04-24): surface ownership_proof_version at top level so log
    # consumers and dashboards don't have to dig into the token payload or
    # rely on verbose=True. See docs/ontology/s1-continuity-token-retirement.md §4.5.
    if continuity_support.get("ownership_proof_version") is not None:
        result["ownership_proof_version"] = continuity_support["ownership_proof_version"]
    # R2 PR 3 (2026-05-04): surface honest-memory lineage state at top
    # level. `lineage_state` ∈ {"provisional", "rejected_cross_role",
    # "no_lineage_declared", None}; `provisional_lineage` mirrors the
    # storage column so downstream gates (trust-tier, KG provenance,
    # R3 baselines) can be derived from the response without a follow-up
    # query. See docs/ontology/r2-honest-memory-integration.md.
    if lineage_state is not None:
        result["lineage_state"] = lineage_state
    result["provisional_lineage"] = bool(provisional_lineage)

    if verbose:
        result["welcome_message"] = welcome_message
        result["force_new_applied"] = force_new
        result["session_continuity"] = {
            "client_session_id": stable_session_id,
            "instruction": "Your session is auto-bound. You only need client_session_id if tools don't recognize you.",
            "tip": client_tips.get(client_hint, client_tips["unknown"]),
            "resolution_source": continuity_source,
            "token_support": continuity_support,
        }
        result["next_calls_ref"] = "unitares://skill#workflow"
        result["next_calls"] = next_calls
        if system_activity is not None:
            result["system_activity"] = system_activity
        result["skill_resource"] = {
            "uri": "unitares://skill",
            "tip": "Read this MCP resource for full framework orientation instead of calling list_tools/describe_tool",
        }

    if thread_context:
        result["thread_context"] = thread_context
    if continuity_token:
        result["continuity_token"] = continuity_token
        if "session_continuity" in result:
            result["session_continuity"]["continuity_token"] = continuity_token
            result["session_continuity"]["instruction"] = (
                "Prefer continuity_token for robust resume across session-key changes. "
                "Use client_session_id when token support is unavailable."
            )

    if verbose:
        if tool_mode_info:
            result["tool_mode"] = tool_mode_info
        if is_new or force_new:
            result["workflow"] = {
                "step_1": "Copy client_session_id from above",
                "step_2": "Do your work",
                "step_3": "Call process_agent_update with response_text describing what you did",
                "loop": "Repeat steps 2-3. Check metrics with get_governance_metrics when curious.",
            }

    if parent_agent_id and not force_new:
        result["predecessor"] = {
            "uuid": parent_agent_id,
            "note": "Lineage record only; no state was inherited.",
        }

    if was_archived:
        result["auto_resumed"] = True
        result["previous_status"] = "archived"

    if trajectory_result:
        result["trajectory"] = dict(trajectory_result)
        result["trajectory"]["trust_tier"] = {
            "tier": 1,
            "name": "emerging",
            "reason": "Genesis stored at onboard. Identity will mature with behavioral consistency.",
        }

    return result
