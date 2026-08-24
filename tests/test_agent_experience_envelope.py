"""Agent-experience response envelope (middleware/envelope_step.py).

Alias-gated: only calls invoked via an experience alias (start_session,
sync_state, check_working_state, search_shared_memory, record_result,
request_review) are reshaped. The two contract guarantees pinned here:

1. Canonical names stay byte-identical - the envelope NEVER touches a
   response unless the invoked name is an experience alias.
2. The envelope never breaks a response - malformed payloads, error
   payloads, and builder failures all fall back to the raw result.
"""

from __future__ import annotations

from copy import deepcopy
import json

import pytest
from mcp.types import TextContent

from src.mcp_handlers.middleware import DispatchContext
from src.mcp_handlers.middleware.envelope_step import (
    apply_experience_envelope,
    build_experience_envelope,
)
from src.mcp_handlers.response_formatter import format_response
from src.mcp_handlers.tool_stability import is_experience_alias


def _result(payload) -> list:
    return [TextContent(type="text", text=json.dumps(payload))]


def _ctx(original_name: str) -> DispatchContext:
    return DispatchContext(original_name=original_name)


def _parse(result) -> dict:
    return json.loads(result[0].text)


# ---------------------------------------------------------------------------
# Registry flag
# ---------------------------------------------------------------------------


def test_experience_flag_inventory():
    """Exactly the eight workflow aliases are experience-enveloped - a
    ninth (or a canonical name) sneaking in changes response shapes
    and must be a deliberate edit here.

    Grew from six on 2026-08-11: `store_finding` / `update_finding` were added
    so the `knowledge` write actions have names of their own. They are flagged
    like every other workflow alias, and deliberately so — an agent that reaches
    shared memory through a friendly name should get the same envelope whether
    it is reading or writing. `record_result`, the existing write-side alias,
    already sets the precedent.
    """
    expected = {
        "start_session", "sync_state", "check_working_state",
        "search_shared_memory", "store_finding", "update_finding",
        "record_result", "request_review",
    }
    from src.mcp_handlers.tool_stability import _TOOL_ALIASES

    flagged = {n for n, a in _TOOL_ALIASES.items() if a.experience}
    assert flagged == expected


def test_canonical_names_are_not_experience_aliases():
    for name in ("onboard", "process_agent_update", "get_governance_metrics",
                 "knowledge", "outcome_event", "dialectic", "status", "checkin"):
        assert not is_experience_alias(name), name


# ---------------------------------------------------------------------------
# Step gating: who gets reshaped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_canonical_invocation_passes_through_byte_identical():
    raw = _result({"success": True, "agent_uuid": "u-1"})
    out = await apply_experience_envelope(
        "onboard", {}, _ctx("onboard"), raw
    )
    assert out is raw  # same object, not just equal


@pytest.mark.asyncio
async def test_legacy_alias_passes_through():
    """Pre-existing intuitive aliases (status, checkin) keep their raw
    shape - only experience aliases opt in."""
    raw = _result({"success": True})
    out = await apply_experience_envelope(
        "get_governance_metrics", {}, _ctx("status"), raw
    )
    assert out is raw


@pytest.mark.asyncio
async def test_experience_alias_gets_envelope():
    raw = _result({"success": True, "agent_uuid": "u-1", "client_session_id": "s-1"})
    out = await apply_experience_envelope(
        "onboard", {}, _ctx("start_session"), raw
    )
    data = _parse(out)
    assert data["tool"] == "start_session"
    assert data["agent_uuid"] == "u-1"
    assert data["client_session_id"] == "s-1"
    assert data["raw_governance"]["agent_uuid"] == "u-1"
    assert "next_action" in data


@pytest.mark.asyncio
async def test_error_payload_passes_through():
    """Error responses keep the raw contract (typed refusals, recovery
    blocks) - the envelope only reshapes successes."""
    for payload in ({"success": False, "error": "nope"},
                    {"error": "boom"},
                    {"success": False, "status": "identity_required"}):
        raw = _result(payload)
        out = await apply_experience_envelope(
            "outcome_event", {}, _ctx("record_result"), raw
        )
        assert out is raw, payload


@pytest.mark.asyncio
async def test_malformed_result_passes_through():
    for raw in ([TextContent(type="text", text="not json")],
                [TextContent(type="text", text="[1, 2]")],
                [], None):
        out = await apply_experience_envelope(
            "onboard", {}, _ctx("start_session"), raw
        )
        assert out is raw


@pytest.mark.asyncio
async def test_builder_failure_returns_raw(monkeypatch):
    """A bug in the builder must degrade to the raw response."""
    import src.mcp_handlers.middleware.envelope_step as es

    def _boom(*a, **k):
        raise RuntimeError("builder bug")

    monkeypatch.setattr(es, "build_experience_envelope", _boom)
    raw = _result({"success": True})
    out = await apply_experience_envelope(
        "onboard", {}, _ctx("start_session"), raw
    )
    assert out is raw


