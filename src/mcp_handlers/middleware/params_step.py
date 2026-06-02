"""Steps 3-6: Parameter handling (unwrap kwargs, resolve alias, inject identity, validate)."""

import json
import re
from difflib import get_close_matches
from typing import Any, Dict

from src.logging_utils import get_logger
from ..utils import error_response
from ..shared import lazy_mcp_server as mcp_server

logger = get_logger(__name__)


def _bound_identity_aliases(bound_id: str) -> set[str]:
    """Collect accepted aliases for a bound UUID from runtime/session caches."""
    aliases = {str(bound_id)}

    try:
        meta = mcp_server.agent_metadata.get(bound_id)
        if meta:
            for attr in ("label", "public_agent_id", "structured_id"):
                value = getattr(meta, attr, None)
                if value:
                    aliases.add(str(value))
    except Exception:
        pass

    try:
        from ..identity.shared import _session_identities

        for binding in _session_identities.values():
            if binding.get("bound_agent_id") == bound_id or binding.get("agent_uuid") == bound_id:
                for key in ("display_agent_id", "public_agent_id", "agent_label", "label"):
                    value = binding.get(key)
                    if value:
                        aliases.add(str(value))
    except Exception:
        pass

    return aliases


async def unwrap_kwargs(name: str, arguments: Dict[str, Any], ctx) -> Any:
    """Handle MCP clients that wrap arguments in kwargs."""
    if "kwargs" in arguments:
        kwargs_val = arguments["kwargs"]
        if isinstance(kwargs_val, str):
            try:
                kwargs_parsed = json.loads(kwargs_val)
                if isinstance(kwargs_parsed, dict):
                    del arguments["kwargs"]
                    arguments.update(kwargs_parsed)
                    logger.info(f"[DISPATCH_KWARGS] Unwrapped from string: {list(kwargs_parsed.keys())}")
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning(f"Failed to parse kwargs string: {e}")
        elif isinstance(kwargs_val, dict):
            del arguments["kwargs"]
            arguments.update(kwargs_val)
            logger.info(f"[DISPATCH_KWARGS] Unwrapped from dict: {list(kwargs_val.keys())}")

    return name, arguments, ctx


async def resolve_alias(name: str, arguments: Dict[str, Any], ctx) -> Any:
    """Resolve tool aliases and inject action parameters."""
    from ..tool_stability import resolve_tool_alias

    ctx.original_name = name
    actual_name, alias_info = resolve_tool_alias(name)

    if alias_info:
        ctx.migration_note = alias_info.migration_note
        name = actual_name

        if alias_info.inject_action and "action" not in arguments:
            arguments["action"] = alias_info.inject_action
            logger.debug(f"[ALIAS] Injected action='{alias_info.inject_action}' for consolidated tool '{actual_name}'")

    return name, arguments, ctx


