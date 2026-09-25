"""Byte budgets for the default lifecycle responses.

The payloads below are the live shapes a default ``start_session`` and a
routine ``sync_state`` proceed returned on 2026-09-25, trimmed of nothing.
A budget is a ceiling on the normal case only: guide, pause, provisional and
weak-binding responses keep their full explanations and are asserted to.
"""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from src.mcp_handlers.middleware.envelope_step import build_experience_envelope
from src.mcp_handlers.response_formatter import format_response

SDK_SRC = Path(__file__).resolve().parent.parent / "agents" / "sdk" / "src"
if str(SDK_SRC) not in sys.path:
    sys.path.insert(0, str(SDK_SRC))

from unitares_sdk._checkin_fields import resolve_checkin_fields  # noqa: E402

START_SESSION_BUDGET = 1_200
ROUTINE_SYNC_BUDGET = 950

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
    assert "raw_governance" not in env
    assert "_response_size" not in env
    # Fields the plugin post-identity hook and identity_sidecar read.
    assert env["agent_uuid"] == payload["uuid"]
    assert env["client_session_id"] == payload["client_session_id"]
    assert env["agent_id"] == payload["agent_id"]
    assert env["display_name"] == payload["display_name"]
    assert env["is_new"] is True
    assert env["continuity_token"] == _TOKEN
    # A fresh mint's weak binding is the expected baseline, not a signal.
    assert "identity_assurance" not in env
    # No predecessor, no fork: nothing in thread_context to act on.
    assert "thread_context" not in env
    assert env["state_summary"]["lineage_state"] == "no_lineage_declared"


def test_start_session_full_mode_keeps_the_canonical_record():
    payload = _onboard_payload()
    env = build_experience_envelope(
        "start_session", "onboard", payload, {"response_mode": "full"}
    )
    assert env["raw_governance"] is payload
    # The top-level fields a client reads survive the mode switch.
    for key in ("agent_id", "display_name", "is_new", "continuity_token"):
        assert env[key] == payload[key]


def test_start_session_caller_proven_medium_binding_keeps_assurance():
    payload = _onboard_payload(
        tier="medium",
        caller_proven=True,
        proof_origin="caller_asserted",
        baseline=None,
    )
    env = build_experience_envelope("start_session", "onboard", payload, {})
    assert env["identity_assurance"]["tier"] == "medium"


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


def test_start_session_abnormal_weak_binding_keeps_the_full_explanation():
    # A weak binding that is NOT the fresh-mint baseline (e.g. a resume that
    # fell back to a transport fingerprint) is something the agent must fix.
    payload = _onboard_payload(baseline=None, baseline_note=None)
    payload["is_new"] = False
    env = build_experience_envelope("start_session", "onboard", payload, {})

    assert env["identity_assurance"] is payload["identity_assurance"]
    assert "how_to_strengthen" in env["identity_assurance"]


def test_start_session_caller_proven_binding_omits_assurance():
    payload = _onboard_payload(
        tier="strong", caller_proven=True, baseline=None, proof_origin="caller_asserted"
    )
    env = build_experience_envelope("start_session", "onboard", payload, {})
    assert "identity_assurance" not in env


def test_start_session_with_predecessor_keeps_thread_context():
    payload = _onboard_payload()
    payload["thread_context"]["predecessor"] = {"uuid": "u-prior"}
    env = build_experience_envelope("start_session", "onboard", payload, {})

    assert env["thread_context"]["predecessor"] == {"uuid": "u-prior"}
    assert env["state_summary"]["predecessor_uuid"] == "u-prior"


def test_start_session_provisional_lineage_is_lifted_when_true():
    payload = _onboard_payload()
    payload["provisional_lineage"] = True
    env = build_experience_envelope("start_session", "onboard", payload, {})
    assert env["provisional_lineage"] is True


def test_start_session_hint_does_not_invite_a_re_mint():
    env = build_experience_envelope("start_session", "onboard", _onboard_payload(), {})
    assert env["raw_governance_available"] is True
    assert "Do not re-mint" in env["raw_governance_hint"]


# --- sync_state routine proceed --------------------------------------------


def test_routine_proceed_fits_budget():
    env = _sync_envelope(_sync_source())
    assert _wire(env) <= ROUTINE_SYNC_BUDGET, (_wire(env), env)


def test_routine_proceed_says_each_fact_once():
    env = _sync_envelope(_sync_source())

    assert env["action_summary"] == {
        "reason": "Low risk (27.0%) - healthy operating range",
    }
    state = env["state_summary"]
    assert state["action"] == "proceed"
    assert state["sub_action"] == "approve"
    assert state["risk_score"] == 0.27
    assert "status" not in state
    assert "health_status" not in state
    assert "margin" not in state
    assert env["prediction_id"] == "64c5d92a-fe16-46a5-93f0-4e7c2848aa06"
    assert "prediction_id='64c5d92a" in env["next_action"]


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


def test_comfortable_margin_with_an_unassessed_edge_is_kept():
    # Built at the envelope directly: the compact formatter currently passes
    # only margin/nearest_edge through (response_formatter._format_compact),
    # so this pins the envelope's own rule for when the edges do arrive.
    payload = format_response(deepcopy(_sync_source()), {"response_mode": "auto"})
    payload["decision"]["unmeasurable_edges"] = ["coherence"]
    payload["decision"]["margin_scope"] = "measured_edges_only"
    env = build_experience_envelope(
        "sync_state", "process_agent_update", payload, {"response_mode": "auto"}
    )
    assert env["state_summary"]["margin"] == "comfortable"
    assert env["state_summary"]["margin_scope"] == "measured_edges_only"
    assert env["state_summary"]["unmeasurable_edges"] == ["coherence"]
    # An unassessed edge makes the proceed non-routine as a whole.
    assert env["action_summary"]["action"] == "proceed"
    assert env["action_summary"]["risk_score"] == 0.27


def test_near_edge_proceed_is_not_trimmed():
    payload = format_response(deepcopy(_sync_source()), {"response_mode": "auto"})
    payload["decision"]["nearest_edge"] = "risk"
    env = build_experience_envelope(
        "sync_state", "process_agent_update", payload, {"response_mode": "auto"}
    )
    assert env["state_summary"]["nearest_edge"] == "risk"
    assert env["action_summary"]["action"] == "proceed"


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
