"""Byte budgets for the default lifecycle responses.

The onboard payload below is the live shape a default ``start_session``
returned on 2026-09-25; the rule tests mutate it. The start_session budgets
are measured on the real onboard handler's output instead
(tests/helpers/onboard_producer.py), one mint class each, because that
hand-written fixture is an anonymous mint at thread position 1 and the common
classes never reached the budget. The sync source is a hand-built minimal
proceed in the live shape, so a field a real handler adds later does not reach
this budget; the live check after deploy is the complement. A budget is a
ceiling on the normal case only: guide, pause, provisional, weak-binding and
non-plain-mint responses keep their full explanations and are asserted to.
Budgets count UTF-8 bytes, not tokens; the base64 continuity_token costs more
tokens per byte than prose. Restoring a field an abnormal case needs is a
reason to raise a budget, stated in the PR, not a regression.

start_session budgets per mint class, measured on the real handler
(2026-09-26):

- anonymous, thread position 1: 1,152 B against 1,200.
- sibling_locus (a fresh uuid on a thread earlier process-instances
  occupied): 1,461 B against 1,550. It adds predecessor_uuid,
  episode_fork_kind and the ~210 B sentence saying co-location does not
  establish lineage. That sentence is kept, not trimmed to fit: it is what
  stops the earlier node's uuid being read as this process's parent.
- each lifted mint notice adds up to 300 B on top of its class. A named
  mint's compact resident_registration is about 294 B for not_on_roster
  (the status plus a short detail: what it costs, that this identity cannot
  gain the tags, and the roster -> restart -> fresh-mint fix) and 70-81 B for
  the other statuses; a written bootstrap ack is
  224 B. Named at position 1: 1,434 B against 1,500; named sibling_locus:
  1,743 B against 1,850.
"""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src.mcp_handlers.middleware import envelope_step
from src.mcp_handlers.middleware.envelope_step import build_experience_envelope
from src.mcp_handlers.response_formatter import format_response
from src.thread_identity import build_fork_context
from tests.helpers import onboard_producer

SDK_SRC = Path(__file__).resolve().parent.parent / "agents" / "sdk" / "src"
if str(SDK_SRC) not in sys.path:
    sys.path.insert(0, str(SDK_SRC))

from unitares_sdk._checkin_fields import resolve_checkin_fields  # noqa: E402

# start_session, per mint class; see the module docstring.
START_SESSION_BUDGET = 1_200
START_SESSION_SIBLING_BUDGET = 1_550
START_SESSION_NOTICE_ALLOWANCE = 300
# Raised from 900 in #2448: the margin now carries its scope (~75 B), which
# is the cost of an honest "comfortable" on the normal live decision.
ROUTINE_SYNC_BUDGET = 960

_TOKEN = (
    "v1.eyJhaWQiOiI1NGQ2Mjg0Ni03MGJjLTQxZTAtYWZjZi0wODdkOTRiNWQ3NDciLCJjaCI6"
    "ImNsYXVkZV9jb2RlIiwiZXhwIjoxNzkwMzI0ODc2LCJpYXQiOjE3OTAzMjEyNzYsIm1mIjoi"
    "Y2xhdWRlIiwib3B2IjoxLCJzaWQiOiJhZ2VudC01NGQ2Mjg0Ni03MGIiLCJ2IjoxfQ."
    "esSf_Wip24jzHsTBncPl9Xymkf4jtP2pTIuFRghwhMA"
)