# ---------------------------------------------------------------------------
# Builder: per-tool harvesting (pure)
# ---------------------------------------------------------------------------


def test_onboard_envelope_surfaces_predecessor():
    payload = {
        "success": True,
        "uuid": "u-new",
        "lineage_state": "no_lineage_declared",
        "thread_context": {"predecessor": {"uuid": "u-prior"}},
    }
    env = build_experience_envelope("start_session", "onboard", payload)
    assert env["agent_uuid"] == "u-new"
    assert env["state_summary"]["predecessor_uuid"] == "u-prior"
    assert "parent_agent_id" in env["next_action"]
    assert env["raw_governance"] is payload


def test_sync_state_envelope_summarizes_decision_and_risk():
    payload = {
        "success": True,
        "decision": {
            "action": "continue",
            "reason": "Low risk; continue the bounded task.",
            "margin": 0.31,
            "nearest_edge": "S_min",
        },
        "metrics": {"coherence": 0.82, "risk_score": 0.21, "verdict": "safe"},
        "health_status": "healthy",
    }
    env = build_experience_envelope("sync_state", "process_agent_update", payload)
    assert env["action_summary"] == {
        "action": "proceed",
        "verdict": "safe",
        "reason": "Low risk; continue the bounded task.",
        "risk_score": 0.21,
        "verdict_confidence": "unspecified",
    }
    summary = env["state_summary"]
    assert summary["action"] == "continue"
    assert summary["coherence"] == 0.82
    assert env["risk_summary"].startswith("risk low")
    assert "recovery_hint" not in env  # healthy state stays quiet
    assert env["response_options"]["routine"] == "compact"
    assert env["response_options"]["interpreted_summary"] == "standard"
    assert env["response_options"]["actionable_diagnostics"] == "mirror"
    assert env["_response_size"]["approx_bytes"] > 0
    assert env["_response_size"]["measured_without_self"] is True


def test_sync_state_envelope_emits_recovery_hint_when_degraded():
    payload = {
        "success": True,
        "verdict": {"value": "pause"},
        "metrics": {"coherence": 0.45, "risk_score": 0.75},
    }
    env = build_experience_envelope("sync_state", "process_agent_update", payload)
    assert "self_recovery_review" in env["recovery_hint"]
    assert env["risk_summary"].startswith("risk high")


def test_sync_state_envelope_does_not_warn_on_low_risk_proceed_mid_coherence():
    """Legacy C(V) is diagnostic; low risk should not trigger recovery."""
    payload = {
        "success": True,
        "verdict": {"value": "proceed"},
        "coherence": 0.49,
        "risk_score": 0.26,
    }
    env = build_experience_envelope("sync_state", "process_agent_update", payload)
    assert env["risk_summary"] == "risk low (0.26), coherence diagnostic 0.49"
    assert "recovery_hint" not in env


def test_sync_state_envelope_near_edge_proceed_hint_does_not_say_pause():
    payload = {
        "success": True,
        "verdict": {"value": "proceed"},
        "margin": "near_edge",
        "metrics": {"coherence": 0.51, "risk_score": 0.43},
    }
    env = build_experience_envelope("sync_state", "process_agent_update", payload)
    assert "recovery_hint" in env
    assert "pause" not in env["recovery_hint"].lower()
    assert "only if work stalls" in env["recovery_hint"]


def test_sync_state_envelope_surfaces_provisional_verdict_caveat():
    """A clean verdict riding on the cold-start prior must carry its caveat at
    the envelope surface, not three levels deep in risk_attribution."""
    payload = {
        "success": True,
        "decision": {"action": "proceed"},
        "metrics": {"coherence": 0.7, "risk_score": 0.05, "verdict": "safe"},
        "health_status": "healthy",
        "risk_attribution": {
            "primary_driver": "phi_cold_start",
            "verdict": "safe",
            "discriminability": {
                "baselined": False,
                "non_discriminative": True,
                "updates_until_baseline": 4,
            },
        },
    }
    env = build_experience_envelope("sync_state", "process_agent_update", payload)
    assert "verdict_caveat" in env
    assert "provisional" in env["verdict_caveat"].lower()
    assert "4 more check-in" in env["verdict_caveat"]
    assert env["state_summary"]["verdict_provisional"] is True
    assert env["action_summary"]["verdict_confidence"] == "provisional"
    assert env["action_summary"]["evidence_basis"] == "phi_cold_start"


def test_sync_state_full_payload_uses_primary_eisv_source_for_verdict_maturity():
    """Full mode need not depend on the optional risk-attribution wrapper.

    The canonical metrics source is enough to preserve the same cold-start
    caveat that the filtered modes carry in their wrapped verdict evidence.
    """
    payload = {
        "success": True,
        "decision": {"action": "proceed", "sub_action": "approve"},
        "metrics": {
            "coherence": 0.49,
            "risk_score": 0.05,
            "verdict": "safe",
            "primary_eisv_source": "ode_fallback",
        },
    }

    env = build_experience_envelope(
        "sync_state",
        "process_agent_update",
        payload,
        {"response_mode": "full"},
    )

    assert env["action_summary"]["verdict_confidence"] == "provisional"
    assert env["action_summary"]["evidence_basis"] == "ode_fallback"
    assert "metrics.primary_eisv_source" in env["verdict_caveat"]
    assert env["state_summary"]["verdict_provisional"] is True


