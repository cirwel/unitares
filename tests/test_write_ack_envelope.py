"""Write acknowledgements omit the repeated canonical payload by default.

#2332 bounded the read aliases and routine sync_state. The finding and outcome
write aliases (store_finding, update_finding, record_result) still repeated
their canonical payload under raw_governance, which was most of each ack. These
pin the new default, the ids and warnings lifted in its place, and the explicit
full-response escape hatch. start_session is out of scope here (its ack shape is
decided with the identity/onboarding surface), so one test pins that this change
leaves it as it was.

Kept apart from test_agent_experience_envelope.py so concurrent envelope work
appending to that file does not collide with this one.
"""

from __future__ import annotations

import json

import pytest
from mcp.types import TextContent

from src.mcp_handlers.middleware import DispatchContext
from src.mcp_handlers.middleware.envelope_step import (
    apply_experience_envelope,
    build_experience_envelope,
)


def _result(payload) -> list:
    return [TextContent(type="text", text=json.dumps(payload))]


def _ctx(original_name: str) -> DispatchContext:
    return DispatchContext(original_name=original_name)


def _parse(result) -> dict:
    return json.loads(result[0].text)


_UUID = "7750bf80-20ad-4108-a952-5271b73845b8"
_SID = "agent-7750bf80-20a"
_TOKEN = "v1.payloadpayloadpayloadpayload.sigsignaturesignaturesignature"


def _onboard_payload() -> dict:
    """A default (minimal-mode) onboard ack, built by the real builder and
    wrapped the way handle_onboard_v2 wraps it (success_response with
    lite_response, which adds server_time and no agent_signature).

    Used only to pin that start_session's ack is untouched by this change.
    """
    from src.mcp_handlers.response_base import success_response
    from src.services.identity_payloads import build_onboard_response_data

    data = build_onboard_response_data(
        agent_uuid=_UUID,
        response_agent_id="Claude_Opus_20260925",
        agent_label="canary_dialectic_probe",
        stable_session_id=_SID,
        is_new=True,
        force_new=True,
        client_hint="claude_code",
        was_archived=False,
        trajectory_result=None,
        parent_agent_id=None,
        thread_context=None,
        verbose=False,
        continuity_source="ip_ua_fingerprint",
        continuity_support={"enabled": True},
        continuity_token=_TOKEN,
        system_activity=None,
        tool_mode_info=None,
        identity_resolution_outcome="minted_force_new",
        lineage_state="no_lineage_declared",
        proof_origin="server_inferred",
        response_mode="minimal",
    )
    wrapped = success_response(
        data, agent_id=_UUID, arguments={"lite_response": True}
    )
    return json.loads(wrapped[0].text)


def _store_payload() -> dict:
    return {
        "success": True,
        "message": "Discovery stored for agent 'probe'",
        "discovery_id": "d-new",
        "agent": {"agent_id": "probe", "display_name": "probe"},
        "discovery": {
            "id": "d-new",
            "type": "bug_found",
            "status": "open",
            "severity": "medium",
            "summary": "write ack bug",
            "related_to": ["d-old-1", "d-old-2"],
        },
        "related_discoveries": [{"id": "d-old-1", "summary": "x" * 300}],
        "_supersedes_warning": "supersedes target 'd-gone' not found",
        "_truncated": {"summary": True},
        "agent_signature": {"uuid": _UUID, "identity_context": {"x": "y" * 400}},
    }


def _update_payload() -> dict:
    return {
        "success": True,
        "message": "Discovery 'd-existing' status updated to 'resolved'",
        "discovery": {"id": "d-existing", "status": "resolved", "type": "bug_found"},
        "closure_class": None,
        "closure_class_note": "This closure declares no standard.",
        "agent_signature": {"uuid": _UUID},
    }


def _outcome_payload() -> dict:
    return {
        "success": True,
        "outcome_id": "o-9",
        "outcome_type": "test_passed",
        "is_bad": False,
        "outcome_score": 1.0,
        "eisv_snapshot": {"primary_eisv": {"E": 0.7, "I": 0.6, "S": 0.2, "V": 0.0}},
        "prediction_binding": "registry",
        "prediction_source": "registry",
        "calibration_excluded": False,
        "unverified_fields": ["pr"],
        "verified_fields": [],
        "idempotent_replay": True,
        "identity_warnings": [{"code": "ephemeral_writer", "message": "ephemeral"}],
        "agent_signature": {"uuid": _UUID},
    }


