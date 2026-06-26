"""MCP handler for the file-backed research-run registry."""

from __future__ import annotations

from typing import Any, Dict, Sequence

from mcp.types import TextContent

from src.mcp_handlers.decorators import mcp_tool
from src.mcp_handlers.utils import error_response, success_response
from src.research_registry import (
    ResearchRegistryError,
    ResearchRunNotFound,
    load_research_run,
    query_research_runs,
    record_research_run,
    research_registry_stats,
    rigor_checklist,
    grounding_status,
)


_READ_ACTIONS = {"list", "query", "get", "stats", "export"}
_RECORD_FIELDS = {
    "run_id",
    "title",
    "status",
    "scenario",
    "topology",
    "population",
    "tools",
    "memory",
    "communication_channels",
    "interventions",
    "metrics",
    "observations",
    "outcomes",
    "artifacts",
    "linked_knowledge_ids",
    "linked_outcome_ids",
    "linked_finding_ids",
    "research_areas",
    "tags",
    "exogenous_anchor",
    "hypothesis",
    "operator_question",
    "notes",
}


def _record_payload(arguments: Dict[str, Any]) -> dict[str, Any]:
    nested = arguments.get("run")
    if nested is not None:
        if not isinstance(nested, dict):
            raise ResearchRegistryError("run must be an object")
        payload = dict(nested)
    else:
        payload = {}
    for key in _RECORD_FIELDS:
        if key in arguments:
            payload[key] = arguments[key]
    return payload


@mcp_tool(
    "research_registry",
    timeout=15.0,
    description=(
        "Register and query agent-network research runs: scenario, topology, "
        "population, interventions, metrics, exogenous anchors, outcomes, and artifacts."
    ),
    pre_onboard_actions=_READ_ACTIONS,
    default_action="list",
)
async def handle_research_registry(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Register and query agent-network research runs."""

    action = str((arguments or {}).get("action") or "list").lower()

    try:
        if action in {"list", "query"}:
            data = query_research_runs(
                status=arguments.get("status"),
                tag=arguments.get("tag"),
                scenario_id=arguments.get("scenario_id"),
                research_area=arguments.get("research_area"),
                grounding=arguments.get("grounding"),
                query=arguments.get("query"),
                limit=arguments.get("limit", 50),
                include_details=arguments.get("include_details") in (True, "true", "1", "yes"),
            )
            return success_response(data, arguments=arguments)

        if action == "stats":
            return success_response(
                {"stats": research_registry_stats()},
                arguments=arguments,
            )

        if action in {"get", "export"}:
            run_id = arguments.get("run_id")
            if not run_id:
                return [error_response("run_id is required")]
            record = load_research_run(str(run_id))
            return success_response(
                {
                    "run": record,
                    "rigor_checklist": rigor_checklist(record),
                    "grounding_status": grounding_status(record),
                },
                arguments=arguments,
            )

        if action == "record":
            payload = _record_payload(arguments)
            record = record_research_run(payload)
            return success_response(
                {
                    "run": record,
                    "rigor_checklist": rigor_checklist(record),
                    "grounding_status": grounding_status(record),
                },
                arguments=arguments,
            )

        return [error_response(
            f"Unknown action: {action}",
            recovery={
                "valid_actions": sorted([*_READ_ACTIONS, "record"]),
                "examples": [
                    "research_registry(action='query', research_area='science-of-agent-networks')",
                    "research_registry(action='record', run={...})",
                ],
            },
        )]
    except ResearchRunNotFound as exc:
        return [error_response(str(exc))]
    except ResearchRegistryError as exc:
        return [error_response(str(exc))]
