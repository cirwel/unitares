"""Write acknowledgements omit the repeated canonical payload by default.

#2332 bounded the read aliases and routine sync_state. The write aliases still
repeated their canonical payload under raw_governance, which was most of each
ack. These pin the new default, the ids and warnings lifted in its place, the
explicit full-response escape hatch, and the readers that used to reach into
raw_governance for fields that now ride at the top level.

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

    Built rather than hand-written so the lift tests exercise the shape the
    server actually sends by default: minimal mode carries no
    session_resolution_source or continuity_token_supported, and a lift of a
    field the default never carries would pass against a hand-written dict.
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


def _onboard_payload_with_mint_signals() -> dict:
    """The default ack of a named fresh mint that hit onboard's failure paths.

    Each block is the exact shape its producer returns: resident_registration
    from onboard_classifier.resident_registration (off-roster name),
    label_renamed as handle_onboard_v2 writes it on a collision, bootstrap as
    bootstrap_checkin.write_bootstrap returns it when the write fails.
    """
    from src.grounding.onboard_classifier import resident_registration

    payload = _onboard_payload()
    payload["resident_registration"] = resident_registration(
        "vigil_probe", ["ephemeral"], roster=["vigil"]
    )
    payload["label_renamed"] = {
        "requested": "canary_dialectic_probe",
        "applied": "canary_dialectic_probe_2",
        "reason": "label_taken_by_active_agent",
        "detail": "'canary_dialectic_probe' is already held by another active agent.",
    }
    payload["bootstrap"] = {"written": False, "reason": "error", "detail": "TimeoutError"}
    return payload


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
    ("start_session", "onboard", _onboard_payload, {}),
    ("store_finding", "knowledge", _store_payload, {"summary": "write ack bug"}),
    ("update_finding", "knowledge", _update_payload, {"discovery_id": "d-existing"}),
    ("record_result", "outcome_event", _outcome_payload, {}),
]


def _wire_bytes(value: dict) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


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
    # write: a repeated store mints a second finding, a repeated
    # start_session(force_new) mints a second identity.
    hint = env["raw_governance_hint"]
    assert "Re-call" not in hint
    # The ack no longer carries the canonical payload's bulk.
    full = build_experience_envelope(
        friendly, canonical, make(), {**args, "response_mode": "full"}
    )
    assert _wire_bytes(env) < _wire_bytes(full)
    assert "agent_signature" not in json.dumps(env)


@pytest.mark.parametrize("friendly,canonical,make,args", _WRITE_CASES[:2])
def test_write_ack_is_under_half_its_full_form(friendly, canonical, make, args):
    """start_session and store_finding carry the bulk (identity ontology,
    signature, related findings); the default ack sheds most of it."""
    env = build_experience_envelope(friendly, canonical, make(), args)
    full = build_experience_envelope(
        friendly, canonical, make(), {**args, "response_mode": "full"}
    )
    assert _wire_bytes(env) * 2 < _wire_bytes(full)


@pytest.mark.parametrize("friendly,canonical,make,args", _WRITE_CASES)
def test_write_ack_full_mode_restores_raw_governance(friendly, canonical, make, args):
    payload = make()
    env = build_experience_envelope(
        friendly, canonical, payload, {**args, "response_mode": "full"}
    )
    assert env["raw_governance"] is payload
    assert "raw_governance_available" not in env
    assert "raw_governance_hint" not in env


def test_write_ack_older_full_spellings_restore_raw_governance():
    """onboard's verbose and outcome_event's include_semantics already meant
    'give me everything'; they keep meaning it where they reach the envelope
    (verbose on start_session reaches it over REST and stdio only; see
    test_older_full_spellings_as_the_mcp_transport_delivers_them)."""
    env = build_experience_envelope(
        "start_session", "onboard", _onboard_payload(), {"verbose": True}
    )
    assert "raw_governance" in env
    env = build_experience_envelope(
        "record_result", "outcome_event", _outcome_payload(), {"include_semantics": True}
    )
    assert "raw_governance" in env


def test_start_session_ack_keeps_identity_fields_top_level():
    env = build_experience_envelope("start_session", "onboard", _onboard_payload(), {})

    assert env["agent_uuid"] == _UUID
    assert env["client_session_id"] == _SID
    assert env["continuity_token"] == _TOKEN
    assert env["agent_id"] == "Claude_Opus_20260925"
    assert env["display_name"] == "canary_dialectic_probe"
    # Only the full-mode payload carries these, and a full-mode ack keeps
    # raw_governance; the default ack neither has nor invents them.
    assert "continuity_token_supported" not in env
    assert "session_resolution_source" not in env
    summary = env["state_summary"]
    assert summary["is_new"] is True
    assert summary["identity_resolution_outcome"] == "minted_force_new"
    assert summary["lineage_state"] == "no_lineage_declared"
    # Tier and proof stay visible; the coaching prose is what full mode is for.
    assert summary["identity_assurance"] == {
        "tier": "weak",
        "score": 0.35,
        "caller_proven": False,
        "proof_origin": "server_inferred",
        "baseline": "fresh_identity",
    }
    assert "how_to_strengthen" not in json.dumps(env)


def test_start_session_hint_names_a_read_and_never_start_session():
    """Re-sending the original force_new arguments with response_mode='full'
    would mint a second identity, so the hint must not name start_session as
    the route to the payload. It names an identity read of this session."""
    env = build_experience_envelope("start_session", "onboard", _onboard_payload(), {})
    hint = env["raw_governance_hint"]
    assert f"identity(client_session_id='{_SID}')" in hint
    assert "start_session(response_mode" not in hint
    assert "start_session(" in hint and "second identity" in hint
    # The only start_session call the hint names is the one it warns against.
    assert hint.count("start_session(") == 1
    assert "start_session(force_new=true) mints a second identity" in hint


def test_start_session_ack_surfaces_mint_failure_signals():
    """resident_registration, label_renamed and bootstrap exist because each
    failure is otherwise invisible to the caller. The default ack omits
    raw_governance, so they must ride at the top level."""
    env = build_experience_envelope(
        "start_session", "onboard", _onboard_payload_with_mint_signals(), {}
    )
    assert "raw_governance" not in env
    registration = env["resident_registration"]
    assert registration["status"] == "not_on_roster"
    assert registration["on_roster"] is False
    assert registration["requested_name"] == "vigil_probe"
    assert "NOT in this deployment's UNITARES_RESIDENTS roster" in registration["detail"]
    assert env["label_renamed"]["requested"] == "canary_dialectic_probe"
    assert env["label_renamed"]["applied"] == "canary_dialectic_probe_2"
    assert env["label_renamed"]["reason"] == "label_taken_by_active_agent"
    assert env["bootstrap"] == {"written": False, "reason": "error", "detail": "TimeoutError"}


@pytest.mark.parametrize("tags,roster,status,has_detail", [
    (["persistent", "autonomous"], ["vigil_probe"], "registered", False),
    (["ephemeral"], [], "no_roster_configured", False),
    (["ephemeral"], ["vigil"], "not_on_roster", True),
    (None, ["vigil"], "caller_supplied_tags", True),
])
def test_resident_registration_detail_rides_only_when_it_may_be_a_failure(
    tags, roster, status, has_detail
):
    from src.grounding.onboard_classifier import resident_registration

    payload = _onboard_payload()
    payload["resident_registration"] = resident_registration(
        "vigil_probe", tags, roster=roster
    )
    env = build_experience_envelope("start_session", "onboard", payload, {})
    registration = env["resident_registration"]
    assert registration["status"] == status
    assert ("detail" in registration) is has_detail


def test_start_session_ack_without_mint_signals_adds_none():
    env = build_experience_envelope("start_session", "onboard", _onboard_payload(), {})
    for key in ("resident_registration", "label_renamed", "bootstrap"):
        assert key not in env


def test_start_session_ack_without_a_token_does_not_invent_one():
    payload = _onboard_payload()
    payload.pop("continuity_token")
    env = build_experience_envelope("start_session", "onboard", payload, {})
    assert "continuity_token" not in env


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
        ("start_session", "onboard", {"success": True, "uuid": _UUID}, {}),
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
    ("start_session", "onboard"),
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


# -- readers that used to reach into raw_governance -------------------------

def _load_script(name: str, relative: str):
    import importlib.util
    import sys
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / relative
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_dialectic_canary_onboard_check_reads_the_default_ack():
    canary = _load_script("dialectic_canary", "scripts/ops/dialectic_canary.py")
    payload = _onboard_payload()
    payload["display_name"] = canary.LABEL_PREFIX + "probe"
    env = build_experience_envelope("start_session", "onboard", payload, {})
    ok, detail = canary.evaluate_onboard(env)
    assert ok, detail


def test_coordination_demo_finds_the_token_in_the_default_ack():
    demo = _load_script("coordination_demo", "scripts/demo/coordination_demo.py")
    env = build_experience_envelope("start_session", "onboard", _onboard_payload(), {})
    assert demo._deep_first(env, ("agent_uuid", "uuid")) == _UUID
    assert demo._deep_first(env, ("continuity_token",)) == _TOKEN


def test_audit_attribution_resolves_the_minted_uuid_from_the_default_ack():
    from src.services.tool_usage_recorder import resolve_minted_agent_id

    env = build_experience_envelope("start_session", "onboard", _onboard_payload(), {})
    assert resolve_minted_agent_id("start_session", None, _result(env)) == _UUID


_MCP_WRITE_ARGS = [
    ("start_session", {}, True),
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
    so over /mcp/ a response_mode='full' on them never reaches the envelope;
    only REST and stdio, which dispatch without that model, carry it. Their
    hint therefore names a knowledge details read, which works on every
    transport. Declaring response_mode on them would change the advertised
    input schema and so the interface contract digest; if that is ever done,
    this test flips and the hint should name it.
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
    if friendly == "start_session":
        # Declared, but the hint deliberately does not name it (a repeated
        # start_session mints a second identity). It names an identity read,
        # whose /mcp/ model must deliver client_session_id.
        identity_tool = mcp_server.mcp._tool_manager.get_tool("identity")
        read = identity_tool.fn_metadata.arg_model.model_validate(
            {"client_session_id": _SID}
        ).model_dump_one_level()
        assert read.get("client_session_id") == _SID
        assert f"identity(client_session_id='{_SID}')" in hint
        assert "response_mode" not in hint
    elif declares_full:
        assert f"{friendly}(response_mode='full')" in hint
    else:
        assert "response_mode" not in hint
        assert "knowledge(action='details'" in hint


@pytest.mark.parametrize("friendly,args,older_flag,delivered", [
    # onboard's schema declares verbose, but start_session's /mcp/ argument
    # model does not, so FastMCP drops it there; only REST and stdio carry it.
    ("start_session", {}, "verbose", False),
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