_WRITE_CASES = [
    ("store_finding", "knowledge", _store_payload, {"summary": "write ack bug"}),
    ("update_finding", "knowledge", _update_payload, {"discovery_id": "d-existing"}),
    ("record_result", "outcome_event", _outcome_payload, {}),
]


def _wire_bytes(value: dict) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


_FINDING_WRITES = ("store_finding", "update_finding")


def _full_form_bytes(friendly, canonical, make, args) -> int:
    """Bytes of the response that carries the whole payload.

    record_result: the alias with response_mode='full'.
    The finding writes have no full request at the alias (see
    test_finding_write_response_mode_is_not_a_full_request), so their full
    form is the ack as it was before the omission: the same envelope with
    the canonical payload under raw_governance.
    """
    if friendly in _FINDING_WRITES:
        payload = make()
        env = build_experience_envelope(friendly, canonical, payload, args)
        env.pop("raw_governance_available", None)
        env.pop("raw_governance_hint", None)
        env["raw_governance"] = payload
        return _wire_bytes(env)
    return _wire_bytes(build_experience_envelope(
        friendly, canonical, make(), {**args, "response_mode": "full"}
    ))


@pytest.mark.parametrize("friendly,canonical,make,args", _WRITE_CASES)
def test_write_ack_omits_raw_governance_by_default(friendly, canonical, make, args):
    payload = make()
    env = build_experience_envelope(friendly, canonical, payload, args)

    assert "raw_governance" not in env
    assert env["raw_governance_available"] is True
    assert env["success"] is True
    assert env["tool"] == friendly
    assert env["next_action"]
    # The hint names a read or an explicit full request, never "re-call" the
    # write: a repeated store mints a second finding.
    hint = env["raw_governance_hint"]
    assert "Re-call" not in hint
    # The ack no longer carries the canonical payload's bulk.
    assert _wire_bytes(env) < _full_form_bytes(friendly, canonical, make, args)
    assert "agent_signature" not in json.dumps(env)


@pytest.mark.parametrize("friendly,canonical,make,args", _WRITE_CASES[:1])
def test_write_ack_is_under_half_its_full_form(friendly, canonical, make, args):
    """store_finding carries the bulk (signature, related findings); the
    default ack sheds most of it."""
    env = build_experience_envelope(friendly, canonical, make(), args)
    assert _wire_bytes(env) * 2 < _full_form_bytes(friendly, canonical, make, args)


@pytest.mark.parametrize(
    "friendly,canonical,make,args",
    [case for case in _WRITE_CASES if case[0] not in _FINDING_WRITES],
)
def test_write_ack_full_mode_restores_raw_governance(friendly, canonical, make, args):
    payload = make()
    env = build_experience_envelope(
        friendly, canonical, payload, {**args, "response_mode": "full"}
    )
    assert env["raw_governance"] is payload
    assert "raw_governance_available" not in env
    assert "raw_governance_hint" not in env


@pytest.mark.parametrize(
    "friendly,canonical,make,args",
    [case for case in _WRITE_CASES if case[0] in _FINDING_WRITES],
)
def test_finding_write_response_mode_is_not_a_full_request(
    friendly, canonical, make, args
):
    """KnowledgeParams fills response_mode='full' by default, so after
    validation an explicit 'full' and an omitted one are indistinguishable.
    Honouring it would keep raw_governance on every finding write."""
    env = build_experience_envelope(
        friendly, canonical, make(), {**args, "response_mode": "full"}
    )
    assert "raw_governance" not in env
    assert "knowledge(action='details'" in env["raw_governance_hint"]


def test_include_semantics_restores_raw_governance():
    """outcome_event's include_semantics already meant 'give me everything';
    it keeps meaning it at the friendly surface, and it survives /mcp/."""
    env = build_experience_envelope(
        "record_result", "outcome_event", _outcome_payload(), {"include_semantics": True}
    )
    assert "raw_governance" in env


def test_knowledge_write_acks_keep_ids_and_write_warnings():
    env = build_experience_envelope(
        "store_finding", "knowledge", _store_payload(), {"summary": "write ack bug"}
    )
    assert env["discovery_id"] == "d-new"
    assert env["state_summary"]["related_to"] == ["d-old-1", "d-old-2"]
    assert env["_supersedes_warning"] == "supersedes target 'd-gone' not found"
    assert env["_truncated"] == {"summary": True}
    assert "knowledge(action='details', discovery_id='d-new')" in env["raw_governance_hint"]

    env = build_experience_envelope(
        "update_finding", "knowledge", _update_payload(), {"discovery_id": "d-existing"}
    )
    assert env["discovery_id"] == "d-existing"
    assert env["closure_class"] is None
    assert env["closure_class_note"] == "This closure declares no standard."


