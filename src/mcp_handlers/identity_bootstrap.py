"""Identity-bootstrap policy helpers for #425.

Centralizes the STRICT_IDENTITY_REQUIRED env-flag check so every auto-mint
path checks the same gate the same way. Without this, the gate drifts
(one path checks "true", another "1", another normalizes case differently)
and the rollout becomes a per-path negotiation instead of a single switch.
"""

from __future__ import annotations

import os


_TRUTHY = frozenset({"1", "true", "yes", "on"})


def is_strict_identity_required() -> bool:
    """True iff STRICT_IDENTITY_REQUIRED env var is set to a truthy value.

    Truthy values: "1", "true", "yes", "on" (case-insensitive). Anything
    else, including unset, is False.

    When True, all auto-mint paths MUST refuse-or-skip rather than create
    an ephemeral identity. See CLAUDE.md "STRICT_IDENTITY_REQUIRED (#425
    staged rollout)" for the contract and rollout sequence.
    """
    raw = os.getenv("STRICT_IDENTITY_REQUIRED", "").strip().lower()
    return raw in _TRUTHY


_DEFAULT_REFUSAL_HINT = (
    "This tool works once you have a governance identity — call onboard() to "
    "mint one and it's yours to use. If you're continuing work handed off from "
    "an exited session, pass parent_agent_id to declare lineage; otherwise "
    "pass force_new=true for a fresh identity."
)
_DEFAULT_REFUSAL_NEXT_STEP = (
    "Call onboard(force_new=true) to mint a fresh process identity, or pass "
    "parent_agent_id only for a real handoff from an exited predecessor."
)
_DEFAULT_REFUSAL_SAFE_OPTIONS = (
    {
        "action": "start_fresh",
        "call": "onboard(force_new=true)",
        "when": "This is a new process-instance with no causal predecessor.",
    },
    {
        "action": "declare_lineage",
        "call": "onboard(force_new=true, parent_agent_id=<prior UUID>, spawn_reason='explicit')",
        "when": "A finished predecessor explicitly handed this work to you.",
    },
    {
        "action": "stay_read_only",
        "call": "get_governance_metrics() or list_tools()",
        "when": "You do not yet have caller-proven identity for a write.",
    },
)
_DEFAULT_REFUSAL_DO_NOT = (
    "Do not retry bare identity(agent_uuid=..., resume=true); UUID alone is not ownership proof.",
)


# ─── Recovery wording for a process that may already hold an identity ───
#
# One source for every surface that tells a caller how to get back to its own
# identity: the unbound metrics read (core._unbound_next_action), the strict
# write refusal for a server-inferred binding and the identity_assurance
# breadcrumb (updates/phases.py, mirrored word for word by
# services/identity_payloads._how_to_strengthen), and the strict refusal for a
# call that resolved no identity (session_miss_refusal_options below). When
# the caller sent no id of its own, each leads with the client_session_id its
# own start_session returned, and a mint is offered only to a process that
# never called start_session: a process that already holds an identity and
# mints again splits its work across two. When the caller sent an id that
# names nothing, repeating it cannot help; the rebind leads there, and the
# mint is for a process with nothing on this server to rebind to.

# How a caller turns a server-inferred binding into a caller-proven one. The
# rebind names the id identity() returns: a retry without it resolves by
# transport inference again and is refused again.
CALLER_PROOF_REMEDY = (
    "pass the client_session_id returned by start_session explicitly on "
    "the next call; adapters may inject it automatically. If this transport "
    "cannot retain that binding, use identity(agent_uuid=..., "
    "continuity_token=..., resume=true) as an explicit same-live-process "
    "rebind and pass the client_session_id it returns, instead of attaching "
    "continuity_token to ordinary tool calls"
)

# A caller-sent id that resolves to nothing: repeating it cannot help.
SESSION_ID_NAMES_NO_IDENTITY = (
    "The client_session_id on this call names no identity on this server "
    "(never minted here, or its binding expired), so repeating it cannot help."
)

# The mint, with lineage only for a real handoff.
FRESH_MINT_STEP = (
    "start_session(force_new=true); add parent_agent_id=<prior_uuid>, "
    "spawn_reason='explicit' only to continue a finished predecessor's work."
)

REBIND_RETURNS_SESSION_ID = (
    "identity(agent_uuid=..., continuity_token=..., resume=true) returns it"
)

