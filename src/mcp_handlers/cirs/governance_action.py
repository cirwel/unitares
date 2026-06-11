"""
CIRS governance_action handler — initiate, respond, query, status.
"""

from typing import Dict, Any, Sequence
from datetime import datetime
import uuid

from mcp.types import TextContent
from ..decorators import mcp_tool
from ..utils import success_response, error_response, require_registered_agent
from src.logging_utils import get_logger
from .types import GovernanceActionType, GovernanceAction
from .storage import (
    _store_governance_action, _get_governance_action,
    _get_governance_actions_for_agent, _get_boundary_contract,
    _governance_action_buffer,
)

logger = get_logger(__name__)
from src.mcp_handlers.shared import lazy_mcp_server as mcp_server
@mcp_tool("governance_action", timeout=15.0, register=False, description="CIRS Protocol: Coordinate interventions across agents for collaborative governance")
async def handle_governance_action(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """
    CIRS GOVERNANCE_ACTION - Multi-agent intervention coordination.

    Four modes: initiate, respond, query, status
    """
    action = arguments.get("action", "").lower()

    if not action or action not in ("initiate", "respond", "query", "status"):
        return [error_response(
            "action parameter required: 'initiate', 'respond', 'query', or 'status'",
            recovery={
                "valid_actions": ["initiate", "respond", "query", "status"],
                "initiate_example": "governance_action(action='initiate', action_type='void_intervention', target_agent_id='...')",
                "respond_example": "governance_action(action='respond', action_id='...', accept=True)"
            }
        )]

    if action == "initiate":
        return await _handle_governance_action_initiate(arguments)
    elif action == "respond":
        return await _handle_governance_action_respond(arguments)
    elif action == "query":
        return await _handle_governance_action_query(arguments)
    else:
        return await _handle_governance_action_status(arguments)

async def _handle_governance_action_initiate(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Handle GOVERNANCE_ACTION initiate"""
    agent_id, error = require_registered_agent(arguments)
    if error:
        return [error]

    action_type_str = arguments.get("action_type", "").lower()
    valid_types = ["void_intervention", "coherence_boost", "delegation_request", "delegation_response", "coordination_sync"]
    if not action_type_str or action_type_str not in valid_types:
        return [error_response(
            f"Invalid or missing action_type: {action_type_str}",
            recovery={"valid_values": valid_types}
        )]
    action_type = GovernanceActionType(action_type_str)

    target_agent_id = arguments.get("target_agent_id")
    if not target_agent_id:
        return [error_response(
            "target_agent_id required for initiate action",
            recovery={"example": "governance_action(action='initiate', action_type='...', target_agent_id='...')"}
        )]

    target_contract = _get_boundary_contract(target_agent_id)
    trust_warning = None
    if target_contract:
        trust_level = target_contract.get("trust_overrides", {}).get(agent_id)
        if trust_level is None:
            trust_level = target_contract.get("trust_default", "partial")

        if trust_level == "none":
            return [error_response(
                f"Target agent '{target_agent_id}' has trust level 'none' for you",
                recovery={
                    "note": "Target does not accept interactions from this agent",
                    "suggestion": "Contact target through other channels to establish trust"
                }
            )]
        elif trust_level == "observe":
            trust_warning = "Target agent has 'observe' trust level - may reject active interventions"

    action_id = str(uuid.uuid4())[:12]
    payload = arguments.get("payload", {})

    if action_type == GovernanceActionType.VOID_INTERVENTION:
        monitor = mcp_server.get_or_create_monitor(agent_id)
        from src.agent_monitor_state import ensure_hydrated
        await ensure_hydrated(monitor, agent_id)
        metrics = monitor.get_metrics()
        payload["initiator_state"] = {
            "coherence": float(metrics.get("coherence", 0.5)),
            "risk_score": float(metrics.get("risk_score") or 0.3),
            "verdict": str(metrics.get("verdict", "caution"))
        }

    gov_action = GovernanceAction(
        action_id=action_id,
        timestamp=datetime.now().isoformat(),
        action_type=action_type,
        initiator_agent_id=agent_id,
        target_agent_id=target_agent_id,
        payload=payload,
        status="pending"
    )

    _store_governance_action(gov_action)

    response = {
        "action": "initiate",
        "governance_action": gov_action.to_dict(),
        "message": f"Governance action '{action_type.value}' initiated for {target_agent_id}",
        "cirs_protocol": "GOVERNANCE_ACTION"
    }

    if trust_warning:
        response["warning"] = trust_warning

    return success_response(response, agent_id=agent_id)

async def _handle_governance_action_respond(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Handle GOVERNANCE_ACTION respond"""
    agent_id, error = require_registered_agent(arguments)
    if error:
        return [error]

    action_id = arguments.get("action_id")
    if not action_id:
        return [error_response(
            "action_id required for respond action",
            recovery={"example": "governance_action(action='respond', action_id='...', accept=True)"}
        )]

    gov_action = _get_governance_action(action_id)
    if not gov_action:
        return [error_response(
            f"Governance action '{action_id}' not found",
            recovery={"suggestion": "Use governance_action(action='query') to see your pending actions"}
        )]

    if gov_action["target_agent_id"] != agent_id:
        return [error_response(
            "You are not the target of this governance action",
            recovery={"note": f"Target is {gov_action['target_agent_id']}"}
        )]

    if gov_action["status"] != "pending":
        return [error_response(
            f"Action already has status '{gov_action['status']}'",
            recovery={"note": "Cannot respond to non-pending actions"}
        )]

    accept = arguments.get("accept", False)
    response_data = arguments.get("response_data", {})

    new_status = "accepted" if accept else "rejected"
    gov_action["status"] = new_status
    gov_action["response"] = {
        "responder_agent_id": agent_id,
        "accepted": accept,
        "response_time": datetime.now().isoformat(),
        "data": response_data
    }

    _governance_action_buffer[action_id] = gov_action

    return success_response({
        "action": "respond",
        "governance_action": gov_action,
        "message": f"Governance action {new_status}: {gov_action['action_type']}",
        "cirs_protocol": "GOVERNANCE_ACTION"
    }, agent_id=agent_id)

async def _handle_governance_action_query(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Handle GOVERNANCE_ACTION query"""
    agent_id, error = require_registered_agent(arguments)
    if error:
        return [error]

    as_initiator = arguments.get("as_initiator", True)
    as_target = arguments.get("as_target", True)
    status_filter = arguments.get("status_filter")

    actions = _get_governance_actions_for_agent(
        agent_id,
        as_initiator=as_initiator,
        as_target=as_target,
        status=status_filter
    )

    pending = sum(1 for a in actions if a["status"] == "pending")
    accepted = sum(1 for a in actions if a["status"] == "accepted")
    rejected = sum(1 for a in actions if a["status"] == "rejected")

    summary = {
        "total_actions": len(actions),
        "pending": pending,
        "accepted": accepted,
        "rejected": rejected,
    }

    return success_response({
        "action": "query",
        "actions": actions,
        "summary": summary,
        "cirs_protocol": "GOVERNANCE_ACTION"
    }, agent_id=agent_id)

async def _handle_governance_action_status(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Handle GOVERNANCE_ACTION status"""
    action_id = arguments.get("action_id")
    if not action_id:
        return [error_response(
            "action_id required for status action",
            recovery={"example": "governance_action(action='status', action_id='...')"}
        )]

    gov_action = _get_governance_action(action_id)
    if not gov_action:
        return [error_response(
            f"Governance action '{action_id}' not found",
            recovery={"note": "Action may have expired (24h TTL)"}
        )]

    return success_response({
        "action": "status",
        "governance_action": gov_action,
        "cirs_protocol": "GOVERNANCE_ACTION"
    })