def _wire(obj) -> int:
    return len(json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def _onboard_payload(**assurance_overrides) -> dict:
    assurance = {
        "tier": "weak",
        "score": 0.35,
        "session_source": "ip_ua_fingerprint",
        "trajectory_confidence": None,
        "reason": "server-inferred binding ('ip_ua_fingerprint'); not caller-proven",
        "caller_proven": False,
        "proof_origin": "server_inferred",
        "how_to_strengthen": (
            "binding was server-inferred (not caller-proven); pass the "
            "client_session_id returned by start_session explicitly on the next "
            "call; adapters may inject it automatically. If this transport cannot "
            "retain that binding, use identity(agent_uuid=..., continuity_token=..., "
            "resume=true) as an explicit same-live-process rebind instead of "
            "attaching continuity_token to ordinary tool calls"
        ),
        "baseline": "fresh_identity",
        "baseline_note": (
            "Expected baseline for a just-minted identity — not a deficiency. Use "
            "the returned client_session_id on the next call; adapters may inject "
            "it automatically."
        ),
    }
    assurance.update(assurance_overrides)
    return {
        "success": True,
        "server_time": "2026-09-25T07:27:56.084524+00:00",
        "welcome": (
            "Your session ID is `agent-54d62846-70b`. You are node 1 in thread "
            "t-f3a8bb1ebd."
        ),
        "uuid": "54d62846-70bc-41e0-afcf-087d94b5d747",
        "agent_id": "Claude_Opus_5_5_20260925",
        "display_name": "claude-code-opus_54d62846",
        "is_new": True,
        "client_session_id": "agent-54d62846-70b",
        "identity_assurance": assurance,
        "next_step": (
            "Call process_agent_update with response_text describing your work and "
            "the returned client_session_id; adapters may inject it automatically. "
            "Reserve continuity_token for an explicit identity(agent_uuid=..., "
            "continuity_token=..., resume=true) rebind, not ordinary tool calls."
        ),
        "provisional_lineage": False,
        "response_mode": "minimal",
        "identity_resolution_outcome": "minted_force_new",
        "onboard_origin": "agent",
        "onboard_origin_basis": "default_unmarked_call",
        "lineage_state": "no_lineage_declared",
        "continuity_token": _TOKEN,
        "thread_context": {
            "thread_id": "t-f3a8bb1ebde032c8",
            "position": 1,
            "spawn_reason": "new_session",
            "predecessor": None,
            "thread_size": 1,
            "is_root": True,
            "is_fork": False,
            "episode_fork_kind": "none",
            "identity_lineage_fork": False,
            "honest_message": "You are the first observation under this thread. No fork.",
        },
    }


def _sync_source(**decision_overrides) -> dict:
    decision = {
        "action": "proceed",
        "sub_action": "approve",
        "reason": "Low risk (27.0%) - healthy operating range",
        "margin": "comfortable",
        "nearest_edge": None,
        # Live shape: the coherence edge is unassessed for every agent today,
        # so a real decision carries these and the formatter passes them on.
        "unmeasurable_edges": ["coherence"],
        "margin_scope": "measured_edges_only",
    }
    decision.update(decision_overrides)
    return {
        "success": True,
        "status": "healthy",
        "health_status": "healthy",
        "prediction_id": "64c5d92a-fe16-46a5-93f0-4e7c2848aa06",
        "decision": decision,
        "metrics": {
            "E": 0.72,
            "I": 0.79,
            "S": 0.21,
            "V": -0.02,
            "coherence": 0.4830146074987347,
            "coherence_source": "legacy_tanh_v",
            "coherence_role": "ode_control_feedback",
            "risk_score": 0.27,
            "verdict": "safe",
            "health_status": "healthy",
        },
    }


def _sync_envelope(source: dict) -> dict:
    formatted = format_response(deepcopy(source), {"response_mode": "auto"})
    return build_experience_envelope(
        "sync_state", "process_agent_update", formatted, {"response_mode": "auto"}
    )


# --- start_session ---------------------------------------------------------


def test_default_start_session_fits_budget_and_keeps_what_adapters_read():
    payload = _onboard_payload()
    env = build_experience_envelope("start_session", "onboard", payload, {})

    assert _wire(env) <= START_SESSION_BUDGET, _wire(env)
    assert env["response_shape"] == "routine"
    assert "raw_governance" not in env
    assert "raw_governance_available" not in env  # not fetchable after the mint
    assert "_response_size" not in env
    # Fields the plugin post-identity hook and identity_sidecar read.
    assert env["agent_uuid"] == payload["uuid"]
    assert env["client_session_id"] == payload["client_session_id"]
    assert env["agent_id"] == payload["agent_id"]
    assert env["display_name"] == payload["display_name"]
    assert env["label_is"] == "social_or_cosmetic"
    assert env["is_new"] is True
    # "fresh because asked" vs "fresh because a resume missed".
    assert env["identity_resolution_outcome"] == "minted_force_new"
    # The minting call's own assurance: weak / ip_ua_fingerprint by
    # construction on a transport with no session signal, and `baseline` says
    # that is expected. The operator guide confirms threading on the NEXT call.
    assert env["identity_assurance"] == {
        "tier": "weak",
        "session_source": "ip_ua_fingerprint",
        "caller_proven": False,
        "baseline": "fresh_identity",
    }
    # The token rides with its caveat, not bare at the top level.
    assert "continuity_token" not in env
    assert env["rebind"]["continuity_token"] == _TOKEN
    assert "resume=true" in env["rebind"]["use_only_for"]
    assert env["state_summary"]["lineage_state"] == "no_lineage_declared"


def test_start_session_full_mode_keeps_the_canonical_record():
    payload = _onboard_payload()
    env = build_experience_envelope(
        "start_session", "onboard", payload, {"response_mode": "full"}
    )
    assert env["raw_governance"] is payload
    assert env["response_shape"] == "full"
    # The top-level fields a client reads survive the mode switch.
    for key in ("agent_id", "display_name", "is_new", "identity_resolution_outcome"):
        assert env[key] == payload[key]
    assert env["rebind"]["continuity_token"] == _TOKEN


@pytest.mark.parametrize("mode", ["standard", "verbose"])
def test_start_session_other_expanded_modes_keep_the_canonical_record(mode):
    payload = _onboard_payload()
    env = build_experience_envelope(
        "start_session", "onboard", payload, {"response_mode": mode}
    )
    assert env["raw_governance"] is payload


def test_start_session_legacy_verbose_flag_keeps_the_canonical_record():
    payload = _onboard_payload()
    env = build_experience_envelope("start_session", "onboard", payload, {"verbose": True})
    assert env["raw_governance"] is payload


@pytest.mark.parametrize(
    "change",
    [
        {"identity_resolution_outcome": "minted_after_resume_miss"},
        {"identity_resolution_outcome": None},
        {"is_new": False, "identity_resolution_outcome": "resumed"},
        {"auto_resumed": True},
        {"previous_status": "archived"},
        {"trajectory": {"genesis": "g", "trust_tier": "t"}},
        {"provisional_lineage": True},
    ],
    ids=[
        "resume_miss",
        "outcome_missing",
        "resumed",
        "auto_resumed",
        "reactivated_archive",
        "trajectory",
        "provisional_lineage",
    ],
)
def test_a_mint_that_did_not_go_as_asked_keeps_the_whole_record(change):
    """These facts exist only in the mint's response; identity() re-reads the
    binding, not how it was made. So anything but a plain fresh mint keeps the
    canonical record, and a missing outcome counts as not plain."""
    payload = _onboard_payload()
    for key, value in change.items():
        if value is None:
            payload.pop(key, None)
        else:
            payload[key] = value
    env = build_experience_envelope("start_session", "onboard", payload, {})

    assert env["raw_governance"] is payload
    assert env["response_shape"] == "full"


_EARLIER = onboard_producer.EARLIER_UUID
_MODEL = {"force_new": True, "model_type": "claude-opus-5-5"}
_NAMED = {**_MODEL, "name": "my-agent"}
# (arguments, thread position, byte budget) per mint class.
_MINT_CLASSES = {
    "anonymous": (_MODEL, 1, START_SESSION_BUDGET),
    "sibling_locus": (_MODEL, 2, START_SESSION_SIBLING_BUDGET),
    "named": (_NAMED, 1, START_SESSION_BUDGET + START_SESSION_NOTICE_ALLOWANCE),
    "named_sibling_locus": (
        _NAMED, 2, START_SESSION_SIBLING_BUDGET + START_SESSION_NOTICE_ALLOWANCE,
    ),
}


def _field_exists(env: dict, reason: str) -> bool:
    """A response_shape_reason entry names a field the response carries."""
    path = reason.split("=", 1)[0].split(".")
    for container in (env, env.get("raw_governance") or {}):
        node = container
        for part in path:
            if not isinstance(node, dict) or part not in node:
                break
            node = node[part]
        else:
            return True
    # A reason may name a field by its absence ("...=missing").
    return reason.endswith("=missing")


def test_the_envelope_budgets_are_the_ones_stated_here():
    assert envelope_step._START_SESSION_BUDGET_BYTES == START_SESSION_BUDGET
    assert envelope_step._START_SESSION_SIBLING_BUDGET_BYTES == START_SESSION_SIBLING_BUDGET
    assert (
        envelope_step._START_SESSION_NOTICE_ALLOWANCE_BYTES
        == START_SESSION_NOTICE_ALLOWANCE
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mint_class", list(_MINT_CLASSES))
async def test_a_default_mint_is_routine_within_its_class_budget(mint_class):
    """Built by the real onboard handler, not a hand-written payload."""
    arguments, position, budget = _MINT_CLASSES[mint_class]
    payload = await onboard_producer.mint(arguments, position=position)
    env, wire = await onboard_producer.start_session(arguments, payload)

    # The premise: the handler produced the class this case is about.
    fork_kind = payload["thread_context"]["episode_fork_kind"]
    assert fork_kind == ("sibling_locus" if position > 1 else "none")
    assert ("resident_registration" in payload) is ("name" in arguments)

    assert env["response_shape"] == "routine", env.get("response_shape_reason")
    assert "raw_governance" not in env
    assert "response_shape_reason" not in env
    assert "_response_size" not in env
    assert wire <= budget, (mint_class, wire, budget)
    assert env["agent_uuid"] == payload["uuid"]
    assert env["client_session_id"] == payload["client_session_id"]
    assert env["rebind"]["continuity_token"] == payload["continuity_token"]
    if position > 1:
        # R6's discriminator and the earlier node, with the sentence that
        # keeps that node from being read as a parent.
        assert env["state_summary"]["episode_fork_kind"] == "sibling_locus"
        assert env["state_summary"]["predecessor_uuid"] == _EARLIER
        assert "co-location does not establish lineage" in env["next_action"]
    else:
        assert "episode_fork_kind" not in env["state_summary"]
        assert "predecessor_uuid" not in env["state_summary"]
    if "name" in arguments:
        registration = payload["resident_registration"]
        assert env["resident_registration"]["status"] == registration["status"]
        assert env["resident_registration"]["on_roster"] is registration["on_roster"]
    else:
        assert "resident_registration" not in env


@pytest.mark.asyncio
async def test_a_declared_lineage_mint_keeps_the_whole_record_and_says_why():
    arguments = {**_MODEL, "parent_agent_id": _EARLIER, "spawn_reason": "explicit"}
    payload = await onboard_producer.mint(
        arguments,
        position=2,
        lineage_row={"parent_agent_id": _EARLIER, "provisional_lineage": True},
    )
    env, _wire_bytes = await onboard_producer.start_session(arguments, payload)

    assert payload["thread_context"]["episode_fork_kind"] == "identity_lineage"
    assert env["response_shape"] == "full"
    assert env["raw_governance"] == payload
    reasons = env["response_shape_reason"].split(", ")
    assert f"lineage_state={payload['lineage_state']}" in reasons
    assert "thread_context.episode_fork_kind=identity_lineage" in reasons
    assert all(_field_exists(env, reason) for reason in reasons), reasons
    assert env["state_summary"]["episode_fork_kind"] == "identity_lineage"
    assert env["state_summary"]["predecessor_uuid"] == _EARLIER
    assert "Declared lineage for this fork is already recorded" in env["next_action"]
    assert "co-location does not establish lineage" not in env["next_action"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [{}, {"response_mode": "full"}], ids=["default", "full"])
async def test_a_label_rename_keeps_the_whole_record_and_says_why(mode):
    """A refused label is abnormal and exists only in this response, so it is
    named as the reason under an explicit full request too."""
    arguments = {**_NAMED, **mode}
    with patch(
        "src.mcp_handlers.identity.handlers.set_agent_label_resolved",
        AsyncMock(return_value="my-agent_4dc58779"),
    ):
        payload = await onboard_producer.mint(arguments)
    env, _wire_bytes = await onboard_producer.start_session(arguments, payload)

    assert payload["label_renamed"]["requested"] == "my-agent"
    assert env["response_shape"] == "full"
    assert env["raw_governance"]["label_renamed"] == payload["label_renamed"]
    assert env["response_shape_reason"] == "label_renamed"
    # The registration verdict is lifted the same way in the full shape.
    assert env["resident_registration"]["status"] == "not_on_roster"


def _sibling_payload(**thread_overrides) -> dict:
    payload = _onboard_payload()
    payload["thread_context"] = build_fork_context(
        thread_id="t-f3a8bb1ebde032c8",
        position=2,
        parent_uuid=None,
        spawn_reason="new_session",
        all_nodes=[onboard_producer.EARLIER_NODE],
        agent_uuid=payload["uuid"],
        minted_fresh=True,
    )
    payload["thread_context"].update(thread_overrides)
    return payload


def test_a_sibling_locus_mint_is_routine():
    payload = _sibling_payload()
    env = build_experience_envelope("start_session", "onboard", payload, {})

    assert env["response_shape"] == "routine"
    assert "raw_governance" not in env
    assert env["state_summary"]["predecessor_uuid"] == _EARLIER
    assert env["state_summary"]["episode_fork_kind"] == "sibling_locus"


@pytest.mark.parametrize(
    "change",
    [
        {"thread": {"episode_fork_kind": None}},
        {"thread": {"episode_fork_kind": "continuation"}},
        {"thread": {"identity_lineage_fork": True}},
        {"thread": {"identity_lineage_fork": None}},
        {"thread": {"spawn_reason": "subagent"}},
        # A declared succession that produced no lineage (no parent given):
        # the reason appears nowhere in the routine shape.
        {"thread": {"spawn_reason": "explicit"}},
        # Earlier nodes pruned: no uuid to lift beside the co-location
        # sentence, so the record's honest_message is the only statement.
        {"thread": {"predecessor": None}},
        {"payload": {"lineage_state": None}},
        {"payload": {"lineage_state": "rejected_coincidental"}},
    ],
    ids=[
        "kind_missing",
        "kind_unknown",
        "lineage_fork",
        "lineage_fork_missing",
        "lineage_spawn_reason",
        "explicit_without_parent",
        "predecessor_pruned",
        "lineage_state_missing",
        "rejected_coincidental",
    ],
)
def test_a_sibling_locus_with_any_lineage_signal_keeps_the_whole_record(change):
    """Positive evidence only: the sibling case is routine only when every
    lineage signal says none was declared."""
    payload = _sibling_payload(**change.get("thread", {}))
    for key, value in change.get("payload", {}).items():
        if value is None:
            payload.pop(key)
        else:
            payload[key] = value
    env = build_experience_envelope("start_session", "onboard", payload, {})

    assert env["raw_governance"] is payload
    assert env["response_shape"] == "full"
    assert env["response_shape_reason"]


def test_a_root_node_with_a_lineage_spawn_reason_keeps_the_whole_record():
    # A lineage spawn reason with no parent is a self-declared continuation
    # (build_fork_context classifies it identity_lineage at position 1 too).
    payload = _onboard_payload()
    payload["thread_context"] = build_fork_context(
        thread_id="t-f3a8bb1ebde032c8",
        position=1,
        parent_uuid=None,
        spawn_reason="compaction",
        all_nodes=[],
        agent_uuid=payload["uuid"],
        minted_fresh=True,
    )
    env = build_experience_envelope("start_session", "onboard", payload, {})

    assert env["raw_governance"] is payload
    assert env["response_shape_reason"] == (
        "thread_context.episode_fork_kind=identity_lineage"
    )
    assert env["state_summary"]["episode_fork_kind"] == "identity_lineage"


def _root_payload(**thread_overrides):
    payload = _onboard_payload()
    payload["thread_context"] = build_fork_context(
        thread_id="t-f3a8bb1ebde032c8",
        position=1,
        parent_uuid=None,
        spawn_reason="new_session",
        all_nodes=[],
        agent_uuid=payload["uuid"],
        minted_fresh=True,
    )
    payload["thread_context"].update(thread_overrides)
    return payload


def test_a_plain_root_node_mint_is_routine():
    env = build_experience_envelope("start_session", "onboard", _root_payload(), {})

    assert env["response_shape"] == "routine"
    assert "raw_governance" not in env


@pytest.mark.parametrize(
    "overrides, reason",
    [
        ({"predecessor": {"uuid": "earlier-uuid", "position": 0}}, "thread_context.predecessor"),
        ({"is_fork": True}, "thread_context.is_fork"),
    ],
    ids=["predecessor", "is_fork"],
)
def test_a_root_node_that_names_a_predecessor_keeps_the_whole_record(overrides, reason):
    """An inconsistent producer state (a root node pointing at an earlier
    node, or flagged a fork) is shown whole, not trimmed into a root."""
    payload = _root_payload(**overrides)
    env = build_experience_envelope("start_session", "onboard", payload, {})

    assert env["raw_governance"] is payload
    assert env["response_shape"] == "full"
    assert reason in env["response_shape_reason"]


def test_a_mint_without_thread_context_keeps_the_whole_record():
    """Onboard omits thread_context when it could not place the mint (thread
    lookup raised, or no thread_id). Unknown position is not a root node."""
    payload = _root_payload()
    del payload["thread_context"]
    env = build_experience_envelope("start_session", "onboard", payload, {})

    assert env["raw_governance"] is payload
    assert env["response_shape"] == "full"
    assert "thread_context=missing" in env["response_shape_reason"]


def test_start_session_abnormal_weak_binding_keeps_the_full_explanation():
    # A weak binding that is NOT the fresh-mint baseline (e.g. a resume that
    # fell back to a transport fingerprint) is something the agent must fix.
    payload = _onboard_payload(baseline=None, baseline_note=None)
    env = build_experience_envelope("start_session", "onboard", payload, {})

    assert env["identity_assurance"] is payload["identity_assurance"]
    assert "how_to_strengthen" in env["identity_assurance"]
    assert env["raw_governance"] is payload


def test_start_session_caller_proven_medium_binding_keeps_assurance():
    payload = _onboard_payload(
        tier="medium",
        caller_proven=True,
        proof_origin="caller_asserted",
        baseline=None,
    )
    env = build_experience_envelope("start_session", "onboard", payload, {})
    assert env["identity_assurance"] is payload["identity_assurance"]


def test_start_session_caller_proven_strong_binding_is_compact():
    payload = _onboard_payload(
        tier="strong", caller_proven=True, baseline=None, proof_origin="caller_asserted",
        session_source="explicit_client_session_id",
    )
    env = build_experience_envelope("start_session", "onboard", payload, {})
    assert env["identity_assurance"] == {
        "tier": "strong",
        "session_source": "explicit_client_session_id",
        "caller_proven": True,
    }


def test_routine_start_session_has_no_hint_inviting_a_re_mint():
    env = build_experience_envelope("start_session", "onboard", _onboard_payload(), {})
    assert "raw_governance_hint" not in env


# --- sync_state routine proceed --------------------------------------------


def test_routine_proceed_fits_budget():
    env = _sync_envelope(_sync_source())
    assert _wire(env) <= ROUTINE_SYNC_BUDGET, (_wire(env), env)


def test_routine_proceed_says_each_fact_once():
    env = _sync_envelope(_sync_source())

    # action_summary is the documented first read; the integration samples
    # read action_summary.action.
    assert env["action_summary"] == {
        "action": "proceed",
        "reason": "Low risk (27.0%) - healthy operating range",
        "risk_score": 0.27,
    }
    assert env["response_shape"] == "routine"
    state = env["state_summary"]
    assert state["action"] == "proceed"
    assert state["sub_action"] == "approve"
    assert state["risk_score"] == 0.27
    assert "status" not in state
    assert "health_status" not in state
    # The margin stays: it is short, and honest only beside its scope.
    assert state["margin"] == "comfortable"
    assert env["prediction_id"] == "64c5d92a-fe16-46a5-93f0-4e7c2848aa06"
    # The id is lifted beside next_action, which names it instead of repeating it.
    assert "prediction_id" in env["next_action"]


def test_sdk_still_resolves_a_trimmed_routine_proceed():
    env = _sync_envelope(_sync_source())
    fields = resolve_checkin_fields(env)

    assert fields["verdict"] == "proceed"
    assert fields["coherence"] == pytest.approx(0.4830146074987347)
    assert fields["risk"] == 0.27
    # A routine proceed carries no guidance; the reason is not guidance there.
    assert fields["guidance"] is None


def test_unhealthy_status_is_not_trimmed():
    source = _sync_source()
    source["status"] = "moderate"
    env = _sync_envelope(source)
    assert env["state_summary"].get("status") == "moderate"
    # A proceed in an unhealthy state is not routine: action_summary keeps
    # its action too, not just state_summary its status.
    assert env["action_summary"]["action"] == "proceed"


def test_unassessed_edges_stay_visible_on_a_routine_proceed():
    # The coherence edge is unassessed for every agent today, so this is the
    # normal live decision shape once the edges reach the envelope. It is
    # routine, and the margin keeps its scope and unassessed edges beside it:
    # "comfortable" alone would read as "no limit is near".
    payload = format_response(deepcopy(_sync_source()), {"response_mode": "auto"})
    payload["decision"]["unmeasurable_edges"] = ["coherence"]
    payload["decision"]["margin_scope"] = "measured_edges_only"
    env = build_experience_envelope(
        "sync_state", "process_agent_update", payload, {"response_mode": "auto"}
    )
    assert env["response_shape"] == "routine"
    assert env["state_summary"]["margin"] == "comfortable"
    assert env["state_summary"]["margin_scope"] == "measured_edges_only"
    assert env["state_summary"]["unmeasurable_edges"] == ["coherence"]


def test_near_edge_proceed_is_not_trimmed():
    payload = format_response(deepcopy(_sync_source()), {"response_mode": "auto"})
    payload["decision"]["nearest_edge"] = "risk"
    env = build_experience_envelope(
        "sync_state", "process_agent_update", payload, {"response_mode": "auto"}
    )
    assert env["state_summary"]["nearest_edge"] == "risk"
    assert "response_shape" not in env
    assert env["state_summary"]["margin"] == "comfortable"


def test_guide_keeps_the_full_action_summary():
    env = _sync_envelope(
        _sync_source(sub_action="guide", reason="Drift rising - narrow scope.")
    )
    summary = env["action_summary"]
    assert summary["action"] == "proceed"
    assert summary["sub_action"] == "guide"
    assert summary["verdict"] == "guide"
    assert summary["risk_score"] == 0.27
    # The SDK's guide guidance is action_summary.reason, whatever mirror put there.
    assert resolve_checkin_fields(env)["guidance"] == summary["reason"]


def test_pause_keeps_the_full_action_summary():
    source = _sync_source(
        action="pause",
        sub_action="reject",
        reason="Risk crossed the pause threshold.",
        margin="critical",
        nearest_edge="risk_pause",
    )
    source["status"] = source["health_status"] = "critical"
    source["metrics"]["risk_score"] = 0.82
    source["metrics"]["verdict"] = "high-risk"
    env = _sync_envelope(source)

    summary = env["action_summary"]
    assert summary["action"] == "pause"
    assert summary["verdict"] == "pause"
    assert summary["risk_score"] == 0.82
    assert env["state_summary"]["nearest_edge"] == "risk_pause"
    assert env["state_summary"]["margin"] == "critical"
    assert resolve_checkin_fields(env)["verdict"] == "pause"


def test_tight_margin_keeps_the_full_shape():
    env = _sync_envelope(_sync_source(margin="tight", nearest_edge="risk_pause"))
    assert env["action_summary"]["action"] == "proceed"
    assert env["state_summary"]["margin"] == "tight"


@pytest.mark.parametrize(
    "drop",
    [("status", "health_status"), ("verdict",), ("margin",)],
    ids=["health_missing", "verdict_missing", "margin_missing"],
)
def test_a_proceed_missing_its_evidence_is_not_trimmed(drop):
    """A producer regression that drops a field must not look routine.

    Before this change a routine proceed always said "healthy"; trimming it
    made absence the normal shape, so the rule admits positive evidence only
    and marks what it trims.
    """
    source = _sync_source()
    for key in drop:
        source.pop(key, None)
        source["metrics"].pop(key, None)
        source["decision"].pop(key, None)
    env = _sync_envelope(source)

    assert "response_shape" not in env
    assert env["action_summary"]["action"] == "proceed"


def test_a_proceed_carrying_a_review_nudge_is_not_trimmed():
    # The formatter attaches review_suggested itself; injected after it here.
    payload = format_response(deepcopy(_sync_source()), {"response_mode": "auto"})
    payload["review_suggested"] = {"trigger": "low_confidence"}
    env = build_experience_envelope(
        "sync_state", "process_agent_update", payload, {"response_mode": "auto"}
    )
    assert "request_review" in env["next_action"]
    assert "response_shape" not in env


def _deprecation_block() -> dict:
    from src.mcp_handlers.identity.session import build_token_deprecation_block

    return build_token_deprecation_block(
        used_token_for_resume=True, token_issued_at=1790321276
    )


@pytest.mark.parametrize(
    "extra",
    [
        # The handler's shape (identity/handlers.py); the real-handler rename
        # is test_a_label_rename_keeps_the_whole_record_and_says_why.
        {"label_renamed": {
            "requested": "taken",
            "applied": "taken_54d62846",
            "reason": "label_taken_by_active_agent",
            "detail": "'taken' is already held by another active agent.",
        }},
        {"resident_registration": {"status": "a_future_status", "on_roster": False}},
        {"resident_registration": {"status": "not_on_roster"}},
        {"bootstrap": {"written": False, "reason": "error", "detail": "RuntimeError"}},
        {"bootstrap": {"written": True, "state_id": 7, "a_future_field": 1}},
        {"deprecations": "real"},
        {"temporal_context": "Last session: 3 days ago."},
        {"lineage_state": "rejected_cross_role"},
        {"lineage_state": "rejected_coincidental"},
        {"some_future_notice": {"note": "x"}},
    ],
    ids=[
        "label_renamed",
        "resident_registration_unknown_status",
        "resident_registration_without_on_roster",
        "bootstrap_not_written",
        "bootstrap_unknown_field",
        "deprecations",
        "temporal_context",
        "rejected_cross_role",
        "rejected_coincidental",
        "unknown_key",
    ],
)
def test_a_mint_with_anything_extra_to_say_keeps_the_whole_record(extra):
    """Onboard adds keys after building the record; an allowlist, not a
    denylist, decides routine, so an unknown notice (or a known one in a
    shape the envelope does not know) is shown, not dropped."""
    if extra.get("deprecations") == "real":
        extra = {"deprecations": [_deprecation_block()]}
    payload = _onboard_payload()
    payload.update(extra)
    env = build_experience_envelope("start_session", "onboard", payload, {})

    assert env["raw_governance"] is payload
    assert env["response_shape"] == "full"
    (key,) = extra
    reasons = env["response_shape_reason"].split(", ")
    assert any(reason.split("=")[0] == key for reason in reasons), reasons
    assert all(_field_exists(env, reason) for reason in reasons), reasons


_RESIDENT = "ResidentX"


@pytest.mark.parametrize(
    "name, stamped, roster, status",
    [
        (_RESIDENT, ["persistent", "autonomous"], [_RESIDENT], "registered"),
        ("my-agent", ["ephemeral"], [_RESIDENT], "not_on_roster"),
        ("my-agent", ["ephemeral"], [], "no_roster_configured"),
        ("my-agent", None, [_RESIDENT], "caller_supplied_tags"),
    ],
)
def test_a_resident_registration_verdict_is_lifted_compact(name, stamped, roster, status):
    """Every named mint carries one; the verdict stays, the prose goes."""
    from src.grounding.onboard_classifier import resident_registration

    registration = resident_registration(name, stamped, roster=roster)
    assert registration["status"] == status
    payload = _onboard_payload()
    payload["display_name"] = name
    payload["resident_registration"] = registration
    env = build_experience_envelope("start_session", "onboard", payload, {})

    assert env["response_shape"] == "routine"
    assert "raw_governance" not in env
    lifted = env["resident_registration"]
    assert lifted["status"] == status
    assert lifted["on_roster"] is registration["on_roster"]
    assert set(lifted) == (
        {"status", "on_roster", "detail"}
        if status in ("not_on_roster", "caller_supplied_tags")
        else {"status", "on_roster"}
    )
    if status == "caller_supplied_tags":
        # The tags this mint carried are all it will ever carry.
        assert "cannot gain them later" in lifted["detail"]
    if status == "not_on_roster":
        # What it costs, that it cannot be fixed on this identity, and the
        # producer's remedy, whose fresh mint helps only after the roster
        # change and restart (onboard_classifier._REGISTRATION_DETAIL).
        assert "only at mint" in lifted["detail"]
        assert "auto-archive" in lifted["detail"]
        assert "bootstrap" not in lifted["detail"]
        assert "only to roster names" in lifted["detail"]
        assert "this identity cannot gain them" in lifted["detail"]
        remedy = lifted["detail"][lifted["detail"].index("Fix:"):]
        assert remedy.index("roster") < remedy.index("restart") < remedy.index("mint fresh")
    assert _wire({"resident_registration": lifted}) <= START_SESSION_NOTICE_ALLOWANCE
    assert _wire(env) <= START_SESSION_BUDGET + START_SESSION_NOTICE_ALLOWANCE


def test_the_envelope_knows_every_resident_registration_status():
    from src.grounding.onboard_classifier import _REGISTRATION_DETAIL

    assert envelope_step._RESIDENT_REGISTRATION_STATUSES == set(_REGISTRATION_DETAIL)


@pytest.mark.asyncio
async def test_a_written_bootstrap_is_lifted_whole():
    """The ack of the bootstrap row the caller asked for (initial_state),
    from the real writer."""
    from src.mcp_handlers.identity.bootstrap_checkin import write_bootstrap
    from src.mcp_handlers.schemas.core import BootstrapStateParams

    db = AsyncMock()
    db.is_substrate_earned = AsyncMock(return_value=False)
    db.record_bootstrap_state = AsyncMock(return_value=(42, True))
    bootstrap = await write_bootstrap(
        db, identity_id=1, agent_id="u", params=BootstrapStateParams(),
    )
    payload = _onboard_payload()
    payload["bootstrap"] = bootstrap
    env = build_experience_envelope("start_session", "onboard", payload, {})

    assert env["response_shape"] == "routine"
    assert env["bootstrap"] == bootstrap
    assert _wire({"bootstrap": bootstrap}) <= START_SESSION_NOTICE_ALLOWANCE
    assert "_response_size" not in env


_EXPLICIT_FULL = {
    "full": {"response_mode": "full"},
    "full_verbose": {"response_mode": "full", "verbose": True},
    # Onboard maps an unknown mode plus verbose to its full shape
    # (_derive_onboard_response_mode); the envelope must follow that rule.
    "unknown_mode_verbose": {"response_mode": "detailed", "verbose": True},
}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", list(_EXPLICIT_FULL))
@pytest.mark.parametrize("mint_class", list(_MINT_CLASSES))
async def test_an_explicit_full_request_on_a_plain_mint_gives_no_reason(mint_class, mode):
    """The handler builds its full (and verbose) shape for these, so the keys
    that shape adds to every mint must not read as why the record was kept.
    governance-start tells agents to ask for it to read
    session_resolution_source."""
    arguments, position, _budget = _MINT_CLASSES[mint_class]
    arguments = {**arguments, **_EXPLICIT_FULL[mode]}
    payload = await onboard_producer.mint(arguments, position=position)
    env, _wire_bytes = await onboard_producer.start_session(arguments, payload)

    # The premise: the handler built the full shape.
    assert "identity_context" in payload
    assert ("next_calls" in payload) is (mode != "full")
    assert env["response_shape"] == "full"
    assert env["raw_governance"] == payload
    assert "response_shape_reason" not in env, env.get("response_shape_reason")
    assert env["session_resolution_source"] == payload["session_resolution_source"]


@pytest.mark.asyncio
async def test_the_full_shape_keys_are_exactly_what_the_full_shape_adds():
    """_FULL_ONBOARD_SHAPE_KEYS is taken from the handler, not written down: a
    key the full shape starts carrying must be classified here (shape or
    fact), and a fact both shapes carry must never be in the set."""
    minimal = await onboard_producer.mint(_MODEL)
    full = await onboard_producer.mint({**_MODEL, **_EXPLICIT_FULL["full_verbose"]})

    assert minimal["response_mode"] == "minimal"
    assert set(full) - set(minimal) == envelope_step._FULL_ONBOARD_SHAPE_KEYS


def test_empty_extras_do_not_make_a_mint_unusual():
    payload = _onboard_payload()
    payload.update({"deprecations": [], "label_renamed": None})
    env = build_experience_envelope("start_session", "onboard", payload, {})
    assert env["response_shape"] == "routine"


def test_a_mint_without_an_assurance_block_keeps_the_whole_record():
    payload = _onboard_payload()
    payload.pop("identity_assurance")
    env = build_experience_envelope("start_session", "onboard", payload, {})
    assert env["raw_governance"] is payload
    assert env["response_shape"] == "full"