def test_store_ack_omits_empty_related_to():
    payload = _store_payload()
    payload["discovery"]["related_to"] = []
    env = build_experience_envelope("store_finding", "knowledge", payload, {})
    assert "related_to" not in env["state_summary"]


def test_record_result_ack_keeps_outcome_id_and_disclosures():
    env = build_experience_envelope("record_result", "outcome_event", _outcome_payload(), {})
    summary = env["state_summary"]
    assert summary["outcome_id"] == "o-9"
    assert summary["is_bad"] is False
    assert summary["idempotent_replay"] is True
    assert summary["unverified_fields"] == ["pr"]
    assert "verified_fields" not in summary
    # A handler-side identity warning survives the omitted payload.
    assert env["identity_warnings"] == [{"code": "ephemeral_writer", "message": "ephemeral"}]


@pytest.mark.parametrize(
    "friendly,canonical,payload,args",
    [
        # A batch or otherwise unrecognised knowledge write has no single id to
        # lift, so the canonical payload is the only record of what happened.
        ("store_finding", "knowledge", {"success": True, "stored": [1, 2]}, {}),
        ("record_result", "outcome_event", {"success": True, "outcome_type": "x"}, {}),
    ],
)
def test_write_ack_without_a_liftable_id_keeps_raw_governance(
    friendly, canonical, payload, args
):
    env = build_experience_envelope(friendly, canonical, payload, args)
    assert env["raw_governance"] is payload


def test_request_review_ack_still_carries_raw_governance():
    """The review ack carries the review itself (resolution, dispatch,
    thesis-failure flags), and the dialectic canary reads those fields there."""
    payload = {
        "success": True,
        "session_id": "sess-1",
        "phase": "resolved",
        "one_call_review": True,
        "thesis_recorded": True,
        "resolution": {"action": "resume", "conditions": ["c1"]},
    }
    env = build_experience_envelope("request_review", "dialectic", payload, {})
    assert env["raw_governance"] is payload


@pytest.mark.asyncio
@pytest.mark.parametrize("invoked,canonical", [
    ("store_finding", "knowledge"),
    ("update_finding", "knowledge"),
    ("record_result", "outcome_event"),
])
async def test_success_shaped_identity_refusal_on_a_write_stays_whole(invoked, canonical):
    """A typed identity refusal is success-shaped. It must reach the caller as
    its own contract, top level, with nothing omitted and no omission hint."""
    from src.mcp_handlers.identity_bootstrap import strict_identity_refusal_payload
    from src.mcp_handlers.response_base import success_response

    raw = success_response(strict_identity_refusal_payload(canonical))
    refusal = json.loads(raw[0].text)
    assert refusal["success"] is True and "error" not in refusal

    out = await apply_experience_envelope(canonical, {}, _ctx(invoked), raw)
    data = _parse(out)
    assert data["status"] == "identity_required"
    for field in ("hint", "next_step", "safe_options", "do_not", "rollout_flag"):
        assert data[field] == refusal[field], field
    assert data["tool"] == invoked
    assert "next_action" not in data
    assert "raw_governance_hint" not in data
    assert "raw_governance_available" not in data


@pytest.mark.asyncio
async def test_write_error_passes_through_untouched():
    raw = _result({"success": False, "error": "Authentication required", "error_code": "AUTH_REQUIRED"})
    out = await apply_experience_envelope("knowledge", {}, _ctx("store_finding"), raw)
    assert out is raw


_MCP_WRITE_ARGS = [
    ("store_finding", {"summary": "write ack bug"}, False),
    ("update_finding", {"discovery_id": "d-existing"}, False),
    ("record_result", {"outcome_type": "task_completed"}, True),
]