async def inject_identity(name: str, arguments: Dict[str, Any], ctx) -> Any:
    """Auto-inject agent_id from session, prevent impersonation."""
    try:
        from ..context import get_context_agent_id
        bound_id = get_context_agent_id()
        provided_id = arguments.get("agent_id")

        if bound_id:
            if not provided_id:
                # Browsable data tools should NOT auto-filter by agent
                browsable_data_tools = {
                    "search_knowledge_graph", "query_knowledge_graph", "list_knowledge_graph",
                    "list_dialectic_sessions", "get_dialectic_session", "dialectic"
                }
                # For the consolidated 'knowledge' tool, only skip injection for read/browse
                # actions (search/list/stats/details/get) so agents can learn from each
                # other's discoveries. Write actions (store/note/update/supersede) still
                # need attribution and get agent_id injected normally.
                knowledge_browsable_actions = {"search", "list", "stats", "details", "get"}
                action = arguments.get("action", "")
                is_knowledge_browsable = (
                    name == "knowledge" and action in knowledge_browsable_actions
                )
                logger.info(
                    f"[DISPATCH] name={name}, action={action!r}, "
                    f"in browsable_data_tools={name in browsable_data_tools}, "
                    f"is_knowledge_browsable={is_knowledge_browsable}, "
                    f"bound_id={bound_id[:8] if bound_id else None}..."
                )
                # bind_session handles its own identity resolution — injecting
                # the middleware-resolved agent_id overwrites its validation.
                identity_internal_tools = {"bind_session"}
                if name not in browsable_data_tools and not is_knowledge_browsable and name not in identity_internal_tools:
                    arguments["agent_id"] = bound_id
                    logger.debug(f"Injected session-bound agent_id: {bound_id}")
            elif provided_id != bound_id:
                # Prevent impersonation
                identity_tools = {"status"}
                dialectic_tools = {
                    "submit_thesis", "submit_antithesis", "submit_synthesis",
                    "request_dialectic_review"
                }

                accepted_aliases = _bound_identity_aliases(bound_id)
                is_alias_match = str(provided_id) in accepted_aliases
                if is_alias_match:
                    logger.debug(f"Identity alias match allowed: {provided_id} -> {bound_id}")

                # Operator tools that act on OTHER agents (dashboard resume/archive/observe)
                operator_tools = {
                    "agent", "observe_agent", "detect_stuck_agents",
                    "archive_agent", "archive_old_test_agents",
                    "direct_resume_if_safe", "operator_resume_agent",
                    "ping_agent",
                    "dashboard",
                }
                if name not in identity_tools and name not in dialectic_tools and name not in operator_tools and not is_alias_match:
                    return [error_response(
                        f"Session mismatch: you are bound as '{bound_id}' but requested '{provided_id}'",
                        details={
                            "error_type": "identity_mismatch",
                            "bound_agent_id": bound_id,
                            "requested_agent_id": provided_id,
                            "accepted_aliases": sorted(accepted_aliases),
                        },
                        recovery={
                            "action": "Remove agent_id parameter (session binding handles identity)",
                            "note": "Each session is bound to one agent. Identity auto-binds on first tool call.",
                            "related_tools": ["identity"]
                        }
                    )]
        elif provided_id:
            # REST client with X-Agent-Id but no session binding
            logger.warning(f"[IDENTITY] No session binding but agent_id provided: {provided_id}. V2 may have failed.")
            arguments["agent_id"] = provided_id
        else:
            # No binding and no agent_id
            identity_tools = {"status", "list_tools", "health_check", "get_server_info",
                              "describe_tool", "debug_request_context", "onboard", "identity"}
            if name not in identity_tools:
                logger.warning(f"[IDENTITY] No identity for tool {name}. V2 should have created one.")
    except Exception as e:
        logger.debug(f"Session identity check skipped: {e}")

    return name, arguments, ctx


async def validate_params(name: str, arguments: Dict[str, Any], ctx) -> Any:
    """Parameter validation: aliases → Pydantic model_validate (if available)."""
    from ..validators import apply_param_aliases

    # Step 1: Fuzzy parameter aliases (e.g., "content" → "summary")
    arguments = apply_param_aliases(name, arguments)

    # Step 3: Pydantic model validation (structural + range + enum validation)
    try:
        from src.tool_schemas import get_pydantic_schemas
        schema_model = get_pydantic_schemas().get(name)
    except Exception:
        schema_model = None

    if name == "process_agent_update":
        try:
            from src.provenance_context import recover_mangled_s22_provenance

            recovery_warnings = recover_mangled_s22_provenance(arguments)
            if recovery_warnings:
                existing = arguments.get("_mangled_s22_recovery_warnings") or []
                arguments["_mangled_s22_recovery_warnings"] = [
                    *existing,
                    *recovery_warnings,
                ]
        except Exception as exc:
            logger.debug("S22 provenance unmangling skipped before validation: %s", exc)

    if schema_model:
        try:
            from pydantic import ValidationError
            validated = schema_model.model_validate(arguments)
            validated_dict = validated.model_dump()
            # Preserve extra fields not in the model (e.g., _param_coercions, client_hint)
            for key, value in arguments.items():
                if key not in validated_dict:
                    validated_dict[key] = value
            arguments = validated_dict
        except ValidationError as e:
            return [_format_pydantic_error(e, name)]
        except Exception as e:
            # Non-validation errors (import failure, etc.): log and continue with coerced dict
            logger.debug(f"Pydantic validation skipped for {name}: {e}")
    else:
        # Fallback debug log - should not happen if migration 100% successful
        logger.warning(f"[VALIDATION] Schema for {name} not found in Pydantic models!")

    return name, arguments, ctx


