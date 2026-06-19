"""Transport-neutral payload builders for identity-related tools."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from src.services.principal_rollup import lookup as _principal_lookup


S22_IDENTITY_RESPONSE_SCHEMA = "s22.identity_response.v1"

_STRONG_IDENTITY_SOURCES = {
    "continuity_token",
    "client_session_id",
    "explicit_client_session_id",
    "explicit_client_session_id_scoped",
    "mcp_session_id",
    "x_session_id",
    "oauth_client_id",
    "agent_uuid_direct",
    "agent_uuid_direct_fastpath",
}

_MEDIUM_IDENTITY_SOURCES = {
    "x_client_id",
    "pinned_onboard_session",
    "context_mcp_session_id",
    "context_session_key",
}


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
    client_hint: Optional[str] = None,
    proof_origin: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the identity() response payload."""
    identity_context = build_identity_response_context(
        agent_uuid=agent_uuid,
        agent_id=agent_id,
        display_name=display_name,
        session_resolution_source=continuity_source,
        identity_status=identity_status,
        identity_resolution_outcome=identity_resolution_outcome,
        client_hint=client_hint,
        model_type=model_type,
        proof_origin=proof_origin,
    )
    response_data = {
        "uuid": agent_uuid,
        "agent_id": agent_id,
        "display_name": display_name,
        "client_session_id": client_session_id,
        "session_resolution_source": continuity_source,
        "continuity_token_supported": continuity_support.get("enabled", False),
        "identity_status": identity_status,
        "identity_context": identity_context,
        "identity_assurance": identity_context["identity_assurance"],
        "bound_identity": {
            "uuid": agent_uuid,
            "agent_id": agent_id,
            "display_name": display_name,
        },
    }
    # Derived principal (octopus) — "you are one instance of logical worker P".
    # Advisory/display-only, fail-open (absent for singletons or pre-reconcile);
    # NEVER a credential. See src/services/principal_rollup.py.
    _principal = _principal_lookup(agent_uuid)
    if _principal:
        response_data["principal"] = _principal
    if identity_resolution_outcome:
        response_data["identity_resolution_outcome"] = identity_resolution_outcome
    # R2 PR 3: surface persisted lineage flag at top level so callers
    # don't need a follow-up read to detect provisional edges. See
 # .
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
            response_data["quick_reference"]["for_path0_ownership_proof"] = continuity_token

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
                    "Use client_session_id for continuity within this process. "
                    "continuity_token is only for proof-owned PATH 0 rebinds "
                    "with agent_uuid."
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
    client_hint: Optional[str] = None,
    model_type: Optional[str] = None,
    proof_origin: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the lightweight identity diagnostic payload used by fast-return paths."""
    identity_context = build_identity_response_context(
        agent_uuid=agent_uuid,
        agent_id=agent_id,
        display_name=display_name,
        session_resolution_source=continuity_source,
        identity_status=identity_status,
        identity_resolution_outcome=identity_resolution_outcome,
        client_hint=client_hint,
        model_type=model_type,
        proof_origin=proof_origin,
    )
    payload = {
        "uuid": agent_uuid,
        "agent_id": agent_id,
        "display_name": display_name,
        "client_session_id": client_session_id,
        "session_resolution_source": continuity_source,
        "continuity_token_supported": continuity_support.get("enabled", False),
        "identity_status": identity_status,
        "identity_context": identity_context,
        "identity_assurance": identity_context["identity_assurance"],
        "bound_identity": {
            "uuid": agent_uuid,
            "agent_id": agent_id,
            "display_name": display_name,
        },
    }
    # Derived principal (octopus) — "you are one instance of logical worker P".
    # Advisory/display-only, fail-open (absent for singletons or pre-reconcile);
    # NEVER a credential. See src/services/principal_rollup.py.
    _principal = _principal_lookup(agent_uuid)
    if _principal:
        payload["principal"] = _principal
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
    proof_origin: Optional[str] = None,
    response_mode: str = "full",
) -> dict:
    """Build the onboard() response payload.

    `response_mode` controls verbosity of the identity envelope (#734):

    - ``"full"`` (default) returns the complete identity ontology —
      `identity_context` with its nested registry/public_handle/label/
      harness_context blocks, plus a top-level `identity_assurance` mirror.
      Preserved byte-compatibly for existing consumers (dashboard, plugin).
    - ``"minimal"`` returns a lean payload: uuid, agent_id, session id, the
      single `identity_assurance` block, the resolution verdict, lineage
      flags, and a `next_step` hint — dropping the nested ontology and the
      `verbose` extras. Use this when the caller just needs "who am I and is
      my binding trustworthy" without the full self-description.
    """
    identity_status = "created" if is_new else ("reactivated" if was_archived else "resumed")
    identity_context = build_identity_response_context(
        agent_uuid=agent_uuid,
        agent_id=structured_agent_id,
        display_name=agent_label,
        session_resolution_source=continuity_source,
        identity_status=identity_status,
        identity_resolution_outcome=identity_resolution_outcome,
        client_hint=client_hint,
        model_type=None,
        proof_origin=proof_origin,
    )
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

    if response_mode == "minimal":
        # #734: lean envelope — one assurance block, no nested ontology,
        # no verbose extras. Functional fields (continuity_token, lineage
        # flags, thread/predecessor context) are kept because downstream
        # gates and callers derive from them; the self-description is dropped.
        minimal: dict = {
            "success": True,
            "welcome": welcome,
            "uuid": agent_uuid,
            "agent_id": structured_agent_id,
            "display_name": agent_label,
            "is_new": is_new,
            "client_session_id": stable_session_id,
            "identity_assurance": identity_context["identity_assurance"],
            "next_step": "Call process_agent_update with response_text describing your work",
            "provisional_lineage": bool(provisional_lineage),
            "response_mode": "minimal",
        }
        if identity_resolution_outcome:
            minimal["identity_resolution_outcome"] = identity_resolution_outcome
        if lineage_state is not None:
            minimal["lineage_state"] = lineage_state
        if continuity_token:
            minimal["continuity_token"] = continuity_token
        if thread_context:
            minimal["thread_context"] = thread_context
        if was_archived:
            minimal["auto_resumed"] = True
            minimal["previous_status"] = "archived"
        trajectory_block = _build_trajectory_block(trajectory_result)
        if trajectory_block is not None:
            minimal["trajectory"] = trajectory_block
        # Derived principal (octopus) — advisory/display-only, fail-open, never
        # a credential. Absent for singletons / pre-reconcile.
        _principal = _principal_lookup(agent_uuid)
        if _principal:
            minimal["principal"] = _principal
        return minimal

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
        "identity_context": identity_context,
        "identity_assurance": identity_context["identity_assurance"],
        "date_context": {"date": datetime.now().strftime("%Y-%m-%d"), "source": "mcp-server"},
        "next_step": "Call process_agent_update with response_text describing your work",
    }
    if identity_resolution_outcome:
        result["identity_resolution_outcome"] = identity_resolution_outcome
    # S1-a (2026-04-24): surface ownership_proof_version at top level so log
    # consumers and dashboards don't have to dig into the token payload or
 # rely on verbose=True.
    if continuity_support.get("ownership_proof_version") is not None:
        result["ownership_proof_version"] = continuity_support["ownership_proof_version"]
    # R2 PR 3 (2026-05-04): surface honest-memory lineage state at top
    # level. `lineage_state` ∈ {"provisional", "rejected_cross_role",
    # "no_lineage_declared", None}; `provisional_lineage` mirrors the
    # storage column so downstream gates (trust-tier, KG provenance,
    # R3 baselines) can be derived from the response without a follow-up
 # query. .
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
                "Use client_session_id for continuity within this process. "
                "continuity_token is only for proof-owned PATH 0 rebinds "
                "with agent_uuid."
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

    trajectory_block = _build_trajectory_block(trajectory_result)
    if trajectory_block is not None:
        result["trajectory"] = trajectory_block

    return result


def _build_trajectory_block(trajectory_result: Optional[dict]) -> Optional[dict]:
    """Build the onboard `trajectory` block (genesis + glossary trust_tier).

    Shared by the minimal and full onboard envelopes so the genesis/trust-tier
    info an agent sees once at onboard stays identical across modes (#734).
    Returns ``None`` when there is no trajectory result to surface.
    """
    if not trajectory_result:
        return None
    from src.governance_glossary import explain_trust_tier
    block = dict(trajectory_result)
    # #428: glossary-sourced trust_tier with meaning + criteria.
    # Preserves prior {tier, name, reason} shape and adds the explanation.
    block["trust_tier"] = explain_trust_tier({
        "tier": 1,
        "name": "emerging",
        "reason": "Genesis stored at onboard. Identity will mature with behavioral consistency.",
    })
    return block


def build_identity_response_context(
    *,
    agent_uuid: str,
    agent_id: str,
    display_name: Optional[str],
    session_resolution_source: Optional[str],
    identity_status: Optional[str],
    identity_resolution_outcome: Optional[str] = None,
    client_hint: Optional[str] = None,
    model_type: Optional[str] = None,
    proof_origin: Optional[str] = None,
) -> Dict[str, Any]:
    """Build S22 response annotation for identity/onboard payloads.

    This is response-shape metadata, not an authentication decision. It makes
    the ontology explicit for clients: UUID is the registry anchor; agent_id
    is a public/structured handle in identity responses; display_name is
    social/cosmetic; harness/model context is descriptive; assurance is the
    strength of the session-resolution signal.
    """
    source_key = _normalize_source(session_resolution_source)
    identity_assurance = _identity_assurance_from_source(source_key, proof_origin)
    continuity_claim = _continuity_claim(
        source_key,
        identity_status=identity_status,
        identity_resolution_outcome=identity_resolution_outcome,
    )

    context = {
        "schema": S22_IDENTITY_RESPONSE_SCHEMA,
        "identity_is": "uuid",
        "label_is": "social_or_cosmetic",
        "agent_id_is": "public_structured_handle",
        "harness_is": "context_not_identity_proof",
        "continuity_claim": continuity_claim,
        "identity_assurance": identity_assurance,
        "registry": {
            "uuid": agent_uuid,
            "role": "registry_anchor",
            "is_identity_key": True,
        },
        "public_handle": {
            "agent_id": agent_id,
            "role": "public_structured_handle",
            "is_identity_key": False,
        },
        "label": {
            "display_name": display_name,
            "role": "social_or_cosmetic",
            "is_identity_key": False,
        },
        "harness_context": {
            "harness_type": _normalize_optional_text(client_hint) or "unknown",
            "model": _normalize_optional_text(model_type),
            "role": "descriptive_context",
            "is_identity_proof": False,
        },
    }
    if identity_status:
        context["identity_status"] = identity_status
    if identity_resolution_outcome:
        context["identity_resolution_outcome"] = identity_resolution_outcome
    return context


_STICKY_CACHE_PREFIX = "sticky_cache:"
_DECAY_BY_ONE = {"strong": "medium", "medium": "weak"}
_TIER_PAYLOADS = {
    "strong": (1.0, "cryptographic, explicit stable session, or direct UUID proof path"),
    "medium": (0.7, "session continuity source with weaker explicit proof"),
    "weak": (0.35, "heuristic, fallback, or unknown session source"),
}


def _tier_for_source(source_key: str) -> str:
    if source_key in _STRONG_IDENTITY_SOURCES:
        return "strong"
    if source_key in _MEDIUM_IDENTITY_SOURCES:
        return "medium"
    return "weak"


def _how_to_strengthen(
    tier: str,
    source_key: str,
    proof_origin: Optional[str],
) -> Optional[str]:
    """One-line breadcrumb telling the agent how to reach a higher tier (#732).

    Surfaces the *transition*, not just the state: a `weak`/`medium` binding
    already carries the `reason` for being where it is, but nothing told the
    agent what to do about it. Returns ``None`` for `strong` (no action
    needed) so the assurance block stays lean (#734). Mirrored in
    `mcp_handlers/updates/phases.py` so the read- and write-path assurance
    blocks agree.
    """
    if tier == "strong":
        return None
    if proof_origin == "server_inferred":
        return (
            "binding was server-inferred (not caller-proven); pass the "
            "client_session_id from your onboard response explicitly in each "
            "call to reach strong"
        )
    if tier == "medium":
        return "pass an explicit client_session_id in each call to reach strong"
    # weak
    return (
        "pass the client_session_id from your onboard response in each call to "
        "reach strong; cross-process resumes need continuity_token + agent_uuid "
        "(PATH 0)"
    )


def _identity_assurance_from_source(
    source_key: str,
    proof_origin: Optional[str] = None,
) -> Dict[str, Any]:
    """Compute assurance tier from a session-resolution source.

    Recognizes the S3 `sticky_cache:<original>` envelope: the original
    proof source is mapped to its tier, then decayed one step
    (strong→medium, medium→weak, weak→weak). Mirrors the same logic in
    `mcp_handlers/updates/phases.py:_compute_identity_assurance` so the
    identity() response and update-path tier stay consistent.

    `proof_origin` ('caller_asserted' | 'server_inferred' | None) is
    authoritative over the source label (#679). A server-inferred binding
    (transport-injected CSID, fingerprint pin, context fallback) is NEVER
    strong, no matter what source label resolution stamped on it — this is
    what closes the laundering path where an injected `ip_ua_fingerprint`
    wore the `explicit_client_session_id` label and was reported strong/1.0.
    `None` (unknown origin) leaves the tier unchanged so legacy callers that
    don't thread provenance keep today's behavior (fail-open), exactly like
    the write-path `_compute_identity_assurance`.
    """
    if proof_origin == "server_inferred":
        # Authoritative downgrade — a server-guessed binding is weak proof no
        # matter what source label resolution stamped on it (#679).
        tier = "weak"
        score, _ = _TIER_PAYLOADS[tier]
        reason = f"server-inferred binding ('{source_key}'); not caller-proven"
        caller_proven = False
        resolved_origin = proof_origin
    else:
        caller_proven = (proof_origin == "caller_asserted")
        resolved_origin = proof_origin or "unknown"
        if source_key.startswith(_STICKY_CACHE_PREFIX):
            original_key = source_key[len(_STICKY_CACHE_PREFIX):] or "unknown"
            original_tier = _tier_for_source(original_key)
            tier = _DECAY_BY_ONE.get(original_tier, original_tier)
            score, _ = _TIER_PAYLOADS[tier]
            if original_tier == tier:
                reason = (
                    f"cache hit; original proof '{original_key}' was {original_tier} "
                    "(no further decay)"
                )
            else:
                reason = (
                    f"cache hit; original proof '{original_key}' was {original_tier}, "
                    f"decayed one tier to {tier} for per-call proof absence"
                )
        else:
            tier = _tier_for_source(source_key)
            score, reason = _TIER_PAYLOADS[tier]

    assurance: Dict[str, Any] = {
        "tier": tier,
        "score": score,
        "session_source": source_key,
        "trajectory_confidence": None,
        "reason": reason,
        "caller_proven": caller_proven,
        "proof_origin": resolved_origin,
    }
    hint = _how_to_strengthen(tier, source_key, proof_origin)
    if hint:
        assurance["how_to_strengthen"] = hint
    return assurance


def _continuity_claim(
    source_key: str,
    *,
    identity_status: Optional[str],
    identity_resolution_outcome: Optional[str],
) -> str:
    outcome = _normalize_source(identity_resolution_outcome)
    status = _normalize_source(identity_status)
    if outcome == "minted_after_resume_miss":
        return "fresh_uuid_minted_after_resume_miss"
    if outcome == "minted_force_new":
        return "fresh_uuid_minted_by_force_new"
    if outcome == "minted_fresh" or status == "created":
        return "fresh_uuid_minted"
    if source_key == "continuity_token":
        return "resumed_by_continuity_token"
    if source_key in {"agent_uuid_direct", "agent_uuid_direct_fastpath"}:
        return source_key.replace("agent_uuid", "resumed_by_uuid")
    if source_key in {
        "client_session_id",
        "explicit_client_session_id",
        "explicit_client_session_id_scoped",
    }:
        return "resumed_by_explicit_session"
    if source_key in {"mcp_session_id", "x_session_id", "oauth_client_id", "x_client_id"}:
        return "resumed_by_transport_session"
    if source_key == "pinned_onboard_session":
        return "resumed_by_recent_onboard_pin"
    if status == "reactivated":
        return "resumed_archived_identity_reactivated"
    if status == "resumed":
        return "resumed_source_unknown"
    return "heuristic_or_fallback_resolution"


def _normalize_source(value: Optional[str]) -> str:
    text = _normalize_optional_text(value)
    return text.lower() if text else "unknown"


def _normalize_optional_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