DO_NOT_MINT_A_SECOND_IDENTITY = (
    "If this process already called start_session, do not call "
    "start_session(force_new=true) to clear this refusal: a second identity "
    "splits this process's work from the first."
)


def session_miss_refusal_options(
    tool_name: str,
    *,
    caller_sent_session_id: bool,
) -> dict:
    """hint / next_step / safe_options / do_not for a call that resolved no
    identity under STRICT_IDENTITY_REQUIRED (``session_resolve_miss`` on
    /mcp/, an unbound required call on REST).

    The defaults above lead with ``onboard(force_new=true)``, which is right
    for the bare-onboard lineage refusal and wrong here: the most common
    caller of a required tool that resolves nothing is a process that called
    start_session and did not send its id on this call. Telling it to mint
    splits its work across two identities.

    Branches only on what the caller itself sent, as the unbound read does.
    With no client_session_id of its own on the call, the retry with that id
    leads and the mint is the branch for a process that never called
    start_session. With a caller-sent id that names nothing, repeating it
    cannot help, so the rebind leads, and the mint is for a process that
    cannot rebind (it never onboarded here, or lost its uuid and token).
    ``caller_sent_session_id`` must exclude an id the transport put on the
    call: that id is the server's inference, not the caller's.
    """
    retry_call = f"{tool_name}(..., client_session_id=<from start_session>)"
    retry_option = {
        "action": "retry_with_client_session_id",
        "call": retry_call,
        "when": (
            "This process called start_session and still has the "
            "client_session_id it returned."
        ),
    }
    rebind_option = {
        "action": "rebind_then_retry",
        "call": (
            "identity(agent_uuid=<uuid>, continuity_token=<token>, "
            f"resume=true), then {tool_name}(..., "
            "client_session_id=<from identity>)"
        ),
        "when": (
            "You lost the client_session_id but still hold this live "
            "process's uuid and continuity_token."
        ),
    }
    read_only_option = {
        "action": "stay_read_only",
        "call": "check_working_state(client_session_id=<from start_session>)",
        "when": (
            "You want to read state without writing. Without the "
            "client_session_id the read returns unbound."
        ),
    }
    mint_option = {
        "action": "start_session_first",
        "call": (
            f"start_session(force_new=true), then {tool_name}(..., "
            "client_session_id=<from start_session>)"
        ),
        "when": "This process never called start_session on this server.",
    }
    lineage_option = {
        "action": "declare_lineage",
        "call": (
            "start_session(force_new=true, parent_agent_id=<prior UUID>, "
            "spawn_reason='explicit')"
        ),
        "when": (
            "This process never called start_session, and a finished "
            "predecessor explicitly handed its work to you."
        ),
    }
    if caller_sent_session_id:
        # The id this process sent is gone (or was never minted here), so no
        # retry with it can succeed. A process that still holds its uuid and
        # continuity_token rebinds; one that cannot (never onboarded here, or
        # lost both) has no identity on this server to split, and mints.
        return {
            "hint": (
                SESSION_ID_NAMES_NO_IDENTITY
                + " Under strict identity nothing is minted for this call. "
                "If this process still holds its uuid and continuity_token, "
                "use identity(agent_uuid=..., continuity_token=..., "
                "resume=true) as an explicit same-live-process rebind and "
                "pass the client_session_id it returns. Otherwise mint one: "
                + FRESH_MINT_STEP
            ),
            "next_step": (
                "If this process still holds its uuid and continuity_token, "
                "rebind with identity(agent_uuid=..., continuity_token=..., "
                f"resume=true) and retry {tool_name} with the "
                "client_session_id it returns. Otherwise call "
                "start_session(force_new=true) first."
            ),
            "safe_options": (
                rebind_option,
                {
                    **mint_option,
                    "when": (
                        "This process cannot rebind: it never called "
                        "start_session on this server, or it no longer holds "
                        "its uuid and continuity_token."
                    ),
                },
                {
                    **lineage_option,
                    "when": (
                        "As start_session_first, and a finished predecessor "
                        "explicitly handed its work to you."
                    ),
                },
            ),
            "do_not": _DEFAULT_REFUSAL_DO_NOT,
        }
    return {
        "hint": (
            "No identity resolved for this call, and under strict identity "
            "nothing is minted for it: you sent no client_session_id, and "
            "nothing else on the call names a session this server knows. To "
            "retry, " + CALLER_PROOF_REMEDY + "."
        ),
        "next_step": (
            "If this process already called start_session, retry "
            f"{tool_name} with the client_session_id it returned. If you no "
            "longer have it, " + REBIND_RETURNS_SESSION_ID + ". If this "
            "process never called start_session, call "
            "start_session(force_new=true) first."
        ),
        "safe_options": (
            retry_option,
            rebind_option,
            read_only_option,
            mint_option,
            lineage_option,
        ),
        "do_not": (*_DEFAULT_REFUSAL_DO_NOT, DO_NOT_MINT_A_SECOND_IDENTITY),
    }


