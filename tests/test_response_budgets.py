"""Byte budgets for the default lifecycle responses.

The onboard payload below is the live shape a default ``start_session``
returned on 2026-09-25. The sync source is a hand-built minimal proceed in the
live shape, so a field a real handler adds later does not reach this budget;
the live check after deploy is the complement. A budget is a ceiling on the
normal case only: guide, pause, provisional, weak-binding and non-plain-mint
responses keep their full explanations and are asserted to. Budgets count
UTF-8 bytes, not tokens; the base64 continuity_token costs more tokens per
byte than prose. Restoring a field an abnormal case needs is a reason to raise
a budget, stated in the PR, not a regression.
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


def test_start_session_with_predecessor_keeps_the_whole_record():
    payload = _onboard_payload()
    payload["thread_context"]["predecessor"] = {"uuid": "u-prior"}
    env = build_experience_envelope("start_session", "onboard", payload, {})

    assert env["raw_governance"] is payload
    assert env["state_summary"]["predecessor_uuid"] == "u-prior"


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


@pytest.mark.parametrize(
    "extra",
    [
        {"label_renamed": {"from": "taken", "to": "taken_54d62846"}},
        {"resident_registration": {"status": "not_on_roster"}},
        {"bootstrap": {"status": "written"}},
        {"deprecations": ["old_param"]},
        {"lineage_state": "rejected_cross_role"},
        {"lineage_state": "rejected_coincidental"},
        {"some_future_notice": {"note": "x"}},
    ],
    ids=[
        "label_renamed",
        "resident_registration",
        "bootstrap",
        "deprecations",
        "rejected_cross_role",
        "rejected_coincidental",
        "unknown_key",
    ],
)
def test_a_mint_with_anything_extra_to_say_keeps_the_whole_record(extra):
    """Onboard adds keys after building the record; an allowlist, not a
    denylist, decides routine, so an unknown notice is shown, not dropped."""
    payload = _onboard_payload()
    payload.update(extra)
    env = build_experience_envelope("start_session", "onboard", payload, {})

    assert env["raw_governance"] is payload
    assert env["response_shape"] == "full"


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