def test_sync_state_compact_envelope_lifts_provisional_evidence_and_legacy_diagnostic():
    """Compact mode keeps cold-start evidence inside metrics.verdict; the
    action-first envelope must lift it without requiring a full payload."""
    payload = {
        "success": True,
        "_mode": "compact",
        "decision": {
            "action": "proceed",
            "sub_action": "approve",
            "reason": "Low risk (0.05)",
        },
        "metrics": {
            "coherence": 0.49,
            "coherence_source": "legacy_tanh_v",
            "coherence_role": "ode_control_feedback",
            "risk_score": 0.05,
            "verdict": {
                "value": "safe",
                "meaning": "Behavioral assessment: low risk. Provisional.",
                "evidence": {
                    "grade": "provisional",
                    "basis": "ode_fallback",
                },
            },
        },
    }

    env = build_experience_envelope("sync_state", "process_agent_update", payload)

    assert env["action_summary"] == {
        "action": "proceed",
        "sub_action": "approve",
        "verdict": "safe",
        "reason": "Low risk (0.05)",
        "risk_score": 0.05,
        "verdict_confidence": "provisional",
        "evidence_basis": "ode_fallback",
    }
    assert "cold-start prior" in env["verdict_caveat"]
    assert "metrics.verdict.evidence" in env["verdict_caveat"]
    assert env["state_summary"]["verdict_provisional"] is True
    assert env["legacy_diagnostics"] == {
        "source": "legacy_tanh_v",
        "role": "ode_control_feedback",
        "health_evidence": False,
        "interpretation": (
            "Compatibility ODE controller feedback; diagnostic context, "
            "not a behavioral health score."
        ),
        "coherence": 0.49,
    }
    # state_summary.coherence carries the same "not health-rated" badge inline
    # (matching check_working_state's lite presentation of the same legacy
    # field) instead of a bare float a reader has to cross-reference against
    # legacy_diagnostics to interpret correctly.
    assert env["state_summary"]["coherence"] == {
        "value": 0.49,
        "status": "⚪ legacy control feedback (not health-rated)",
        "source": "legacy_tanh_v",
        "role": "ode_control_feedback",
    }


def test_sync_state_envelope_coherence_stays_a_bare_float_when_not_legacy():
    """Only the known-legacy tanh coherence gets the inline badge; an
    ordinary coherence reading (no legacy source/role) is left as a plain
    number, matching test_sync_state_envelope_summarizes_decision_and_risk."""
    payload = {
        "success": True,
        "decision": {"action": "proceed"},
        "metrics": {"coherence": 0.82, "risk_score": 0.21},
    }
    env = build_experience_envelope("sync_state", "process_agent_update", payload)
    assert env["state_summary"]["coherence"] == 0.82


@pytest.mark.parametrize(
    ("requested_mode", "resolved_mode"),
    (("minimal", "minimal"), ("standard", "standard"), ("interpreted", "standard")),
)
def test_filtered_modes_keep_action_and_cold_start_caveat(
    requested_mode: str,
    resolved_mode: str,
):
    """Bare and interpreted summary modes must not erase verdict assurance."""
    source = {
        "success": True,
        "status": "healthy",
        "decision": {
            "action": "proceed",
            "sub_action": "approve",
            "reason": "Low risk on the cold-start prior.",
            "margin": "settling",
            "nearest_edge": None,
        },
        "metrics": {
            "E": 0.5,
            "I": 0.5,
            "S": 0.3,
            "V": 0.0,
            "phi": 0.7,
            "coherence": 0.49,
            "coherence_source": "legacy_tanh_v",
            "coherence_role": "ode_control_feedback",
            "risk_score": 0.05,
            "verdict": "safe",
            "primary_eisv_source": "ode_fallback",
        },
    }

    formatted = format_response(
        deepcopy(source),
        {"response_mode": requested_mode},
        task_type="feature",
    )
    env = build_experience_envelope(
        "sync_state",
        "process_agent_update",
        formatted,
        {"response_mode": requested_mode},
    )

    assert formatted["_mode"] == resolved_mode
    assert env["action_summary"]["action"] == "proceed"
    assert env["action_summary"]["sub_action"] == "approve"
    assert env["action_summary"]["verdict"] == "safe"
    assert env["action_summary"]["verdict_confidence"] == "provisional"
    assert env["action_summary"]["evidence_basis"] == "ode_fallback"
    assert env["state_summary"]["action"] == "proceed"
    assert env["state_summary"]["verdict_provisional"] is True
    assert "raw_governance.verdict.evidence" in env["verdict_caveat"]
    assert env["legacy_diagnostics"]["health_evidence"] is False