@pytest.mark.parametrize("friendly,args,declares_full", _MCP_WRITE_ARGS)
def test_hint_names_only_a_full_route_the_mcp_transport_delivers(
    friendly, args, declares_full
):
    """The escape hatch the hint names must survive /mcp/ argument validation.

    FastMCP validates alias arguments against the registered argument model
    and silently discards undeclared keys. store_finding and update_finding
    keep a narrowed schema (ALIAS_SCHEMA_KEEP) that declares no response_mode,
    so over /mcp/ a response_mode='full' on them never reaches the envelope.
    On REST and stdio it reaches validation, but KnowledgeParams defaults it
    to 'full' anyway, so the envelope cannot tell it from an omitted one and
    does not treat it as a request (see
    test_finding_write_response_mode_is_not_a_full_request). Their hint
    therefore names a knowledge details read, which works on every transport.
    """
    from src import mcp_server

    tool = mcp_server.mcp._tool_manager.get_tool(friendly)
    validated = tool.fn_metadata.arg_model.model_validate(
        {**args, "response_mode": "full"}
    ).model_dump_one_level()
    assert (validated.get("response_mode") == "full") is declares_full

    make = {case[0]: case[2] for case in _WRITE_CASES}[friendly]
    canonical = {case[0]: case[1] for case in _WRITE_CASES}[friendly]
    hint = build_experience_envelope(friendly, canonical, make(), args)[
        "raw_governance_hint"
    ]
    if declares_full:
        # Named for a later outcome, after the warning not to repeat this one.
        assert "response_mode='full'" in hint
        assert hint.startswith("Do not repeat this outcome")
    else:
        assert "response_mode" not in hint
        assert "knowledge(action='details'" in hint


@pytest.mark.parametrize("friendly,args,older_flag,delivered", [
    ("record_result", {"outcome_type": "task_completed"}, "include_semantics", True),
])
def test_older_full_spellings_as_the_mcp_transport_delivers_them(
    friendly, args, older_flag, delivered
):
    """Pin which older full-mode spelling survives /mcp/ validation, so the
    docs and changelog cannot claim one works there when it is dropped."""
    from src import mcp_server

    tool = mcp_server.mcp._tool_manager.get_tool(friendly)
    validated = tool.fn_metadata.arg_model.model_validate(
        {**args, older_flag: True}
    ).model_dump_one_level()
    assert (validated.get(older_flag) is True) is delivered

    make = {case[0]: case[2] for case in _WRITE_CASES}[friendly]
    canonical = {case[0]: case[1] for case in _WRITE_CASES}[friendly]
    env = build_experience_envelope(friendly, canonical, make(), validated)
    assert ("raw_governance" in env) is delivered


# -- through the real dispatch steps ----------------------------------------

