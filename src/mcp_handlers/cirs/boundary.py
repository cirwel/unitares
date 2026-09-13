"""
CIRS boundary_contract handler — set, get, list trust policies.
"""

from typing import Dict, Any, Sequence
from datetime import datetime

from mcp.types import TextContent
from ..decorators import mcp_tool
from ..utils import success_response, error_response, require_registered_agent
from src.logging_utils import get_logger
from .types import TrustLevel, VoidResponsePolicy, BoundaryContract
from .storage import (
    _store_boundary_contract, _get_boundary_contract, _get_all_boundary_contracts,
)

logger = get_logger(__name__)


@mcp_tool("boundary_contract", timeout=10.0, register=False, description="CIRS Protocol: Declare trust policies and void response rules for multi-agent coordination")
async def handle_boundary_contract(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """
    CIRS BOUNDARY_CONTRACT - Multi-agent trust and boundary management.

    Three modes: set, get, list
    """
    # None-safe: `action` is declared with no default, so the middleware hands
    # an omitted action over as None, and it must reach the valid_actions refusal.
    action = (arguments.get("action") or "").lower()

    if not action or action not in ("set", "get", "list"):
        return [error_response(
            "action parameter required: 'set', 'get', or 'list'",
            recovery={
                "valid_actions": ["set", "get", "list"],
                "set_example": "boundary_contract(action='set', trust_default='partial')",
                "get_example": "boundary_contract(action='get', target_agent_id='...')"
            }
        )]

    if action == "set":
        return await _handle_boundary_contract_set(arguments)
    elif action == "get":
        return await _handle_boundary_contract_get(arguments)
    else:
        return await _handle_boundary_contract_list(arguments)


async def _handle_boundary_contract_set(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Handle BOUNDARY_CONTRACT set action"""
    agent_id, error = require_registered_agent(arguments)
    if error:
        return [error]

    trust_default_str = arguments.get("trust_default", "partial").lower()
    valid_trust = ["full", "partial", "observe", "none"]
    if trust_default_str not in valid_trust:
        return [error_response(
            f"Invalid trust_default: {trust_default_str}",
            recovery={"valid_values": valid_trust}
        )]
    trust_default = TrustLevel(trust_default_str)

    trust_overrides = arguments.get("trust_overrides", {})
    if trust_overrides:
        for target_id, level in trust_overrides.items():
            if level.lower() not in valid_trust:
                return [error_response(
                    f"Invalid trust level '{level}' for agent '{target_id}'",
                    recovery={"valid_values": valid_trust}
                )]
        trust_overrides = {k: v.lower() for k, v in trust_overrides.items()}

    void_policy_str = arguments.get("void_response_policy", "notify").lower()
    valid_policies = ["notify", "assist", "isolate", "coordinate"]
    if void_policy_str not in valid_policies:
        return [error_response(
            f"Invalid void_response_policy: {void_policy_str}",
            recovery={"valid_values": valid_policies}
        )]
    void_response_policy = VoidResponsePolicy(void_policy_str)

    max_delegation_complexity = float(arguments.get("max_delegation_complexity", 0.5))
    max_delegation_complexity = max(0.0, min(1.0, max_delegation_complexity))

    accept_coherence_threshold = float(arguments.get("accept_coherence_threshold", 0.4))
    accept_coherence_threshold = max(0.0, min(1.0, accept_coherence_threshold))

    existing = _get_boundary_contract(agent_id)
    boundary_violations = existing.get("boundary_violations", 0) if existing else 0

    contract = BoundaryContract(
        agent_id=agent_id,
        timestamp=datetime.now().isoformat(),
        trust_default=trust_default,
        trust_overrides=trust_overrides,
        void_response_policy=void_response_policy,
        max_delegation_complexity=max_delegation_complexity,
        accept_coherence_threshold=accept_coherence_threshold,
        boundary_violations=boundary_violations
    )

    _store_boundary_contract(contract)

    return success_response({
        "action": "set",
        "contract": contract.to_dict(),
        "message": f"Boundary contract set: trust_default={trust_default.value}, void_policy={void_response_policy.value}",
        "cirs_protocol": "BOUNDARY_CONTRACT"
    }, agent_id=agent_id)


async def _handle_boundary_contract_get(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Handle BOUNDARY_CONTRACT get action"""
    target_agent_id = arguments.get("target_agent_id")
    if not target_agent_id:
        return [error_response(
            "target_agent_id required for get action",
            recovery={"example": "boundary_contract(action='get', target_agent_id='...')"}
        )]

    contract = _get_boundary_contract(target_agent_id)
    if not contract:
        return [error_response(
            f"No boundary contract found for agent '{target_agent_id}'",
            recovery={
                "note": "Agent has not declared a boundary contract",
                "suggestion": "Use default trust assumptions or ask agent to set contract",
                "default_assumption": {
                    "trust_default": "partial",
                    "void_response_policy": "notify"
                }
            }
        )]

    return success_response({
        "action": "get",
        "contract": contract,
        "cirs_protocol": "BOUNDARY_CONTRACT"
    })


async def _handle_boundary_contract_list(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Handle BOUNDARY_CONTRACT list action"""
    contracts = _get_all_boundary_contracts()

    trust_distribution = {}
    policy_distribution = {}
    for c in contracts:
        trust = c.get("trust_default", "unknown")
        policy = c.get("void_response_policy", "unknown")
        trust_distribution[trust] = trust_distribution.get(trust, 0) + 1
        policy_distribution[policy] = policy_distribution.get(policy, 0) + 1

    summary = {
        "total_contracts": len(contracts),
        "trust_distribution": trust_distribution,
        "policy_distribution": policy_distribution,
    }

    return success_response({
        "action": "list",
        "contracts": contracts,
        "summary": summary,
        "cirs_protocol": "BOUNDARY_CONTRACT"
    })