def test_sync_state_envelope_no_caveat_when_baseline_warm():
    """Once the behavioral baseline is warm and discriminative, the verdict is
    authoritative — no provisional caveat, no state_summary flag."""
    payload = {
        "success": True,
        "decision": {"action": "proceed"},
        "metrics": {"coherence": 0.8, "risk_score": 0.1, "verdict": "safe"},
        "health_status": "healthy",
        "risk_attribution": {
            "primary_driver": "behavioral_assessment",
            "verdict": "safe",
            "discriminability": {
                "baselined": True,
                "non_discriminative": False,
                "updates_until_baseline": 0,
            },
        },
    }
    env = build_experience_envelope("sync_state", "process_agent_update", payload)
    assert "verdict_caveat" not in env
    assert "verdict_provisional" not in env["state_summary"]
    assert env["action_summary"]["verdict_confidence"] == "non_provisional"
    assert env["action_summary"]["evidence_basis"] == "behavioral_assessment"


def test_sync_state_envelope_surfaces_reflection():
    """The mirror reflection is a real actionable signal but lives only under
    _mirror_reflection/reflection in the raw payload — lift it to the surface."""
    full_mode = {
        "success": True,
        "decision": {"action": "proceed"},
        "metrics": {"coherence": 0.6, "risk_score": 0.2},
        "_mirror_reflection": "You're near a basin boundary. Proceed carefully.",
    }
    env = build_experience_envelope("sync_state", "process_agent_update", full_mode)
    assert env["reflection"] == "You're near a basin boundary. Proceed carefully."

    mirror_mode = dict(full_mode)
    del mirror_mode["_mirror_reflection"]
    mirror_mode["reflection"] = "You're close to a governance edge."
    env = build_experience_envelope("sync_state", "process_agent_update", mirror_mode)
    assert env["reflection"] == "You're close to a governance edge."


def test_sync_state_envelope_surfaces_discoveries():
    payload = {
        "success": True,
        "relevant_discoveries": [
            {"discovery_id": f"d{i}", "summary": f"finding {i}"} for i in range(5)
        ],
    }
    env = build_experience_envelope(
        "sync_state",
        "process_agent_update",
        payload,
        {"include_memory_suggestions": True},
    )
    assert len(env["memory_suggestions"]) == 3  # truncated
    assert env["memory_suggestions"][0]["discovery_id"] == "d0"


def test_sync_state_envelope_omits_memory_suggestions_by_default():
    payload = {
        "success": True,
        "relevant_discoveries": [
            {"discovery_id": "d1", "summary": "prior work"},
        ],
    }
    env = build_experience_envelope("sync_state", "process_agent_update", payload)
    assert "memory_suggestions" not in env


def test_metrics_envelope_maps_existing_friendly_fields():
    payload = {
        "success": True,
        "verdict": {"verdict": "proceed", "explanation": "stable"},
        "guidance": "keep going",
        "next_action": {
            "tool": "process_agent_update",
            "example": "process_agent_update(response_text='Starting work')",
            "note": "get_governance_metrics is read-only",
        },
        "status": "healthy",
        "E": 0.7,
        "I": 0.8,
        "S": 0.2,
        "V": 0.0,
        "risk_score": 0.1,
    }
    env = build_experience_envelope("check_working_state", "get_governance_metrics", payload)
    assert env["next_action"]["tool"] == "sync_state"
    assert "sync_state(" in env["next_action"]["example"]
    assert "check_working_state" in env["next_action"]["note"]
    assert env["state_summary"]["verdict"] == "proceed"
    assert env["state_summary"]["E"] == 0.7
    assert env["state_summary"]["risk_score"] == 0.1
    assert "raw_governance" not in env
    assert env["raw_governance_available"] is True
    assert payload["verdict"] == {"verdict": "proceed", "explanation": "stable"}


def test_metrics_envelope_translates_state_summary_coaching():
    """Glossary coaching lifted into state_summary speaks the friendly register.

    An uninitialized verdict's next_action names process_agent_update (the
    canonical tool); at the friendly surface that read as a register mismatch
    (dogfood 2026-08-20). state_summary now runs through the same alias
    translation as next_action; scalar fields pass through untouched.
    """
    payload = {
        "success": True,
        "verdict": {
            "verdict": "uninitialized",
            "meaning": "Agent has no recorded state yet.",
            "next_action": "Submit one process_agent_update to activate governance.",
        },
        "status": "uninitialized",
        "E": 0.5,
    }
    env = build_experience_envelope("check_working_state", "get_governance_metrics", payload)
    assert "sync_state" in env["state_summary"]["next_action"]
    assert "process_agent_update" not in env["state_summary"]["next_action"]
    assert env["state_summary"]["E"] == 0.5
    # the source payload keeps its canonical wording
    assert "process_agent_update" in payload["verdict"]["next_action"]


def test_metrics_envelope_full_escape_hatch_preserves_raw_payload():
    payload = {"success": True, "E": 0.7, "verdict": "proceed"}
    env = build_experience_envelope(
        "check_working_state",
        "get_governance_metrics",
        payload,
        {"lite": False},
    )
    assert env["raw_governance"] is payload
    assert "raw_governance_hint" not in env