def _format_pydantic_error(error, tool_name: str):
    """Format a Pydantic ValidationError into a helpful TextContent error response."""
    from ..error_helpers import invalid_parameter_type_error, missing_parameter_error

    errors = error.errors()
    if not errors:
        return error_response(f"Validation error for '{tool_name}': {error}")

    first = errors[0]
    loc = ".".join(str(x) for x in first.get("loc", []))
    msg = first.get("msg", "")
    err_type = first.get("type", "")

    # Map common Pydantic error types to our error taxonomy
    if err_type == "missing":
        missing_fields = [
            ".".join(str(part) for part in err.get("loc", []))
            for err in errors
            if err.get("type") == "missing"
        ]
        if len(missing_fields) > 1:
            return error_response(
                f"Missing required parameters for '{tool_name}': {', '.join(missing_fields)}",
                error_code="MISSING_PARAMETER",
                error_category="validation_error",
                details={
                    "error_type": "missing_parameter",
                    "tool_name": tool_name,
                    "missing_parameters": missing_fields,
                },
                recovery={
                    "action": f"Add all missing parameters: {', '.join(missing_fields)}",
                    "related_tools": ["describe_tool"],
                    "workflow": [
                        f"1. Use describe_tool(tool_name='{tool_name}') for the full schema",
                        "2. Add the missing parameters listed above",
                        "3. Retry the tool call",
                    ],
                },
            )
        return missing_parameter_error(loc, tool_name=tool_name)[0]

    if err_type == "literal_error":
        valid_values = _literal_expected_values(first)
        provided = first.get("input")
        suggestion = _suggest_enum_value(loc, provided, valid_values)
        message = f"Invalid value for '{loc}': {provided!r}. Valid values: {', '.join(valid_values)}"
        recovery_action = f"Use one of: {', '.join(valid_values)}"
        if suggestion:
            message += f". Did you mean '{suggestion}'?"
            recovery_action = f"Use {loc}='{suggestion}' or another valid value"
        return error_response(
            message,
            error_code="PARAMETER_ERROR",
            error_category="validation_error",
            details={
                "error_type": "invalid_enum_value",
                "tool_name": tool_name,
                "parameter": loc,
                "provided_value": provided,
                "valid_values": valid_values,
                "suggested_value": suggestion,
            },
            recovery={
                "action": recovery_action,
                "related_tools": ["describe_tool"],
                "workflow": [
                    f"1. Use describe_tool(tool_name='{tool_name}') for allowed values",
                    f"2. Set {loc} to a valid value",
                    "3. Retry the tool call",
                ],
            },
        )

    if err_type in ("int_parsing", "float_parsing", "bool_parsing"):
        expected = err_type.replace("_parsing", "")
        provided = str(first.get("input", ""))[:50]
        return invalid_parameter_type_error(loc, expected, type(first.get("input")).__name__, tool_name=tool_name)[0]

    # General error: include all issues for multi-error cases
    detail_lines = []
    for err in errors[:5]:  # Cap at 5 errors
        err_loc = ".".join(str(x) for x in err.get("loc", []))
        detail_lines.append(f"  - {err_loc}: {err.get('msg', '')}")
    detail_text = "\n".join(detail_lines)

    return error_response(
        f"Parameter validation error for '{tool_name}':\n{detail_text}",
        error_code="PARAMETER_ERROR",
        error_category="validation_error",
        details={
            "tool_name": tool_name,
            "errors": [{"field": ".".join(str(x) for x in e["loc"]), "message": e["msg"]} for e in errors[:5]],
        },
        recovery={
            "action": "Check parameter types and try again",
            "related_tools": ["describe_tool"],
            "workflow": [
                f"1. Use describe_tool(tool_name='{tool_name}') for full schema",
                "2. Fix the parameters listed above",
                "3. Retry the tool call"
            ]
        }
    )


def _literal_expected_values(error: Dict[str, Any]) -> list[str]:
    """Extract Literal choices from Pydantic's human-readable ctx."""
    expected = (error.get("ctx") or {}).get("expected", "")
    values = re.findall(r"'([^']+)'", expected)
    return values or [expected] if expected else []


def _suggest_enum_value(parameter: str, provided: Any, valid_values: list[str]) -> str | None:
    """Suggest a likely enum value for common caller vocabulary."""
    if provided is None or not valid_values:
        return None

    raw = str(provided).strip()
    lowered = raw.lower()
    aliases = {
        "severity": {
            "info": "low",
            "informational": "low",
            "warn": "medium",
            "warning": "medium",
            "error": "high",
            "fatal": "critical",
            "urgent": "critical",
        },
        "discovery_type": {
            "bug": "bug_found",
            "issue": "bug_found",
            "finding": "insight",
            "idea": "insight",
            "obs": "observation",
        },
    }
    alias = aliases.get(parameter, {}).get(lowered)
    if alias in valid_values:
        return alias

    matches = get_close_matches(lowered, valid_values, n=1, cutoff=0.58)
    return matches[0] if matches else None