_PIPELINE_CASES = [
    ("store_finding", {"summary": "write ack bug", "discovery_type": "bug_found"}, _store_payload),
    ("update_finding", {"discovery_id": "d-existing", "status": "resolved"}, _update_payload),
    ("record_result", {"outcome_type": "task_completed"}, _outcome_payload),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("friendly,args,make", _PIPELINE_CASES)
async def test_write_ack_omits_raw_governance_after_real_validation(friendly, args, make):
    """The envelope step sees arguments after resolve_alias and
    validate_params, not what the caller sent. Canonical validation fills
    schema defaults (KnowledgeParams.response_mode='full'), so a test that
    hands build_experience_envelope the caller's arguments cannot see a
    default that flips the policy. This one runs the real steps."""
    from src.mcp_handlers.middleware.params_step import resolve_alias, validate_params

    ctx = DispatchContext()
    resolved = await resolve_alias(friendly, dict(args), ctx)
    assert isinstance(resolved, tuple), resolved
    name, arguments, ctx = resolved
    validated = await validate_params(name, arguments, ctx)
    assert isinstance(validated, tuple), validated
    name, arguments, ctx = validated

    out = await apply_experience_envelope(name, arguments, ctx, _result(make()))
    data = _parse(out)
    assert data["tool"] == friendly
    assert "raw_governance" not in data, arguments
    assert data["raw_governance_available"] is True


def _real_signature(
    session_resolution_source: str = "ip_ua_fingerprint",
    proof_origin: str = "server_inferred",
) -> dict:
    """agent_signature as success_response attaches it to a write."""
    from src.services.identity_payloads import build_identity_signature_payload

    return build_identity_signature_payload(
        agent_uuid=_UUID,
        agent_id="Claude_Opus_20260925",
        display_name="probe",
        label_source="claimed",
        session_resolution_source=session_resolution_source,
        proof_origin=proof_origin,
    )


@pytest.mark.parametrize(
    "friendly,canonical,make,args",
    [case for case in _WRITE_CASES if case[0] in _FINDING_WRITES],
)
def test_write_ack_says_which_identity_it_was_recorded_under(
    friendly, canonical, make, args
):
    """The finding payloads carry attribution only in agent_signature, and
    params_step injects the resolved agent_id, so even a server-inferred
    (weakly bound) caller's write is signed. It must see who it wrote as."""
    payload = make()
    payload["agent_signature"] = _real_signature()
    env = build_experience_envelope(friendly, canonical, payload, args)
    assert "raw_governance" not in env
    assert env["agent_uuid"] == _UUID
    assert env["written_as"] == {
        "agent_id": "Claude_Opus_20260925",
        "display_name": "probe",
        "tier": "weak",
        "caller_proven": False,
        "proof_origin": "server_inferred",
    }
    assert "identity_context" not in json.dumps(env)


def test_record_result_names_its_writer_only_from_a_signature_it_carries():
    """outcome_event calls success_response without arguments, so
    compute_agent_signature returns {"uuid": None} for a server-inferred
    binding; the ack then names no writer. A caller-asserted binding is
    signed and the ack says who wrote it."""

    weak = _outcome_payload()
    weak["agent_signature"] = {"uuid": None}
    env = build_experience_envelope("record_result", "outcome_event", weak, {})
    assert "agent_uuid" not in env
    assert "written_as" not in env

    proven = _outcome_payload()
    proven["agent_signature"] = _real_signature(
        session_resolution_source="explicit_client_session_id",
        proof_origin="caller_asserted",
    )
    env = build_experience_envelope("record_result", "outcome_event", proven, {})
    assert env["agent_uuid"] == _UUID
    assert env["written_as"]["tier"] == "strong"
    assert env["written_as"]["caller_proven"] is True


@pytest.mark.parametrize("value,keeps_raw", [
    (True, True), ("true", True), ("t", True), ("y", True), ("1", True),
    (False, False), ("on", False), ("false", False), (None, False),
])
def test_include_semantics_is_read_the_way_the_handler_reads_it(value, keeps_raw):
    """The ack keeps raw_governance exactly when the handler built the full
    snapshot: both read include_semantics with the handler's coercion."""
    from src.mcp_handlers.observability.outcome_events import _coerce_bool_flag

    assert _coerce_bool_flag(value) is keeps_raw
    env = build_experience_envelope(
        "record_result", "outcome_event", _outcome_payload(), {"include_semantics": value}
    )
    assert ("raw_governance" in env) is keeps_raw


def test_write_ack_without_a_proven_signature_invents_no_writer():
    payload = _update_payload()
    payload["agent_signature"] = {"uuid": None}
    env = build_experience_envelope("update_finding", "knowledge", payload, {})
    assert "agent_uuid" not in env
    assert "written_as" not in env


def test_write_ack_keeps_the_auto_correction_notice():
    from src.mcp_handlers.response_base import success_response

    coercions = {"confidence": {"from": "0.5", "to": 0.5}}
    wrapped = success_response(
        _outcome_payload(), agent_id=None, arguments={"_param_coercions": coercions}
    )
    payload = json.loads(wrapped[0].text)
    assert payload["_param_coercions"]["applied"] == coercions
    env = build_experience_envelope("record_result", "outcome_event", payload, {})
    assert env["_param_coercions"] == payload["_param_coercions"]


# -- start_session is out of scope ------------------------------------------

@pytest.mark.asyncio
async def test_start_session_ack_is_untouched_by_the_write_ack_change(monkeypatch):
    """This change reshapes only the finding and outcome write acks. Through
    the real alias and validation steps, start_session's ack is the same with
    or without the write-ack policy, and today that ack still carries the
    canonical payload under raw_governance by default. (Its shape belongs to
    the identity/onboarding surface; if that surface changes the default, the
    last assertion follows it, and the equality above still has to hold.)"""
    from src.mcp_handlers.middleware import envelope_step
    from src.mcp_handlers.middleware.params_step import resolve_alias, validate_params

    assert "start_session" not in envelope_step._COMPACT_WRITE_ALIASES

    ctx = DispatchContext()
    name, arguments, ctx = await resolve_alias("start_session", {"force_new": True}, ctx)
    name, arguments, ctx = await validate_params(name, arguments, ctx)
    payload = _onboard_payload()

    async def _ack() -> dict:
        return _parse(await apply_experience_envelope(
            name, dict(arguments), ctx, _result(payload)
        ))

    with_policy = await _ack()
    monkeypatch.setattr(envelope_step, "_COMPACT_WRITE_ALIASES", frozenset())
    without_policy = await _ack()
    assert with_policy == without_policy
    assert with_policy["tool"] == "start_session"
    assert with_policy["raw_governance"] == payload