def test_search_envelope_counts_and_suggests():
    payload = {
        "success": True,
        "results": [{
            "id": "d1",
            "summary": "prior art",
            "type": "observation",
            "status": "open",
            "severity": "medium",
            "tags": ["response-ux"],
            "created_at": "2026-08-22T00:00:00+00:00",
            "updated_at": "2026-08-23T00:00:00+00:00",
            "has_details": True,
            "details_preview": "Bounded preview",
            "has_more_details": True,
        }],
        "total_count": 1,
        "discovery_retrieval_options": {
            "current_tier": "digest",
            "digest": "include_details=false",
            "open_one": "knowledge(action='details', discovery_id='...')",
            "all_inline": "include_details=true (can be large)",
        },
    }
    env = build_experience_envelope("search_shared_memory", "knowledge", payload)
    assert "1 prior discoveries matched" in env["next_action"]
    assert "store_finding(" in env["next_action"]
    assert env["memory_suggestions"][0]["summary"] == "prior art"
    assert env["memory_suggestions"][0]["status"] == "open"
    assert env["memory_suggestions"][0]["tags"] == ["response-ux"]
    assert "details_preview" not in env["memory_suggestions"][0]
    assert env["state_summary"]["result_tier"] == "digest"
    assert env["state_summary"]["results_shown_in_digest"] == 1
    assert env["discovery_retrieval_options"]["current_tier"] == "digest"
    assert env["response_options"] == {
        "current": "lean",
        "digest": "lean",
        "diagnostic_digest": "compact",
        "complete_result_set": "full",
        "all_inline_details": "response_mode='full' + include_details=true",
    }
    assert "raw_governance" not in env
    assert "response_mode='full'" in env["raw_governance_hint"]


def test_compact_search_does_not_claim_details_it_omits():
    payload = {
        "success": True,
        "count": 1,
        "discoveries": [
            {
                "id": "d1",
                "summary": "prior art",
                "details": "large inline details",
                "details_preview": "large inline...",
                "has_details": True,
                "has_more_details": True,
            }
        ],
        "discovery_retrieval_options": {
            "current_tier": "full_inline",
            "digest": "include_details=false",
            "open_one": "knowledge(action='details', discovery_id='...')",
            "all_inline": "include_details=true (can be large)",
        },
    }

    env = build_experience_envelope(
        "search_shared_memory",
        "knowledge",
        payload,
        {"response_mode": "compact", "include_details": True},
    )

    options = env["discovery_retrieval_options"]
    assert options["requested_tier"] == "full_inline"
    assert options["current_tier"] == "digest"
    assert options["details_serialized"] is True
    assert options["details_included"] is False
    assert options["details_omitted_by"] == "response_mode='compact'"
    assert "response_mode='full'" in options["all_inline"]
    assert env["state_summary"]["result_tier"] == "digest"
    assert "details" not in env["memory_suggestions"][0]
    assert "raw_governance" not in env


def test_compact_search_suppresses_details_before_serialization():
    payload = {
        "success": True,
        "results": [{
            "id": "d1",
            "summary": "prior art",
            "details_preview": "bounded preview",
            "has_details": True,
        }],
        "total_count": 1,
        "discovery_retrieval_options": {
            "current_tier": "digest",
            "digest": "include_details=false",
            "open_one": "knowledge(action='details', discovery_id='...')",
            "all_inline": "include_details=true (can be large)",
        },
    }
    arguments = {
        "response_mode": "compact",
        "include_details": False,
        "_friendly_search_detail_policy": "digest_before_serialization",
        "_friendly_search_details_requested": True,
    }

    env = build_experience_envelope(
        "search_shared_memory", "knowledge", payload, arguments
    )

    options = env["discovery_retrieval_options"]
    assert options["current_tier"] == "digest"
    assert options["requested_tier"] == "full_inline"
    assert options["details_serialized"] is False
    assert options["details_included"] is False
    assert options["detail_policy"] == "digest_before_serialization"
    assert options["details_omitted_by"] == (
        "response_mode='compact' before serialization"
    )


def test_full_sync_state_reports_large_response_and_reduction_mode():
    payload = {
        "success": True,
        "decision": {"action": "proceed", "reason": "Low risk"},
        "metrics": {"risk_score": 0.1, "verdict": "safe"},
        "large_diagnostic": "x" * 5_000,
    }

    env = build_experience_envelope(
        "sync_state",
        "process_agent_update",
        payload,
        {"response_mode": "full", "include_memory_suggestions": True},
    )

    assert env["_response_size"]["size_class"] in {"medium", "large"}
    assert "response_mode='compact'" in env["_response_size"]["reduce_with"]