# The #425 typed refusal is the one success-SHAPED payload that is not a
# success: `strict_identity_refusal_payload` is deliberately "a structured
# success-shape, not an error" (see below), so it carries `success: true` with
# no `error` key. Every consumer that branches on success/error therefore
# misses it unless it checks this marker. `rollout_flag` is written by
# `strict_identity_refusal_payload` and by nothing else in the codebase, so it
# is a precise marker: no other success payload can false-positive on it.
IDENTITY_REFUSAL_MARKER = "STRICT_IDENTITY_REQUIRED"


def identity_refusal_status(payload: object) -> str | None:
    """Return the refusal ``status`` if ``payload`` is a #425 typed refusal.

    ``status`` varies by emission point (``identity_required``,
    ``lineage_declaration_required``, ...) and is a bounded server-authored
    literal. Returns None for anything else.

    Lives here, beside the builder, for the reason the builder itself is
    single-sourced: the shape and the test for the shape must not drift apart.
    Consumers: ``services/tool_usage_recorder.py`` (so a refusal is not counted
    as a successful call) and ``middleware/envelope_step.py`` (so a refusal is
    not rebuilt into an ordinary check-in).
    """
    if not isinstance(payload, dict):
        return None
    if payload.get("rollout_flag") != IDENTITY_REFUSAL_MARKER:
        return None
    status = payload.get("status")
    return str(status) if status else "identity_required"


def strict_identity_refusal_payload(
    tool_name: str,
    *,
    status: str = "identity_required",
    hint: str | None = None,
    next_step: str | None = None,
    safe_options: list[dict] | tuple[dict, ...] | None = None,
    do_not: list[str] | tuple[str, ...] | None = None,
    identity_assurance: dict | None = None,
    surface_context: dict | None = None,
) -> dict:
    """The #425 typed-refusal shape, single-sourced.

    Consumed by EVERY refusal emission point: the MCP dispatch middleware
    (identity_step.py, wrapped in success_response), the REST gate
    (http_tool_service.execute_http_tool, returned raw), the
    process_agent_update Path-C refusal (updates/phases.py, hint
    override), and the bare-onboard Path-B refusal (identity/handlers.py,
    status+hint overrides — its status is deliberately
    ``lineage_declaration_required``). Stage-1 burn-in (2026-06-11,
    docs/handoffs/strict-identity-stage1-burnin-2026-06-11.md) found the
    surfaces drifting — empty ontology_ref on two of them, divergent
    fields — so the shape lives here and the emission points cannot
    drift (same single-source discipline as core.unbound_metrics_payload).

    A structured success-shape, not an error: error responses invite
    retry-with-mint catch paths and would reintroduce the ghost leak.
    """
    payload = {
        "status": status,
        "tool": tool_name,
        "tool_class": "required",
        "hint": hint if hint is not None else _DEFAULT_REFUSAL_HINT,
        "next_step": next_step if next_step is not None else _DEFAULT_REFUSAL_NEXT_STEP,
        "safe_options": [
            dict(option) for option in (
                safe_options if safe_options is not None else _DEFAULT_REFUSAL_SAFE_OPTIONS
            )
        ],
        "do_not": list(do_not if do_not is not None else _DEFAULT_REFUSAL_DO_NOT),
        "ontology_ref": "docs/ontology/identity.md#operational-contract",
        "rollout_flag": "STRICT_IDENTITY_REQUIRED",
    }
    if identity_assurance is not None:
        payload["identity_assurance"] = dict(identity_assurance)
    if surface_context is not None:
        payload["surface_context"] = dict(surface_context)
    return payload
