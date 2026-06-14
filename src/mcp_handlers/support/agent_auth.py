"""Agent authentication and identity verification for MCP handlers."""
from typing import Dict, Any, Tuple, Optional
from mcp.types import TextContent
from datetime import datetime

from src.logging_utils import get_logger

logger = get_logger(__name__)

_REGISTERED_AGENT_ALLOWED_STATUSES = ("active", "paused", "waiting_input")


def _identity_result_aliases(identity: Dict[str, Any]) -> set[str]:
    aliases = set()
    for key in ("agent_uuid", "agent_id", "public_agent_id", "display_name", "label"):
        value = identity.get(key)
        if value:
            aliases.add(str(value))
    return aliases


def _identity_result_row_status(identity: Optional[Dict[str, Any]]) -> Optional[str]:
    if not isinstance(identity, dict):
        return None
    status = identity.get("core_agent_row_status")
    if status:
        return str(status)
    if identity.get("archived"):
        return "archived"
    return None


def _select_trusted_identity_result(
    *,
    arguments: Dict[str, Any],
    context_identity: Optional[Dict[str, Any]],
    requested_agent_id: str,
    bound_uuid: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Choose middleware/core identity data matching this request."""
    candidates = []
    if isinstance(context_identity, dict):
        candidates.append(context_identity)
    arg_identity = arguments.get("_middleware_identity_result") if arguments else None
    if isinstance(arg_identity, dict):
        candidates.append(arg_identity)

    for identity in candidates:
        agent_uuid = identity.get("agent_uuid")
        if not agent_uuid:
            continue
        aliases = _identity_result_aliases(identity)
        if requested_agent_id in aliases or (bound_uuid and bound_uuid == agent_uuid):
            return identity
    return None


def compute_agent_signature(
    agent_id: Optional[str] = None,
    arguments: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Centralized agent signature computation.

    Priority order:
    1. Explicit agent_id parameter
    2. Context agent_id (set at dispatch entry)
    3. Session binding lookup
    """
    try:
        from ..context import get_context_agent_id
        from ..shared import get_mcp_server
        mcp_server = get_mcp_server()

        context_bound_id = get_context_agent_id()
        bound_id = agent_id or context_bound_id

        logger.debug(
            "compute_agent_signature resolved identity "
            "(explicit=%s, context=%s, final=%s)",
            agent_id is not None,
            context_bound_id is not None,
            bound_id is not None,
        )

        if not bound_id:
            return {"uuid": None}

        agent_uuid = bound_id

        display_label = None
        public_agent_id = None
        structured_id = None
        if bound_id in mcp_server.agent_metadata:
            meta = mcp_server.agent_metadata[bound_id]
            display_label = getattr(meta, 'label', None)
            public_agent_id = getattr(meta, 'public_agent_id', None)
            structured_id = getattr(meta, 'structured_id', None)

        signature = {"uuid": agent_uuid}
        # display_name (user-chosen) takes precedence over agent_id (auto-generated)
        auto_id = public_agent_id or structured_id
        if display_label:
            signature["agent_id"] = display_label
            if auto_id:
                signature["structured_agent_id"] = auto_id
            signature["display_name"] = display_label
        elif auto_id:
            signature["agent_id"] = auto_id

        # Dual-label visibility (identity-invariants #4: "Name is cosmetic").
        # label_source surfaces whether the displayed label reflects an
        # explicit choice by the agent or a server-derived auto-fill:
        #   "claimed" — label differs from auto-derived IDs, agent picked it
        #   "auto"    — label equals public_agent_id/structured_id, or the
        #               label is absent and the signature displays auto_id
        #   "uuid"    — neither label nor auto_id; display falls back to UUID
        # Heuristic (no schema change): identity is inferred from whether
        # the label matches known auto patterns. A future schema change can
        # replace this with an explicit label_claimed_at timestamp on the
        # agent metadata row.
        if display_label and display_label not in (public_agent_id, structured_id):
            signature["label_source"] = "claimed"
        elif display_label or auto_id:
            signature["label_source"] = "auto"
        else:
            signature["label_source"] = "uuid"
        return signature

    except Exception as e:
        logger.debug("compute_agent_signature error: %s", type(e).__name__)
        return {"uuid": None}


def check_agent_can_operate(agent_uuid: str) -> Optional[TextContent]:
    """
    Check if agent is allowed to perform operations (circuit breaker enforcement).

    Returns None if agent can operate, or an error TextContent if blocked.
    """
    from ..shared import get_mcp_server
    from ..error_handling import error_response
    mcp_server = get_mcp_server()

    if agent_uuid not in mcp_server.agent_metadata:
        return None

    meta = mcp_server.agent_metadata[agent_uuid]

    if meta.status == "paused":
        # Pause TTL: stale pauses auto-expire (in-memory flip is
        # synchronous; persistence is fire-and-forget). Sleep-wake
        # artifacts that produced the 2026-05-09 → 2026-05-18 Watcher/
        # Sentinel/Lumen silence are categorizer-driven pauses; once
        # the TTL elapses, the next gate-traversal here clears the
        # in-memory status, and the agent's next check-in flows through
        # the categorizer which re-pauses if state is genuinely
        # degraded. See src/mcp_handlers/support/pause_ttl.py.
        from .pause_ttl import maybe_auto_expire_pause_sync
        if maybe_auto_expire_pause_sync(agent_uuid, meta):
            return None  # status now active; let caller proceed
        return error_response(
            "Agent is paused - circuit breaker active",
            error_code="AGENT_PAUSED",
            error_category="state_error",
            details={
                "agent_id": agent_uuid[:12],
                "paused_at": meta.paused_at,
                "status": "paused",
            },
            recovery={
                "action": "Use self_recovery(action='quick') or self_recovery(action='review', reflection='...') to request recovery",
                "note": "Circuit breaker triggered due to governance threshold violation",
                "alternative": "Wait for auto-dialectic recovery to complete",
            }
        )
    elif meta.status == "archived":
        return error_response(
            "Agent is archived and cannot perform operations",
            error_code="AGENT_ARCHIVED",
            error_category="state_error",
            details={"agent_id": agent_uuid[:12], "status": "archived"},
            recovery={"action": (
                "Reclaim the SAME identity if this is the same live process: "
                "onboard(resume=true) with your continuity_token or "
                "client_session_id (auto-unarchives). Operator restore: "
                "agent(action='update'). Otherwise onboard fresh."
            )}
        )

    return None


def require_argument(arguments: Dict[str, Any], name: str,
                    error_message: str = None) -> Tuple[Any, Optional[TextContent]]:
    """
    Get required argument from arguments dict.

    Uses standardized error taxonomy for better agent self-service debugging.
    """
    value = arguments.get(name)
    if value is None:
        from ..error_helpers import missing_parameter_error
        tool_name = arguments.get("_tool_name")
        if error_message:
            context = {"custom_message": error_message}
            return None, missing_parameter_error(name, tool_name=tool_name, context=context)[0]
        return None, missing_parameter_error(name, tool_name=tool_name)[0]
    return value, None


def require_agent_id(arguments: Dict[str, Any]) -> Tuple[str, Optional[TextContent]]:
    """
    Get or auto-generate agent_id.

    Priority:
    - If agent_id provided: use it (with basic safety validation)
    - If session-bound: use that
    - If neither: auto-generate a UUID-based ID
    """
    agent_id = arguments.get("agent_id")
    explicit_agent_id = agent_id

    # FALLBACK 1: Check session-bound identity
    if not agent_id:
        try:
            from ..context import get_context_agent_id
            bound_id = get_context_agent_id()
            if bound_id:
                agent_id = bound_id
                logger.debug("Using session-bound identity UUID")
                arguments["agent_id"] = agent_id
        except Exception as e:
            logger.debug(
                "Could not retrieve session-bound identity: %s",
                type(e).__name__,
            )

    # Canonical ID resolution when both explicit agent_id and a session binding exist.
    #
    # Two cases:
    #   1. Self-reference: the explicit agent_id is an alias (label, structured_id,
    #      or public_agent_id) of the session-bound agent itself. Rewrite to the
    #      canonical UUID so downstream lookups use the storage key.
    #   2. Cross-agent reference: the explicit agent_id names a different agent
    #      (e.g. an admin calling `agent.update(agent_id='Lumen', ...)`). Honor
    #      the explicit value and let verify_agent_ownership downstream decide
    #      whether the caller is allowed to touch the target's record. Silently
    #      substituting the bound UUID here would violate the identity invariant
    #      (``never silently substitute identity'') by writing the caller's own
    #      record while reporting success under the requested agent_id.
    if explicit_agent_id:
        try:
            from ..context import get_context_agent_id
            bound_uuid = get_context_agent_id()
            if bound_uuid and explicit_agent_id != bound_uuid:
                try:
                    from ..shared import get_mcp_server
                    mcp_server = get_mcp_server()
                    if bound_uuid in mcp_server.agent_metadata:
                        meta = mcp_server.agent_metadata[bound_uuid]
                        label = getattr(meta, 'label', None)
                        structured_id = getattr(meta, 'structured_id', None)
                        public_agent_id = getattr(meta, 'public_agent_id', None)
                        if explicit_agent_id in (label, public_agent_id, structured_id):
                            # Case 1: alias of the bound agent — rewrite to UUID.
                            logger.debug("Explicit agent_id is a bound identity alias; using UUID")
                            agent_id = bound_uuid
                            arguments["agent_id"] = agent_id
                        else:
                            # Case 2: cross-agent reference — honor the explicit
                            # value; ownership verification runs downstream.
                            logger.debug(
                                "Explicit agent_id differs from bound UUID; "
                                "honoring explicit value"
                            )
                except Exception:
                    pass
        except Exception:
            pass

    # FALLBACK 2: Auto-generate if still missing
    # Identity Honesty Part C: this handler-layer generator was the second
    # ghost-creation path the #32 middleware flag missed. Gated on the same
    # env flag as PATH 0. See config.governance_config.identity_strict_mode.
    if not agent_id:
        from config.governance_config import identity_strict_mode
        _partc_mode = identity_strict_mode()
        if _partc_mode == "strict":
            return None, (
                "No agent_id provided and no session-bound identity. "
                "Call onboard(force_new=true, parent_agent_id='<prior UUID>' if continuing, "
                "spawn_reason='new_session') per v2 ontology — declare lineage rather than "
                "resume via token. Resume by proof requires identity(agent_uuid=X, "
                "continuity_token=Y) with both signals."
            )
        elif _partc_mode == "log":
            logger.warning(
                "[IDENTITY_STRICT] Would reject handler FALLBACK 2 "
                "auto-generation. Caller has no agent_id and no session binding."
            )

        import uuid
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        short_uuid = str(uuid.uuid4())[:8]
        agent_id = f"auto_{timestamp}_{short_uuid}"
        arguments["agent_id"] = agent_id
        logger.info("Auto-generated agent_id")

    # Validate format and reserved names
    from ..validators import validate_agent_id_format
    validated_id, format_error = validate_agent_id_format(agent_id)
    if format_error:
        return None, format_error

    from ..validators import validate_agent_id_reserved_names
    validated_id, reserved_error = validate_agent_id_reserved_names(validated_id)
    if reserved_error:
        return None, reserved_error

    return validated_id, None


def require_registered_agent(arguments: Dict[str, Any]) -> Tuple[str, Optional[TextContent]]:
    """
    Get required agent_id AND verify the agent is registered in the system.

    Returns agent_id (model+date) for storage. Sets arguments["_agent_display"]
    and arguments["_agent_uuid"] for internal use.
    """
    from ..error_handling import error_response

    agent_id, error = require_agent_id(arguments)
    if error:
        return None, error

    try:
        from ..shared import get_mcp_server
        from ..context import get_context_agent_id, get_session_context
        import uuid as uuid_module

        mcp_server = get_mcp_server()

        try:
            ensure_metadata_loaded = getattr(mcp_server, 'ensure_metadata_loaded', None)
            if ensure_metadata_loaded:
                ensure_metadata_loaded()
        except Exception as e:
            logger.debug(f"Could not ensure metadata loaded: {e}")

        is_uuid = False
        try:
            uuid_module.UUID(agent_id, version=4)
            is_uuid = True
        except (ValueError, AttributeError):
            pass

        agent_found = False
        actual_uuid = None
        public_agent_id = None
        structured_id = None
        display_name = None
        label = None
        context_identity = None
        try:
            context_identity = (get_session_context() or {}).get("identity_result")
        except Exception:
            context_identity = None

        if is_uuid:
            if agent_id in mcp_server.agent_metadata:
                agent_found = True
                actual_uuid = agent_id
                meta = mcp_server.agent_metadata[agent_id]
                public_agent_id = getattr(meta, 'public_agent_id', None)
                structured_id = getattr(meta, 'structured_id', None)
                display_name = getattr(meta, 'display_name', None) or getattr(meta, 'label', None)
                label = getattr(meta, 'label', None)
        else:
            for uuid_key, meta in mcp_server.agent_metadata.items():
                if agent_id in (
                    getattr(meta, 'label', None),
                    getattr(meta, 'public_agent_id', None),
                    getattr(meta, 'structured_id', None),
                ):
                    agent_found = True
                    actual_uuid = uuid_key
                    public_agent_id = getattr(meta, 'public_agent_id', None)
                    structured_id = getattr(meta, 'structured_id', None)
                    display_name = getattr(meta, 'display_name', None) or getattr(meta, 'label', None)
                    label = getattr(meta, 'label', None)
                    break

        if not agent_found:
            bound_uuid = get_context_agent_id()
            if bound_uuid and bound_uuid in mcp_server.agent_metadata:
                agent_found = True
                actual_uuid = bound_uuid
                meta = mcp_server.agent_metadata[bound_uuid]
                public_agent_id = getattr(meta, 'public_agent_id', None)
                structured_id = getattr(meta, 'structured_id', None)
                display_name = getattr(meta, 'display_name', None) or getattr(meta, 'label', None)
                label = getattr(meta, 'label', None)

        trusted_identity = _select_trusted_identity_result(
            arguments=arguments,
            context_identity=context_identity,
            requested_agent_id=agent_id,
            bound_uuid=get_context_agent_id(),
        )
        if not agent_found and trusted_identity:
            actual_uuid = trusted_identity.get("agent_uuid")
            agent_found = True
            public_agent_id = (
                trusted_identity.get("public_agent_id")
                or trusted_identity.get("agent_id")
            )
            structured_id = trusted_identity.get("structured_id")
            display_name = (
                trusted_identity.get("display_name")
                or trusted_identity.get("label")
            )
            label = trusted_identity.get("label")

        # S21-b §2: gate on meta.status. update_identity_status writes only PG,
        # so the in-memory dict can hold a stale-active row for an agent that
        # core.identities has marked archived/deleted/disabled. Without this
        # gate, a stale-positive caller passes auth and writes against a row
        # that downstream lifecycle code treats as terminal — the 67-row
        # active/archived inversion observed in council pass-2 (live-verifier).
        #
        # Allowlist (not blocklist) so a future status value not enumerated
        # below fails closed instead of silently passing through (council
        # pass-2 dialectic finding #1: blocklist is fail-open on unknown).
        if agent_found and actual_uuid:
            core_status = (
                _identity_result_row_status(trusted_identity)
                if (
                    trusted_identity
                    and trusted_identity.get("agent_uuid") == actual_uuid
                )
                else None
            )
            if core_status is not None:
                agent_status = core_status
            elif actual_uuid in mcp_server.agent_metadata:
                agent_status = getattr(
                    mcp_server.agent_metadata[actual_uuid], "status", "active"
                )
            else:
                agent_status = "active"
            if agent_status not in _REGISTERED_AGENT_ALLOWED_STATUSES:
                # Map to the inferer's keyword so error_code lands in the
                # right category (AGENT_ARCHIVED / AGENT_DELETED / etc.).
                if agent_status == "archived":
                    # Archival is reversible for the SAME live process:
                    # onboard(resume=true) auto-unarchives the same UUID. Route
                    # there first so a (possibly falsely-)archived live agent
                    # reclaims its identity + trajectory rather than being forced
                    # to mint a new one. Forward-lineage is the fallback only
                    # when continuity can't be proven.
                    _recovery = {
                        "error_type": "agent_archived",
                        "agent_status": "archived",
                        "action": (
                            "If this is the same live process, reclaim the SAME "
                            "identity: onboard(resume=true) with your "
                            "continuity_token or client_session_id — this "
                            "auto-unarchives the agent. Only if you cannot prove "
                            "continuity, onboard fresh and declare this UUID as "
                            "parent_agent_id (a new identity succeeding the "
                            "archived one)."
                        ),
                        "related_tools": ["onboard", "self_recovery"],
                    }
                else:
                    _recovery = {
                        "error_type": f"agent_{agent_status}",
                        "agent_status": agent_status,
                        "action": "Onboard a fresh identity with parent_agent_id set to this UUID to declare lineage.",
                        "related_tools": ["onboard"],
                    }
                return None, error_response(
                    f"Agent '{agent_id}' is {agent_status} and cannot accept calls.",
                    recovery=_recovery,
                )

        if not agent_found:
            from .naming_helpers import (
                detect_interface_context,
                generate_name_suggestions,
                format_naming_guidance
            )

            context = detect_interface_context()
            existing_names = [
                getattr(m, 'label', None)
                for m in mcp_server.agent_metadata.values()
                if getattr(m, 'label', None)
            ]
            suggestions = generate_name_suggestions(
                context=context,
                existing_names=existing_names
            )
            naming_guidance = format_naming_guidance(suggestions=suggestions)

            return None, error_response(
                f"Agent '{agent_id}' is not registered. Identity auto-creates on first tool call.",
                recovery={
                    "error_type": "agent_not_registered",
                    "action": "Call onboard() first to create your identity, or call process_agent_update() to auto-create",
                    "related_tools": ["onboard", "process_agent_update", "identity", "list_tools"],
                    "workflow": [
                        "1. Call onboard(force_new=true, parent_agent_id='<prior UUID>' if continuing prior work, spawn_reason='new_session')",
                        "   — per v2 ontology, fresh process-instances mint fresh identity; lineage is declared, not resumed via token",
                        "2. Save client_session_id from response",
                        "3. Call identity(name='your_name') to set a cosmetic label",
                        "4. Include client_session_id in all future calls within this process-instance",
                        "5. Then call this tool again"
                    ],
                    "naming_suggestions": naming_guidance,
                    "onboarding_sequence": ["onboard", "identity", "process_agent_update", "list_tools"],
 "tip": "onboard registers the current process-instance with governance — for the full v2 ontology"
                }
            )

        resolved_public_id = public_agent_id or structured_id or label or f"Agent_{actual_uuid[:8]}"
        arguments["agent_id"] = resolved_public_id
        arguments["_agent_display"] = {
            "agent_id": resolved_public_id,
            "display_name": display_name or label or resolved_public_id,
            "label": label,
        }
        arguments["_agent_uuid"] = actual_uuid

        return actual_uuid, None

    except Exception as e:
        return None, error_response(
            f"Could not verify agent registration: {str(e)}",
            recovery={
                "action": "System error checking agent registration. Try onboard() or health_check() first.",
                "related_tools": ["onboard", "health_check", "identity"],
                "workflow": [
                    "1. Call health_check() to verify system is healthy",
                    "2. Call onboard() to create your identity",
                    "3. Save client_session_id and include it in future calls"
                ],
                "note": "Identity auto-creates on first tool call. Use onboard() for the best first-time experience."
            }
        )


def verify_agent_ownership(agent_id: str, arguments: Dict[str, Any]) -> bool:
    """
    Verify that the current session owns/is bound to the given agent_id.

    Uses UUID-based auth via session binding.

    The former ``allow_operator`` parameter has been removed. It granted
    cross-agent access whenever the caller's own metadata carried
    ``label == 'operator'`` or ``'operator'`` in ``tags``. Since labels and
    tags are self-claimed at onboard and never server-verified, that branch
    let any agent self-promote by onboarding with the right string. Name and
    tag are cosmetic per the identity-invariants; lookup by label is not an
    authorization primitive. If cross-agent privilege is needed in future,
    it must come from an explicit server-side ACL, not caller-claimed strings.
    """
    try:
        from ..context import get_context_agent_id
        from ..shared import get_mcp_server

        mcp_server = get_mcp_server()

        bound_id = get_context_agent_id()
        if bound_id == agent_id:
            return True

        if bound_id:
            meta = mcp_server.agent_metadata.get(bound_id)
            if meta and getattr(meta, 'agent_uuid', None) == agent_id:
                return True

        return False
    except Exception as e:
        logger.debug(f"verify_agent_ownership failed: {e}")
        return False