@pytest.mark.parametrize(
    ("mode", "wire_limit"),
    (("compact", 4_000), ("standard", 6_000), ("interpreted", 6_000)),
)
def test_agent_summary_modes_stay_small_with_large_audit_gates(
    mode: str,
    wire_limit: int,
):
    """Persisted self-contained gates must not turn summaries into near-full."""
    gate = {
        "schema": "eisv.cold-start-confirmation.v1",
        "measurement_phase": "behavioral_ready",
        "measurement_ready": True,
        "behavioral_confidence": 0.6,
        "is_baselined": True,
        "primary_driver": "behavioral_assessment",
        "primary_eisv_source": "behavioral",
        "eligible": False,
        "outcome": "ineligible",
        "note": "x" * 5_000,
        "original_decision": {"reason": "x" * 5_000},
    }
    source = {
        "success": True,
        "status": "healthy",
        "health_status": "healthy",
        "decision": {
            "action": "proceed",
            "sub_action": "approve",
            "reason": "Low risk.",
            "margin": "comfortable",
            "nearest_edge": None,
        },
        "metrics": {
            "E": 0.6,
            "I": 0.7,
            "S": 0.2,
            "V": -0.1,
            "coherence": 0.49,
            "coherence_source": "legacy_tanh_v",
            "coherence_role": "ode_control_feedback",
            "risk_score": 0.08,
            "verdict": "safe",
            "primary_eisv_source": "behavioral",
        },
        "policy_evaluation": {
            "policy_name": "monitor_decision",
            "action": "proceed",
            "sub_action": "approve",
            "inputs": {
                "primary_eisv_source": "behavioral",
                "risk_score": 0.08,
                "verdict": "safe",
            },
            "maturity_gate": gate,
        },
        "enforcement": {
            "schema": "governance.enforcement.v1",
            "scope": "runtime_circuit_breaker",
            "requested": False,
            "applied": False,
            "basis": "advisory_policy",
            "maturity_gate": gate,
        },
    }

    formatted = format_response(
        deepcopy(source),
        {"response_mode": mode},
        task_type="feature",
    )
    env = build_experience_envelope(
        "sync_state",
        "process_agent_update",
        formatted,
        {"response_mode": mode},
    )

    wire_bytes = len(json.dumps(env, ensure_ascii=False).encode("utf-8"))
    assert wire_bytes < wire_limit
    assert env["_response_size"]["size_class"] in {"small", "medium"}
    assert "policy_evaluation" not in formatted
    assert "enforcement" not in formatted
    assert "response_mode='full'" in formatted["_raw_available"]


def test_search_envelope_full_escape_hatch_preserves_raw_payload():
    payload = {
        "success": True,
        "count": 1,
        "discoveries": [{"id": "d1", "summary": "prior art"}],
    }
    env = build_experience_envelope(
        "search_shared_memory",
        "knowledge",
        payload,
        {"response_mode": "full"},
    )
    assert env["raw_governance"] is payload
    assert "raw_governance_available" not in env
    # raw_governance.discoveries is already the full result set; a trimmed
    # top-N copy under memory_suggestions would just serialize the same
    # results twice in the same response.
    assert "memory_suggestions" not in env
    assert env["raw_governance"]["discoveries"][0]["summary"] == "prior art"


def test_search_envelope_compact_mode_keeps_memory_suggestions():
    """The dedup only applies once raw_governance is actually attached —
    the default compact path still needs memory_suggestions as its only
    view of the results."""
    payload = {
        "success": True,
        "count": 1,
        "discoveries": [{"id": "d1", "summary": "prior art"}],
    }
    env = build_experience_envelope("search_shared_memory", "knowledge", payload)
    assert "raw_governance" not in env
    assert env["memory_suggestions"][0]["summary"] == "prior art"


def test_metrics_envelope_full_mode_keeps_memory_suggestions():
    """Knowledge-search dedup does not suppress an explicit check-in recall
    opt-in, even when the check-in also requests the full governance payload."""
    payload = {
        "success": True,
        "decision": {"action": "proceed"},
        "metrics": {"coherence": 0.6, "risk_score": 0.2},
        "relevant_discoveries": {
            "discoveries": [{"discovery_id": "d1", "summary": "prior art"}],
        },
    }
    env = build_experience_envelope(
        "sync_state",
        "process_agent_update",
        payload,
        {"response_mode": "full", "include_memory_suggestions": True},
    )
    assert env["raw_governance"] is payload
    assert env["memory_suggestions"][0]["summary"] == "prior art"


def test_search_envelope_promotes_low_confidence():
    payload = {
        "success": True,
        "count": 2,
        "search_mode_used": "hybrid_rrf",
        "discoveries": [{"id": "d1", "summary": "semantic lead"}],
        "low_confidence": True,
        "confidence_note": "Semantic-only matches; verify before use.",
    }
    env = build_experience_envelope("search_shared_memory", "knowledge", payload)
    assert "exploratory low-confidence" in env["next_action"]
    assert env["low_confidence"] is True
    assert env["confidence_note"] == "Semantic-only matches; verify before use."
    assert env["state_summary"]["low_confidence"] is True
    assert env["state_summary"]["confidence_note"] == "Semantic-only matches; verify before use."
    assert env["memory_suggestions"][0]["summary"] == "semantic lead"


def test_search_envelope_counts_nested_raw_governance_payload():
    payload = {
        "success": True,
        "tool": "search_shared_memory",
        "raw_governance": {
            "success": True,
            "count": 5,
            "discoveries": [{"id": "d1", "summary": "prior art"}],
        },
    }
    env = build_experience_envelope("search_shared_memory", "knowledge", payload)
    assert "5 prior discoveries matched" in env["next_action"]
    assert env["memory_suggestions"][0]["summary"] == "prior art"


def test_store_finding_envelope_reports_write_instead_of_empty_search():
    payload = {
        "success": True,
        "message": "Discovery stored for agent 'agent-1'",
        "discovery_id": "d-new",
        "discovery": {
            "id": "d-new",
            "type": "bug_found",
            "status": "open",
            "severity": "medium",
            "summary": "write envelope bug",
        },
        "_resolve_when_done": (
            "When this is addressed, close the loop: "
            "knowledge(action='update', discovery_id='d-new', status='resolved')"
        ),
    }

    env = build_experience_envelope(
        "store_finding",
        "knowledge",
        payload,
        {"summary": "write envelope bug"},
    )

    assert env["message"] == "Discovery stored for agent 'agent-1'"
    assert env["discovery_id"] == "d-new"
    assert env["state_summary"] == {
        "type": "bug_found",
        "status": "open",
        "severity": "medium",
        "summary": "write envelope bug",
        "discovery_id": "d-new",
        "message": "Discovery stored for agent 'agent-1'",
    }
    assert "update_finding(" in env["next_action"]
    assert "No prior discoveries matched" not in env["next_action"]
    assert env["raw_governance"] is payload


def test_update_finding_envelope_reports_terminal_status_and_id():
    payload = {
        "success": True,
        "message": "Discovery 'd-existing' status updated to 'resolved'",
        "discovery": {
            "id": "d-existing",
            "type": "bug_found",
            "status": "resolved",
            "severity": "medium",
            "summary": "write envelope bug",
            "updated_at": "2026-08-23T01:00:00+00:00",
            "resolved_at": "2026-08-23T01:00:00+00:00",
        },
    }

    env = build_experience_envelope(
        "update_finding",
        "knowledge",
        payload,
        {"discovery_id": "d-existing", "status": "resolved"},
    )

    assert env["message"] == "Discovery 'd-existing' status updated to 'resolved'"
    assert env["discovery_id"] == "d-existing"
    assert env["state_summary"]["status"] == "resolved"
    assert env["state_summary"]["resolved_at"] == "2026-08-23T01:00:00+00:00"
    assert "is now 'resolved'" in env["next_action"]
    assert "knowledge(action='details'" in env["next_action"]
    assert "No prior discoveries matched" not in env["next_action"]
    assert env["raw_governance"] is payload


def test_update_finding_envelope_falls_back_to_argument_discovery_id():
    payload = {
        "success": True,
        "message": "Discovery 'd-existing' updated",
        "discovery": None,
    }

    env = build_experience_envelope(
        "update_finding",
        "knowledge",
        payload,
        {"discovery_id": "d-existing", "details": "new evidence"},
    )

    assert env["discovery_id"] == "d-existing"
    assert env["state_summary"]["discovery_id"] == "d-existing"
    assert "was updated" in env["next_action"]
    assert "No prior discoveries matched" not in env["next_action"]


def test_record_result_envelope_lifts_outcome():
    payload = {
        "success": True,
        "outcome_id": "o-1",
        "outcome_type": "task_completed",
        "outcome_score": 1.0,
        "corroboration_grade": "claim_only",
        "evidence_weight": 0.1,
        "claim_risk": "high",
        "eisv_snapshot": {
            "primary_eisv": {"E": 0.7, "I": 0.6, "S": 0.8, "V": 0.1},
            "primary_eisv_source": "ode_fallback",
            "state_semantics": {"long": "kept under raw_governance only"},
        },
    }
    env = build_experience_envelope("record_result", "outcome_event", payload)
    assert env["state_summary"]["outcome_id"] == "o-1"
    assert env["state_summary"]["outcome_type"] == "task_completed"
    assert env["state_summary"]["corroboration_grade"] == "claim_only"
    assert env["state_summary"]["claim_risk"] == "high"
    assert env["state_summary"]["working_state"]["E"] == 0.7
    assert env["state_summary"]["working_state"]["source"] == "ode_fallback"
    assert "state_semantics" not in env["state_summary"]["working_state"]


def test_request_review_envelope_preserves_saved_brief_next_call():
    payload = {
        "success": True,
        "session_id": "sess-42",
        "phase": "thesis",
        "whose_move": "YOURS — your thesis is owed; the saved brief can be reused",
        "next_call": (
            "dialectic(action='thesis', session_id='sess-42', "
            "use_brief_as_thesis=true)"
        ),
    }
    env = build_experience_envelope("request_review", "dialectic", payload)
    assert "sess-42" in env["next_action"]
    assert "use_brief_as_thesis=true" in env["next_action"]
    assert env["state_summary"]["phase"] == "thesis"
    assert env["state_summary"]["whose_move"].startswith("YOURS")


def test_request_review_envelope_does_not_reopen_resolved_one_call_review():
    payload = {
        "success": True,
        "session_id": "sess-resolved",
        "phase": "resolved",
        "one_call_review": True,
        "thesis_source": "issue_description",
        "review_verdict": "resume",
        "whose_move": "nobody — review resolved in this call",
    }

    env = build_experience_envelope("request_review", "dialectic", payload)

    assert "resolved" in env["next_action"]
    assert "action='thesis'" not in env["next_action"]
    assert env["state_summary"]["review_verdict"] == "resume"
    assert env["state_summary"]["thesis_source"] == "issue_description"


def test_request_review_envelope_preserves_reviewer_wait_guidance():
    payload = {
        "success": True,
        "session_id": "sess-reviewer",
        "phase": "antithesis",
        "whose_move": (
            "the reviewer's — an independent reviewer was spawned; poll "
            "dialectic(action='get', session_id='sess-reviewer')"
        ),
    }

    env = build_experience_envelope("request_review", "dialectic", payload)

    assert "reviewer" in env["next_action"]
    assert "action='thesis'" not in env["next_action"]


# ---------------------------------------------------------------------------
# Pipeline integration: the seam itself
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_runs_post_execution_steps(monkeypatch):
    """run_tool_dispatch_pipeline applies post_execution_steps to the
    handler result and survives a raising step."""
    from src.services.tool_dispatch_service import run_tool_dispatch_pipeline
    import src.mcp_handlers as mh

    async def fake_handler(arguments):
        return _result({"success": True, "marker": "raw"})

    monkeypatch.setitem(mh.TOOL_HANDLERS, "tmp_envelope_tool", fake_handler)

    async def reshape(name, arguments, ctx, result):
        data = _parse(result)
        data["reshaped"] = True
        return [TextContent(type="text", text=json.dumps(data))]

    async def explode(name, arguments, ctx, result):
        raise RuntimeError("step bug")

    out = await run_tool_dispatch_pipeline(
        name="tmp_envelope_tool",
        arguments={},
        pre_steps=[],
        post_steps=[],
        post_execution_steps=[explode, reshape],
    )
    data = _parse(out)
    assert data["marker"] == "raw"
    assert data["reshaped"] is True  # raising step skipped, next still ran


# ─── dogfood 2026-08-16: recovery-hint honesty + prediction_id threading ────


def test_sync_state_envelope_safe_verdict_tight_margin_low_risk_names_margin_not_risk():
    """Mirror-mode payloads resolve action via the verdict ('safe') and carry a
    margin flag; with measured risk far below the ceiling the hint must talk
    about the margin, never claim elevated risk (observed live: risk 0.00
    rendered 'Risk is elevated')."""
    payload = {
        "success": True,
        "verdict": {"value": "safe"},
        "margin": "tight",
        "coherence": 0.48,
        "risk_score": 0.0,
    }
    env = build_experience_envelope("sync_state", "process_agent_update", payload)
    assert "recovery_hint" in env
    assert "Risk is elevated" not in env["recovery_hint"]
    assert "only if work stalls" in env["recovery_hint"]


def test_sync_state_envelope_attention_without_action_low_risk_names_margin():
    payload = {"success": True, "margin": "boundary", "metrics": {"risk_score": 0.1}}
    env = build_experience_envelope("sync_state", "process_agent_update", payload)
    assert "recovery_hint" in env
    assert "Risk is elevated" not in env["recovery_hint"]
    assert "near an edge" in env["recovery_hint"]


def test_sync_state_envelope_risky_unresolved_action_keeps_elevated_wording():
    payload = {"success": True, "metrics": {"risk_score": 0.55}}
    env = build_experience_envelope("sync_state", "process_agent_update", payload)
    assert "Risk is elevated" in env["recovery_hint"]


def test_sync_state_envelope_next_action_threads_prediction_id():
    payload = {
        "success": True,
        "decision": {"action": "proceed"},
        "prediction_id": "abc-123",
    }
    env = build_experience_envelope("sync_state", "process_agent_update", payload)
    assert "prediction_id='abc-123'" in env["next_action"]


def test_sync_state_envelope_next_action_generic_without_prediction_id():
    payload = {"success": True, "decision": {"action": "proceed"}}
    env = build_experience_envelope("sync_state", "process_agent_update", payload)
    assert "record_result(...)" in env["next_action"]
    assert "prediction_id" not in env["next_action"]


def test_sync_state_envelope_prediction_id_composes_with_review_nudge():
    payload = {
        "success": True,
        "decision": {"action": "proceed"},
        "prediction_id": "abc-123",
        "review_suggested": {"trigger": "low_confidence"},
    }
    env = build_experience_envelope("sync_state", "process_agent_update", payload)
    assert "prediction_id='abc-123'" in env["next_action"]
    assert "request_review" in env["next_action"]
