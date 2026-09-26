"""Agent-experience response envelope (middleware/envelope_step.py).

Alias-gated: only calls invoked via an experience alias (start_session,
sync_state, check_working_state, search_shared_memory, store_finding,
update_finding, record_result, request_review) are reshaped. The two contract guarantees pinned here:

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
    raw = _result({
        "success": True,
        "agent_uuid": "u-1",
        "client_session_id": "s-1",
        "is_new": True,
        "identity_resolution_outcome": "minted_force_new",
        "identity_assurance": {
            "tier": "weak",
            "caller_proven": False,
            "baseline": "fresh_identity",
        },
    })
    out = await apply_experience_envelope(
        "onboard", {}, _ctx("start_session"), raw
    )
    data = _parse(out)
    assert data["tool"] == "start_session"
    assert data["agent_uuid"] == "u-1"
    assert data["client_session_id"] == "s-1"
    # A mint that went as asked does not repeat the canonical record beneath
    # the lifts, and says so.
    assert "raw_governance" not in data
    assert "raw_governance_available" not in data  # not fetchable after the mint
    assert data["response_shape"] == "routine"
    assert "next_action" in data

    # A mint the payload cannot show went as asked keeps the record.
    unproven = _parse(await apply_experience_envelope(
        "onboard", {}, _ctx("start_session"),
        _result({"success": True, "agent_uuid": "u-1", "client_session_id": "s-1"}),
    ))
    assert unproven["raw_governance"]["agent_uuid"] == "u-1"
    assert unproven["response_shape"] == "full"

    full = _parse(await apply_experience_envelope(
        "onboard", {"response_mode": "full"}, _ctx("start_session"), raw
    ))
    assert full["raw_governance"]["agent_uuid"] == "u-1"


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
async def test_a_real_typed_refusal_passes_through():
    """#2134. The case above intends to cover typed refusals, but its
    ``identity_required`` fixture carries ``success: False`` — a shape no
    emission point produces. A real refusal is built by
    ``strict_identity_refusal_payload`` and is deliberately "a structured
    success-shape, not an error", so it carries ``success: true`` with no
    ``error`` key and slipped past the guard.

    Build the payload from the real single-sourced builder, not a hand-written
    dict, so this cannot drift from the shape it is protecting.
    """
    from src.mcp_handlers.identity_bootstrap import strict_identity_refusal_payload
    from src.mcp_handlers.response_base import success_response

    raw = success_response(strict_identity_refusal_payload("process_agent_update"))
    payload = json.loads(raw[0].text)
    # Guard the premise: if the refusal ever becomes error-shaped, this test
    # stops proving anything and should be revisited rather than silently pass.
    assert payload["success"] is True and "error" not in payload

    out = await apply_experience_envelope(
        "process_agent_update", {}, _ctx("sync_state"), raw
    )
    # Not an identity-return: the tool name is translated to the invoked alias
    # (see test_a_refusal_names_the_tool_the_caller_invoked). Everything else
    # must survive verbatim, which is what the field assertions below pin.
    d = json.loads(out[0].text)
    assert d["status"] == "identity_required"
    for field in ("hint", "next_step", "safe_options", "do_not"):
        assert field in d, field
    # The generic check-in guidance must never be layered over a refusal.
    assert "next_action" not in d
    # Both shipped SDKs read the refusal at the top level: the Python SDK keys
    # on rollout_flag, the Elixir SDK on status. Enveloping broke both.
    assert d["rollout_flag"] == "STRICT_IDENTITY_REQUIRED"


@pytest.mark.asyncio
async def test_a_refusal_names_the_tool_the_caller_invoked():
    """The refusal's own `tool` is the canonical name, because that is what
    the emission point knows. Passing it through unchanged would answer an
    agent about a tool it never called — the register-mismatch the envelope
    already guards against elsewhere. The Python SDK surfaces this field
    verbatim in IdentityRefusedError, so it reaches a human.
    """
    from src.mcp_handlers.identity_bootstrap import strict_identity_refusal_payload
    from src.mcp_handlers.response_base import success_response

    raw = success_response(strict_identity_refusal_payload("process_agent_update"))
    assert json.loads(raw[0].text)["tool"] == "process_agent_update"

    out = await apply_experience_envelope(
        "process_agent_update", {}, _ctx("sync_state"), raw
    )
    d = json.loads(out[0].text)
    assert d["tool"] == "sync_state"
    # Nothing else may be reshaped on the way through.
    for field in ("status", "hint", "next_step", "safe_options", "do_not", "rollout_flag"):
        assert field in d, field


@pytest.mark.asyncio
async def test_a_refusal_on_the_canonical_name_is_returned_untouched():
    """When the caller used the canonical name there is nothing to translate,
    so the payload must not be rebuilt at all."""
    from src.mcp_handlers.identity_bootstrap import strict_identity_refusal_payload
    from src.mcp_handlers.response_base import success_response

    raw = success_response(strict_identity_refusal_payload("get_governance_metrics"))
    out = await apply_experience_envelope(
        "get_governance_metrics", {}, _ctx("get_governance_metrics"), raw
    )
    assert out is raw


@pytest.mark.asyncio
async def test_every_refusal_status_passes_through():
    """``status`` varies by emission point; the marker is what identifies a
    refusal, so a differently-statused refusal must pass through too."""
    from src.mcp_handlers.identity_bootstrap import strict_identity_refusal_payload
    from src.mcp_handlers.response_base import success_response

    for status in ("identity_required", "lineage_declaration_required"):
        raw = success_response(
            strict_identity_refusal_payload("onboard", status=status)
        )
        out = await apply_experience_envelope(
            "onboard", {}, _ctx("start_session"), raw
        )
        d = json.loads(out[0].text)
        assert d["status"] == status
        assert d["rollout_flag"] == "STRICT_IDENTITY_REQUIRED", status
        # Recovery contract intact for every status the builder can emit.
        for field in ("hint", "next_step", "safe_options", "do_not"):
            assert field in d, (status, field)
        assert "next_action" not in d, status


@pytest.mark.asyncio
async def test_an_ordinary_success_is_still_enveloped():
    """The refusal guard must not swallow normal payloads: only the
    ``rollout_flag`` marker may divert, and nothing else writes it."""
    raw = _result({"success": True, "status": "healthy", "metrics": {"E": 0.5}})
    out = await apply_experience_envelope(
        "process_agent_update", {}, _ctx("sync_state"), raw
    )
    assert out is not raw
    assert "next_action" in json.loads(out[0].text)


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


def test_onboard_envelope_does_not_turn_sibling_predecessor_into_parent():
    payload = {
        "success": True,
        "uuid": "u-new",
        "lineage_state": "no_lineage_declared",
        "thread_context": {
            "predecessor": {"uuid": "u-prior"},
            "episode_fork_kind": "sibling_locus",
            "identity_lineage_fork": False,
        },
    }
    env = build_experience_envelope("start_session", "onboard", payload)
    assert env["agent_uuid"] == "u-new"
    assert env["state_summary"]["predecessor_uuid"] == "u-prior"
    assert "co-location does not establish lineage" in env["next_action"]
    assert "Do not use its uuid as parent_agent_id" in env["next_action"]
    # A thread with a predecessor is the case the caller must read, so the
    # whole onboard record comes with it.
    assert env["raw_governance"] is payload
    assert env["response_shape"] == "full"
    full = build_experience_envelope(
        "start_session", "onboard", payload, {"response_mode": "full"}
    )
    assert full["raw_governance"] is payload


def test_onboard_envelope_does_not_redeclare_recorded_lineage():
    payload = {
        "success": True,
        "uuid": "u-new",
        "lineage_state": "declared",
        "thread_context": {
            "predecessor": {"uuid": "u-parent"},
            "episode_fork_kind": "identity_lineage",
            "identity_lineage_fork": True,
        },
    }

    env = build_experience_envelope("start_session", "onboard", payload)

    assert env["state_summary"]["predecessor_uuid"] == "u-parent"
    assert "Declared lineage for this fork is already recorded" in env["next_action"]
    assert "future process" not in env["next_action"]


def test_onboard_envelope_surfaces_recorded_creation_origin():
    payload = {
        "success": True,
        "uuid": "u-new",
        "lineage_state": "no_lineage_declared",
        "onboard_origin": "harness_backstop",
        "onboard_origin_basis": "explicit_argument",
    }

    env = build_experience_envelope("start_session", "onboard", payload)

    assert env["state_summary"]["onboard_origin"] == "harness_backstop"
    assert env["state_summary"]["onboard_origin_basis"] == "explicit_argument"


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
    # Asserted as "names the review path", not as a literal tool name: the
    # literal was `self_recovery_review`, a register=False delegate, so this
    # test passed while the hint it checked was a dead end. Above
    # MAX_RISK_FOR_SELF_RECOVERY review records the reflection and then
    # refuses, so the path named is the dialectic review.
    assert "request_review" in env["recovery_hint"]
    assert "self_recovery(" not in env["recovery_hint"]
    assert env["risk_summary"].startswith("risk high")

    payload["metrics"]["risk_score"] = 0.6  # below the review gate
    env = build_experience_envelope("sync_state", "process_agent_update", payload)
    assert "self_recovery(action='review'" in env["recovery_hint"]


def test_sync_state_envelope_pause_surfaces_action_and_stop_guidance():
    # Mirror-mode pause shape: the action lives under `verdict`, not `decision`.
    payload = {
        "success": True,
        "verdict": {"value": "pause"},
        "prediction_id": "p-1",
        "metrics": {"coherence": 0.5, "risk_score": 1.0},
    }
    env = build_experience_envelope("sync_state", "process_agent_update", payload)
    assert env["action_summary"]["action"] == "pause"
    assert env["state_summary"]["action"] == "pause"
    assert "keep working" not in env["next_action"].lower()
    # Risk 1.0 is above the review gate: route to a dialectic review, not to
    # a self_recovery review that records the reflection and then refuses.
    assert "request_review" in env["next_action"]
    assert "self_recovery(" not in env["next_action"]


def test_sync_state_envelope_proceed_keeps_continuation_guidance():
    payload = {
        "success": True,
        "decision": {"action": "proceed"},
        "prediction_id": "p-2",
        "metrics": {"coherence": 0.5, "risk_score": 0.2},
    }
    env = build_experience_envelope("sync_state", "process_agent_update", payload)
    assert env["state_summary"]["action"] == "proceed"
    assert env["next_action"].startswith("Keep working")
    # next_action names the lifted id rather than repeating it.
    assert env["prediction_id"] == "p-2"
    assert "pass this prediction_id to record_result" in env["next_action"]


def test_every_recovery_hint_names_a_callable_tool():
    """recovery_hint is the first route an agent takes when degraded.

    It is attached to every check-in response, so a hint naming a
    register=False delegate is worse than the same defect on a deprecated
    tool's migration block: the agent reaches it while already stuck. All four
    hints said `self_recovery_review(...)` / `quick_resume()` until 2026-08-29.

    Drives each branch of _recovery_hint and asserts every `name(` token it
    emits is a registered dispatch tool or an alias.
    """
    import re

    from src.mcp_handlers.decorators import get_tool_registry
    from src.mcp_handlers.middleware.envelope_step import _recovery_hint
    from src.mcp_handlers.tool_stability import list_all_aliases

    callable_names = set(get_tool_registry()) | set(list_all_aliases())
    call_token = re.compile(r"(?<![\w.])([a-z_][a-z0-9_]*)\(")

    branches = {
        "severe": ({"verdict": {"value": "pause"}}, 0.75),
        "risky": ({"verdict": {"value": "proceed"}}, 0.55),
        "margin_near_edge": (
            {"verdict": {"value": "guide"}, "margin": "tight", "decision": {"action": "proceed"}},
            0.10,
        ),
        "advisory_verdict": (
            {"verdict": {"value": "guide"}, "decision": {"action": "proceed"}},
            0.10,
        ),
    }

    seen, broken = {}, []
    for label, (payload, risk) in branches.items():
        hint = _recovery_hint(payload, None, risk)
        assert hint, f"{label}: expected a hint, got {hint!r}"
        seen[label] = hint
        broken += [
            f"{label}: {name} (in {hint!r})"
            for name in call_token.findall(hint)
            if name not in callable_names
        ]

    assert not broken, (
        "recovery_hint names tools an agent cannot call:\n  " + "\n  ".join(broken)
    )
    # The branches must stay distinguishable, or this guard would pass by
    # checking one string four times.
    assert len(set(seen.values())) == len(seen), seen


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
        "headline": "Provisional: proceed; behavioral evidence is still forming.",
        "action": "proceed",
        "sub_action": "approve",
        "verdict": "safe",
        "reason": "Low risk (0.05)",
        "risk_score": 0.05,
        "verdict_confidence": "provisional",
        "evidence_basis": "ode_fallback",
    }
    assert "cold-start prior" in env["verdict_caveat"]
    # A compact check-in omits raw_governance and carries no evidence object,
    # so the caveat states its basis inline and points nowhere; it used to
    # name raw_governance.metrics.verdict.evidence, a path no mode returns.
    assert "Evidence basis: ode_fallback." in env["verdict_caveat"]
    assert "raw_governance" not in env["verdict_caveat"]
    assert " See " not in env["verdict_caveat"]
    assert env["state_summary"]["verdict_provisional"] is True
    assert "legacy_diagnostics" not in env
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
    # These bounded modes omit raw_governance, so the caveat names no path in
    # it (re-calling a check-in to follow one would write another check-in).
    assert "raw_governance" not in env
    assert "Evidence basis: ode_fallback." in env["verdict_caveat"]
    assert "raw_governance" not in env["verdict_caveat"]
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
    """Glossary coaching lifted out of the verdict speaks the friendly register.

    An uninitialized verdict's next_action names process_agent_update (the
    canonical tool); at the friendly surface that read as a register mismatch
    (dogfood 2026-08-20). state_summary runs through the same alias
    translation as next_action; scalar fields pass through untouched. With no
    top-level next_action in the payload, the verdict's step is lifted to the
    envelope's next_action (the lifecycle contract requires one) and said
    once, so state_summary no longer repeats it.
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
    assert "sync_state" in env["next_action"]
    assert "process_agent_update" not in env["next_action"]
    assert "next_action" not in env["state_summary"]
    assert env["state_summary"]["E"] == 0.5
    # the source payload keeps its canonical wording
    assert "process_agent_update" in payload["verdict"]["next_action"]


def test_metrics_read_states_one_next_step_when_the_payload_has_its_own():
    """The payload's guidance outranks a verdict step that says the same
    thing in other words; state_summary does not keep the second copy, and
    the guidance is not repeated as action_summary.reason (standard tier,
    uninitialized)."""
    payload = {
        "success": True,
        "verdict": {
            "verdict": "uninitialized",
            "meaning": "Agent has no recorded state yet.",
            "next_action": "Submit one process_agent_update to activate governance.",
        },
        "guidance": "Submit one check-in to activate governance.",
        "status": "uninitialized",
    }
    env = build_experience_envelope("check_working_state", "get_governance_metrics", payload)
    assert env["next_action"] == "Submit one check-in to activate governance."
    assert "next_action" not in env["state_summary"]
    assert "reason" not in env["action_summary"]


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


def test_routine_sync_state_omits_duplicate_raw_payload_and_stays_bounded():
    source = {
        "success": True,
        "status": "healthy",
        "health_status": "healthy",
        "decision": {
            "action": "proceed",
            "sub_action": "approve",
            "reason": "Low risk (25.3%) - healthy operating range",
            "margin": "settling",
            "nearest_edge": None,
        },
        "metrics": {
            "E": 0.72,
            "I": 0.79,
            "S": 0.21,
            "V": -0.02,
            "coherence": 0.49,
            "coherence_source": "legacy_tanh_v",
            "coherence_role": "ode_control_feedback",
            "risk_score": 0.25,
            "risk_score_latest": 0.25,
            "phi": 0.17,
            "verdict": {
                "value": "safe",
                "meaning": "Behavioral assessment: low risk. Provisional.",
                "evidence": {
                    "grade": "provisional",
                    "basis": "ode_fallback",
                },
            },
            "health_status": "healthy",
        },
        "policy_evaluation": {
            "action": "proceed",
            "sub_action": "approve",
            "guidance": "45% margin to PAUSE threshold",
            "inputs": {"verdict": "safe", "risk_score": 0.25},
        },
    }

    formatted = format_response(deepcopy(source), {"response_mode": "auto"})
    assert formatted["_mode"] == "compact"

    env = build_experience_envelope(
        "sync_state",
        "process_agent_update",
        formatted,
        {"response_mode": "auto"},
    )

    assert "raw_governance" not in env
    assert env["raw_governance_available"] is True
    assert "response_options" not in env
    assert "legacy_diagnostics" not in env
    assert "_response_size" not in env
    assert len(json.dumps(env, ensure_ascii=False).encode("utf-8")) <= 2_500


def test_actionable_auto_sync_state_is_bounded_but_self_sufficient():
    source = {
        "success": True,
        "status": "critical",
        "health_status": "critical",
        "decision": {
            "action": "pause",
            "sub_action": "reject",
            "reason": "Risk crossed the pause threshold.",
            "margin": "critical",
            "nearest_edge": "risk_pause",
            "require_human": True,
        },
        "metrics": {
            "coherence": 0.31,
            "coherence_source": "legacy_tanh_v",
            "coherence_role": "ode_control_feedback",
            "risk_score": 0.82,
            "risk_score_latest": 0.88,
            "verdict": "high-risk",
            "health_status": "critical",
        },
        "recovery_hint": "Call self_recovery(action='review', reflection='...').",
    }

    formatted = format_response(deepcopy(source), {"response_mode": "auto"})
    assert formatted["_mode"] == "mirror"

    env = build_experience_envelope(
        "sync_state",
        "process_agent_update",
        formatted,
        {"response_mode": "auto"},
    )

    assert "raw_governance" not in env
    assert env["action_summary"]["action"] == "pause"
    assert env["state_summary"]["nearest_edge"] == "risk_pause"
    assert "stop this line of work" in env["next_action"]
    assert "request_review" in env["recovery_hint"]
    assert len(json.dumps(env, ensure_ascii=False).encode("utf-8")) <= 4_000


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
    assert "raw_governance" not in env
    assert env["raw_governance_available"] is True
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


def test_search_lean_projection_keeps_attribution():
    """A lean digest still says who wrote each finding: the write-time label
    and the identity it was filed under."""
    payload = {
        "success": True,
        "count": 1,
        "discoveries": [
            {
                "by": "backup-investigator",
                "summary": "the disk was full",
                "id": "d1",
                "_agent_id": "5b0c1f7e-0000-4000-8000-000000000001",
            }
        ],
    }

    env = build_experience_envelope(
        "search_shared_memory", "knowledge", payload, {"response_mode": "lean"}
    )

    first = env["memory_suggestions"][0]
    assert first["by"] == "backup-investigator"
    assert first["agent_id"] == "5b0c1f7e-0000-4000-8000-000000000001"
    assert "_agent_id" not in first


def test_search_lean_projection_bounds_historical_summaries_and_total_wire():
    payload = {
        "success": True,
        "count": 5,
        "discoveries": [
            {
                "id": f"d{i}",
                "summary": ("qualification-preserving context 🌱 " * 180)
                + "under cold-start conditions only",
                "type": "experiment",
                "status": "open",
                "tags": [f"tag-{n}" for n in range(9)],
                "similarity": 0.9 - (i * 0.01),
                "rrf_score": 0.7,
                "fusion_score": 0.8,
                "details_preview": "bounded detail preview",
                "has_details": True,
            }
            for i in range(5)
        ],
        "discovery_retrieval_options": {
            "current_tier": "digest",
            "open_one": "knowledge(action='details', discovery_id='...')",
            "all_inline": "include_details=true",
        },
    }

    env = build_experience_envelope(
        "search_shared_memory",
        "knowledge",
        payload,
        {"response_mode": "lean"},
    )

    suggestions = env["memory_suggestions"]
    assert len(suggestions) <= 3
    assert env["state_summary"]["result_set_truncated"] is True
    first = suggestions[0]
    assert first["preview_truncated"] is True
    assert len(first["summary"]) <= 240
    assert first["tags"] == [f"tag-{n}" for n in range(5)]
    assert first["tags_truncated"] is True
    assert first["relevance"] == pytest.approx(0.8)
    assert first["relevance_basis"] == "fusion"
    assert "rrf_score" not in first
    assert "fusion_score" not in first
    assert "similarity" not in first
    assert "_response_size" not in env
    assert len(json.dumps(env, ensure_ascii=False).encode("utf-8")) <= 3_000


def test_search_projection_budget_compaction_keeps_attribution():
    payload = {
        "success": True,
        "results": [
            {
                "id": "d1",
                "by": "backup-investigator",
                "_agent_id": "5b0c1f7e-0000-4000-8000-000000000001",
                "title": "legacy-title-" + "x" * 2_600,
                "summary": "short summary",
            }
        ],
        "total_count": 1,
    }

    env = build_experience_envelope("search_shared_memory", "knowledge", payload)

    assert len(json.dumps(env, ensure_ascii=False).encode("utf-8")) <= 3_000
    first = env["memory_suggestions"][0]
    assert "title" not in first
    assert first["by"] == "backup-investigator"
    assert first["agent_id"] == "5b0c1f7e-0000-4000-8000-000000000001"


def test_search_lean_projection_never_truncates_agent_id():
    """`agent_id` is the key a reader passes back as `agent_id_filter`; a
    legacy non-UUID writer id longer than the label bound must arrive whole,
    in the lean digest and through budget compaction alike."""
    legacy_id = "legacy-writer-" + "k" * 90
    lean_payload = {
        "success": True,
        "discoveries": [{"id": "d1", "summary": "s", "_agent_id": legacy_id}],
    }
    compact_payload = {
        "success": True,
        "results": [
            {
                "id": "d1",
                "agent_id": legacy_id,
                "title": "legacy-title-" + "x" * 2_600,
                "summary": "short summary",
            }
        ],
        "total_count": 1,
    }

    lean = build_experience_envelope(
        "search_shared_memory", "knowledge", lean_payload, {"response_mode": "lean"}
    )
    compacted = build_experience_envelope(
        "search_shared_memory", "knowledge", compact_payload
    )

    assert lean["memory_suggestions"][0]["agent_id"] == legacy_id
    assert compacted["projection_truncated"] is True
    assert "title" not in compacted["memory_suggestions"][0]
    assert compacted["memory_suggestions"][0]["agent_id"] == legacy_id


def test_search_lean_projection_flags_a_truncated_by_label():
    """A write-time label longer than the bound is cut visibly (ellipsis plus
    `by_truncated`), never returned as a prefix posing as the whole label,
    and the flag survives budget compaction."""
    long_label = "investigator-" + "q" * 200
    agent_id = "5b0c1f7e-0000-4000-8000-000000000001"
    lean_payload = {
        "success": True,
        "discoveries": [
            {"id": "d1", "summary": "s", "by": long_label, "_agent_id": agent_id}
        ],
    }
    compact_payload = {
        "success": True,
        "results": [
            {
                "id": "d1",
                "by": long_label,
                "_agent_id": agent_id,
                "title": "legacy-title-" + "x" * 2_600,
                "summary": "short summary",
            }
        ],
        "total_count": 1,
    }

    lean = build_experience_envelope(
        "search_shared_memory", "knowledge", lean_payload, {"response_mode": "lean"}
    )["memory_suggestions"][0]
    compacted = build_experience_envelope(
        "search_shared_memory", "knowledge", compact_payload
    )["memory_suggestions"][0]

    for first in (lean, compacted):
        assert first["by_truncated"] is True
        assert first["by"].endswith("…")
        assert len(first["by"]) == 64
        assert long_label.startswith(first["by"][:-1])
        assert first["agent_id"] == agent_id

    short = build_experience_envelope(
        "search_shared_memory",
        "knowledge",
        {"success": True, "discoveries": [{"id": "d1", "by": "x" * 64}]},
        {"response_mode": "lean"},
    )["memory_suggestions"][0]
    assert short["by"] == "x" * 64
    assert "by_truncated" not in short


@pytest.mark.parametrize("id_len", [120, 400])
def test_search_lean_projection_worst_case_attribution_holds_wire_budget(id_len):
    """Worst-case attribution on every digest: maximal labels, long legacy
    writer ids, and summaries/tags at their own bounds. The whole envelope
    stays inside 3,000 bytes; a surviving identity is exact, never a prefix,
    and when any digest's identity is withheld the envelope says so."""
    agent_ids = [f"legacy-writer-{i}-" + "z" * id_len for i in range(5)]
    payload = {
        "success": True,
        "count": 5,
        "discoveries": [
            {
                "id": f"d{i}",
                "by": "label-🌱-" + "w" * 5_000,
                "_agent_id": agent_ids[i],
                "summary": "qualification-preserving context 🌱 " * 180,
                "type": "experiment",
                "status": "open",
                "tags": [f"tag-{n}" for n in range(9)],
                "fusion_score": 0.8,
            }
            for i in range(5)
        ],
    }

    env = build_experience_envelope(
        "search_shared_memory", "knowledge", payload, {"response_mode": "lean"}
    )

    assert len(json.dumps(env, ensure_ascii=False).encode("utf-8")) <= 3_000
    suggestions = env["memory_suggestions"]
    assert suggestions
    for i, digest in enumerate(suggestions):
        if "agent_id" in digest:
            assert digest["agent_id"] == agent_ids[i]
    withheld = any("agent_id" not in digest for digest in suggestions)
    assert (env.get("digest_attribution_omitted") is True) == withheld
    if id_len == 400:
        assert withheld  # the omission path must actually be exercised
    labelled = [digest for digest in suggestions if "by" in digest]
    assert labelled  # the label assertions below must actually run
    for digest in labelled:
        assert digest["by_truncated"] is True
        assert len(digest["by"]) == 64


_ATTRIBUTION_KEYS = ("by", "by_truncated", "agent_id")


def _results(n_results, attributed):
    rows = []
    for i in range(n_results):
        row = {"id": f"d{i + 1}", "title": f"Title {i + 1}", "type": "insight",
               "status": "open", "tags": ["a", "b"], "fusion_score": 0.5 - i / 10,
               "summary": ("finding " + str(i + 1) + " ") * 25}
        if attributed:
            row["by"] = "backup-investigator-session-label"
            row["_agent_id"] = f"5b0c1f7e-1a2b-4c3d-8e9f-00000000000{i + 1}"
        rows.append(row)
    return rows


@pytest.mark.parametrize("n_results", [1, 3])
def test_attribution_never_costs_a_result_or_its_fields(n_results):
    """Review on #2386: attribution never costs a result or its fields.
    Swept over the envelope's free space: every digest an attribution-free
    payload keeps, the attributed payload keeps with the same
    non-attribution fields; attribution returns as label+identity, identity
    alone, or not at all, and an identity that survives is exact.

    What attribution may cost is only what is optional (the coaching the
    budget steps already drop, and a truncated digest's `expand_with`, which
    repeats `raw_governance_hint`), only instead of being withheld, and only
    as much as it takes: a piece that gave way would not fit back."""
    saw_labels, saw_ids_only, saw_none, saw_coaching_yield = False, False, False, False
    for n in range(0, 2600, 2):
        extra = {"total_count": n_results, "confidence_note": "c" * n, "success": True}
        bare = build_experience_envelope(
            "search_shared_memory", "knowledge", {**extra, "results": _results(n_results, False)})
        env = build_experience_envelope(
            "search_shared_memory", "knowledge", {**extra, "results": _results(n_results, True)})
        bare_d = bare.get("memory_suggestions") or []
        env_d = env.get("memory_suggestions") or []
        bare_size = len(json.dumps(bare, ensure_ascii=False).encode("utf-8"))
        env_size = len(json.dumps(env, ensure_ascii=False).encode("utf-8"))
        # Attribution adds no overshoot of its own. Nor do the truncation
        # markers any more: the only envelopes over budget are those whose
        # note alone does not fit, with every digest already dropped.
        if bare_size <= 3_000:
            assert env_size <= 3_000, (n, env_size)
        if bare_d or env_d:
            assert max(bare_size, env_size) <= 3_000, (n, bare_size, env_size)
        # Every attributed row has a label and an identity, so anything
        # missing from a digest was withheld (a dropped label included).
        withheld = any("agent_id" not in d or "by" not in d for d in env_d)
        if withheld and bare_size <= 3_000:
            # Optional coaching or the repeated pointer always gives the
            # marker its room, so a withheld attribution is never unmarked.
            assert env.get("digest_attribution_omitted") is True, n
        if not withheld:
            assert "digest_attribution_omitted" not in env, n
        assert len(env_d) >= len(bare_d), n
        # Envelope-level parity too: attribution alone never triggers the
        # truncation path (its flag, dropped digests, the digest counts).
        assert env.get("projection_truncated") == bare.get("projection_truncated"), n
        assert env.get("state_summary") == bare.get("state_summary"), n
        # Optional coaching and the repeated full-mode pointer may yield to
        # attribution, but are only ever dropped or trimmed, only when they
        # would not fit back, and the full-mode route stays named.
        for key in ("response_options", "discovery_retrieval_options", "expand_with"):
            got, want = env.get(key), bare.get(key)
            if got != want:
                assert got is None or got.items() <= want.items(), (n, key)
                put_back = json.dumps({**env, key: want}, ensure_ascii=False)
                assert len(put_back.encode("utf-8")) > 3_000, (n, key)
                assert env["raw_governance_hint"] == bare["raw_governance_hint"], n
                saw_coaching_yield = True
        for got, want in zip(env_d, bare_d):
            assert {k: v for k, v in got.items() if k not in _ATTRIBUTION_KEYS} == want, n
        for i, got in enumerate(env_d):
            if "agent_id" in got:
                assert got["agent_id"] == f"5b0c1f7e-1a2b-4c3d-8e9f-00000000000{i + 1}"
        for got in env_d:
            if "by" in got:
                saw_labels = True
            elif "agent_id" in got:
                saw_ids_only = True
            else:
                saw_none = True
    assert saw_labels and saw_ids_only and saw_none and saw_coaching_yield


def test_a_short_identity_is_restored_where_the_marker_cannot_fit():
    """Review on #2386 (round 7): with less room than the withheld-marker
    needs (~36 bytes), a short legacy identity (16 bytes) must still be
    restored rather than dropped unmarked. Sweeps the free space across that
    window; wherever the identity alone fits, it is present."""
    base = {"success": True, "results": [{"id": "d1", "summary": "s", "agent_id": "a1"}],
            "total_count": 1}
    exercised = False
    # The free-space dial is confidence_note, which the envelope now carries
    # once (top level), so the window sits at twice the old note length.
    for n in range(2150, 2400):
        payload = {**base, "confidence_note": "c" * n}
        bare = build_experience_envelope(
            "search_shared_memory", "knowledge",
            {**payload, "results": [{"id": "d1", "summary": "s"}]})
        env = build_experience_envelope("search_shared_memory", "knowledge", payload)
        bare_size = len(json.dumps(bare, ensure_ascii=False).encode("utf-8"))
        digest = (env.get("memory_suggestions") or [None])[0]
        if digest is None or bare_size > 3_000:
            continue
        if bare_size + len(', "agent_id": "a1"') <= 3_000:
            assert digest.get("agent_id") == "a1", (n, bare_size)
            if bare_size + 36 > 3_000:
                exercised = True
        elif "agent_id" not in digest and bare_size + 36 <= 3_000:
            assert env.get("digest_attribution_omitted") is True, n
    assert exercised  # the marker-cannot-fit window was actually swept


def test_default_lean_search_carries_attribution_end_to_end():
    """Review on #2386 (round 8): the default search_shared_memory path runs
    the canonical result through _lean_search_payload before the envelope is
    built, and that projection used to drop `by` and `_agent_id`, so no
    attribution ever reached the default digest. Exercise that path."""
    from src.mcp_handlers.knowledge.handlers import _lean_search_payload

    writer = "5b0c1f7e-0000-4000-8000-00000000abcd"
    canonical = {
        "success": True,
        "discoveries": [{"id": "d1", "by": "backup-investigator", "_agent_id": writer,
                         "summary": "what was found", "type": "insight", "status": "open"}],
        "similarity_scores": {"d1": 0.8},
    }
    lean = _lean_search_payload(canonical)
    env = build_experience_envelope(
        "search_shared_memory", "knowledge", lean, {"response_mode": "lean"})
    first = env["memory_suggestions"][0]
    assert first["discovery_id"] == "d1"
    assert first["by"] == "backup-investigator"
    assert first["agent_id"] == writer


def test_search_projection_budget_omits_pathological_identity_explicitly():
    """An identifier that cannot fit is withheld with a marker, not cut to a
    prefix; the handle that opens the record remains."""
    payload = {
        "success": True,
        "results": [
            {
                "id": "d1",
                "by": "backup-investigator",
                "agent_id": "legacy-" + "k" * 4_000,
                "summary": "short summary",
            }
        ],
        "total_count": 1,
    }

    env = build_experience_envelope("search_shared_memory", "knowledge", payload)

    assert len(json.dumps(env, ensure_ascii=False).encode("utf-8")) <= 3_000
    first = env["memory_suggestions"][0]
    assert first["discovery_id"] == "d1"
    assert env["digest_attribution_omitted"] is True
    assert "agent_id" not in first and "by" not in first


def test_search_projection_budget_drops_oversized_single_result():
    payload = {
        "success": True,
        "results": [
            {
                "id": "d1",
                "title": "legacy-title-" + "x" * 10_000,
                "summary": "short summary",
                "tags": ["tag-" + "x" * 10_000],
            }
        ],
        "total_count": 1,
    }

    env = build_experience_envelope("search_shared_memory", "knowledge", payload)

    assert env["projection_truncated"] is True
    assert len(json.dumps(env, ensure_ascii=False).encode("utf-8")) <= 3_000


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
    # Said once: the note is the top-level copy every alias gets; a second
    # copy in state_summary was paid out of the lean digest budget.
    assert "confidence_note" not in env["state_summary"]
    assert json.dumps(env).count("Semantic-only matches") == 1
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


def test_store_finding_envelope_keeps_the_callers_summary_verbatim():
    """Tool-name translation is for hints, not for the caller's own words."""
    summary = "Plugin auto-onboard labels a session; call onboard() to see it"
    payload = {
        "success": True,
        "message": "Discovery stored for agent 'agent-1'",
        "discovery_id": "d-words",
        "discovery": {"id": "d-words", "type": "bug_found", "status": "open", "summary": summary},
        "_resolve_when_done": (
            "When this is addressed, close the loop: "
            "knowledge(action='update', discovery_id='d-words', status='resolved')"
        ),
    }

    env = build_experience_envelope("store_finding", "knowledge", payload, {"summary": summary})

    assert env["state_summary"]["summary"] == summary
    assert "update_finding(" in env["next_action"]


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
    assert "raw_governance" not in env
    assert "discovery_id='d-new'" in env["raw_governance_hint"]


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
    assert "raw_governance" not in env
    assert "discovery_id='d-existing'" in env["raw_governance_hint"]


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
    # next_action names the lifted id rather than repeating it.
    assert env["prediction_id"] == "abc-123"
    assert "pass this prediction_id to record_result" in env["next_action"]


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
    # next_action names the lifted id rather than repeating it.
    assert env["prediction_id"] == "abc-123"
    assert "pass this prediction_id to record_result" in env["next_action"]
    assert "request_review" in env["next_action"]


# ─── #2123: the README quickstart reads prediction_id as a top-level key ────
# The envelope rebuilds the response from scratch, so naming the id only in
# next_action prose made `result.get("prediction_id")` return None. An unbound
# record_result then falls back to prev_confidence_fallback — a member of
# SCRAPED_PREDICTION_SOURCES — so the row is stamped calibration_excluded and
# never reaches the tactical lane, and decision_action for test_passed /
# test_failed is hardcoded "proceed" instead of read off the registered
# prediction. Both calls return success:true.


def test_sync_state_envelope_exposes_prediction_id_top_level():
    payload = {
        "success": True,
        "decision": {"action": "proceed"},
        "prediction_id": "abc-123",
    }
    env = build_experience_envelope("sync_state", "process_agent_update", payload)
    assert env.get("prediction_id") == "abc-123"
    # The prose keeps saying what the id is for; the key is what code reads.
    # next_action names the lifted id rather than repeating it.
    assert env["prediction_id"] == "abc-123"
    assert "pass this prediction_id to record_result" in env["next_action"]


def test_sync_state_envelope_lifts_prediction_id_from_nested_payload():
    """A caller handing us an already-enveloped response still gets the key."""
    payload = {
        "success": True,
        "raw_governance": {
            "success": True,
            "decision": {"action": "proceed"},
            "prediction_id": "nested-456",
        },
    }
    env = build_experience_envelope("sync_state", "process_agent_update", payload)
    assert env.get("prediction_id") == "nested-456"


def test_sync_state_envelope_omits_prediction_id_when_none_was_minted():
    """Absent beats present-and-null: record_result must not echo a None."""
    payload = {"success": True, "decision": {"action": "proceed"}}
    env = build_experience_envelope("sync_state", "process_agent_update", payload)
    assert "prediction_id" not in env


def test_quickstart_loop_can_thread_prediction_id_without_raw_governance():
    """Pins the README Quickstart contract end to end.

    README.md reads `result.get("prediction_id")` straight off the sync_state
    response. This asserts the documented expression yields the real id
    without the caller reaching into raw_governance or regexing next_action.
    """
    payload = {
        "success": True,
        "decision": {"action": "proceed"},
        "metrics": {"risk_score": 0.2},
        "prediction_id": "quickstart-789",
    }
    result = build_experience_envelope("sync_state", "process_agent_update", payload)

    prediction_id = result.get("prediction_id")  # the README's own expression

    assert prediction_id == "quickstart-789"
    # Without this, record_result falls back to prev_confidence_fallback, which
    # is in SCRAPED_PREDICTION_SOURCES — the row is then stamped
    # calibration_excluded and dropped from the tactical lane.
    assert prediction_id is not None


def test_envelope_prefers_canonical_prediction_id_over_stale_outer_copy():
    """Nested raw_governance is the canonical payload, so it wins a conflict.

    Preferring the outer copy would hand back an older id; record_result would
    then consume and grade whatever prediction that id still points at.
    """
    payload = {
        "success": True,
        "prediction_id": "stale-outer",
        "raw_governance": {
            "success": True,
            "decision": {"action": "proceed"},
            "prediction_id": "fresh-canonical",
        },
    }
    env = build_experience_envelope("sync_state", "process_agent_update", payload)
    assert env["prediction_id"] == "fresh-canonical"
    # next_action names the lifted id rather than repeating it.
    assert env["prediction_id"] == "fresh-canonical"
    assert "pass this prediction_id to record_result" in env["next_action"]


def test_prediction_id_is_not_lifted_onto_unrelated_tools():
    """Only the check-in mints one; echoing a consumed id back would misbind."""
    payload = {"success": True, "prediction_id": "already-consumed"}
    env = build_experience_envelope("record_result", "outcome_event", payload)
    assert "prediction_id" not in env


def test_record_result_envelope_discloses_a_fallback_binding():
    """A calibration_excluded outcome must not read as a counted one."""
    payload = {
        "success": True,
        "outcome_id": "o-1",
        "outcome_type": "test_passed",
        "raw_governance": {
            "prediction_binding": "prev_confidence_fallback",
            "prediction_source": "prev_confidence_fallback",
            "calibration_excluded": True,
        },
    }
    env = build_experience_envelope("record_result", "outcome_event", payload)
    assert env["state_summary"]["calibration_excluded"] is True
    assert env["state_summary"]["prediction_binding"] == "prev_confidence_fallback"
    assert "calibration_excluded" in env["next_action"]
    assert "prediction_id" in env["next_action"]


def test_record_result_envelope_reports_a_registry_binding_plainly():
    payload = {
        "success": True,
        "outcome_id": "o-2",
        "outcome_type": "test_passed",
        "raw_governance": {
            "prediction_binding": "registry",
            "prediction_source": "registry",
            "calibration_excluded": False,
        },
    }
    env = build_experience_envelope("record_result", "outcome_event", payload)
    assert env["state_summary"]["prediction_binding"] == "registry"
    assert env["state_summary"]["calibration_excluded"] is False
    assert env["next_action"].startswith("Outcome recorded - continue")


@pytest.mark.parametrize("risk,band", [(0.44, "low"), (0.46, "elevated"), (0.71, "high")])
def test_risk_summary_uses_policy_bands_not_recovery_ceiling(risk, band):
    envelope = build_experience_envelope(
        "check_working_state", "get_governance_metrics", {"risk_score": risk}
    )
    assert envelope["risk_summary"] == f"risk {band} ({risk:.2f})"


# --- check_working_state verbosity tiers ---------------------------------------
#
# The handler has always honoured minimal / standard / full, but only boolean
# `lite` was advertised, so an agent wanting more than the minimum reached `full`
# (~15 KB) and was then told to go back to lite=true (external agent, 2026-09-24).


def _served_tier(env):
    """The tier a check_working_state envelope reports.

    A non-default tier reports itself in response_options.current. The
    default envelope teaches the tier ladder once, in raw_governance_hint, as
    a bounded sync_state does, so it carries no response_options; it is the
    one tier with raw_governance omitted.
    """
    if "response_options" in env:
        assert "raw_governance" in env
        return env["response_options"]["current"]
    assert "raw_governance" not in env
    hint = env.get("raw_governance_hint")
    if hint is None:
        # Only before the first check-in: no tier has basin or mode yet, and
        # next_action names the step that changes that.
        assert env["action_summary"]["verdict"] == "uninitialized", env["action_summary"]
    else:
        assert "verbosity='standard'" in hint
    return "minimal"


@pytest.mark.parametrize(
    "arguments, current",
    [
        ({}, "minimal"),
        ({"lite": True}, "minimal"),
        ({"lite": False}, "full"),
        ({"verbosity": "standard"}, "standard"),
        ({"verbosity": "full", "lite": True}, "full"),
        ({"verbosity": "minimal", "lite": False}, "minimal"),
    ],
)
def test_metrics_response_options_report_the_tier_actually_served(arguments, current):
    env = build_experience_envelope(
        "check_working_state", "get_governance_metrics", {"success": True}, arguments
    )
    assert _served_tier(env) == current
    if current == "minimal":
        # One ladder per response: the hint, not a second copy.
        assert "response_options" not in env
    else:
        assert "verbosity='standard'" in env["response_options"]["interpreted_state"]
        assert "raw_governance_hint" not in env


def test_oversized_full_metrics_point_at_the_standard_tier():
    payload = {"success": True, "padding": "x" * 6_000}
    env = build_experience_envelope(
        "check_working_state", "get_governance_metrics", payload, {"lite": False}
    )
    assert "verbosity='standard'" in env["_response_size"]["reduce_with"]


@pytest.mark.parametrize(
    "arguments",
    [
        {"lite": None},
        {"lite": None, "verbosity": None},
        {"lite": "false"},
        {"lite": "on"},
        {"lite": "banana"},
        {"verbosity": "bogus", "lite": False},
        {"verbosity": "standard", "lite": None},
        # include_state adds no state on any tier; it used to force the raw
        # payload onto the minimal tier (a 3.3 KB read became 7.1 KB).
        {"include_state": True},
        {"include_state": "true", "lite": "true"},
        {"include_state": True, "verbosity": "standard"},
    ],
)
def test_metrics_tier_reported_matches_the_tier_the_handler_builds(arguments):
    """Review of #2430: lite=null made the handler build full while the
    envelope reported minimal. Both now resolve through one function; this
    pins the envelope to the handler on raw AND schema-validated arguments,
    and pins include_state out of the tier decision."""
    from src.mcp_handlers.schemas.core import GetGovernanceMetricsParams
    from src.mcp_handlers.support.param_normalization import resolve_metrics_verbosity

    raw_tier = resolve_metrics_verbosity(arguments)
    try:
        validated = GetGovernanceMetricsParams.model_validate(arguments).model_dump()
    except Exception:
        # Refused on validated routes, but REST get_governance_metrics skips
        # validation and hands these raw arguments to the handler, so the
        # raw-tier assertions below still apply.
        validated = None
    if validated is not None:
        assert resolve_metrics_verbosity(validated) == raw_tier
    env = build_experience_envelope(
        "check_working_state", "get_governance_metrics", {"success": True}, arguments
    )
    assert _served_tier(env) == raw_tier
    assert ("raw_governance" in env) == (raw_tier != "minimal")


def _tier_built(data):
    """Which branch of get_governance_metrics_data produced ``data``."""
    if "_debug_lite_received" in data:
        return "full"
    if str(data.get("_note", "")).startswith("Use verbosity='full'"):
        return "standard"
    return "minimal"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"lite": None},
        {"lite": True},
        {"lite": False},
        {"lite": "false"},
        {"lite": "banana"},
        {"verbosity": "standard"},
        {"verbosity": "standard", "lite": None},
        {"verbosity": "full", "lite": True},
        {"include_state": True},
        {"include_state": "true", "lite": "true"},
    ],
)
async def test_envelope_reports_the_tier_the_real_handler_built(arguments):
    """Runs the handler itself, so reverting runtime_queries' tier logic fails
    here even though the envelope and resolver would still agree. The envelope
    is built from that same handler output."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    from src.governance_monitor import UNITARESMonitor
    from src.services.runtime_queries import get_governance_metrics_data

    monitor = UNITARESMonitor("test-tier-parity", load_state=False)
    server = SimpleNamespace(get_or_create_monitor=lambda aid: monitor, agent_metadata={})
    with patch(
        "src.agent_monitor_state.hydrate_from_db_if_fresh",
        new=AsyncMock(return_value=False),
    ):
        data = await get_governance_metrics_data(
            "test-tier-parity", dict(arguments), server=server
        )
    env = build_experience_envelope(
        "check_working_state",
        "get_governance_metrics",
        {"success": True, **data},
        arguments,
    )
    assert _served_tier(env) == _tier_built(data)


def test_metrics_verbosity_is_matched_exactly_like_the_handler_always_did():
    """No case folding. Only unvalidated REST get_governance_metrics can pass
    "Standard" here, and it keeps falling through to lite; validated routes
    refuse it (see the next test)."""
    from src.mcp_handlers.support.param_normalization import resolve_metrics_verbosity

    assert resolve_metrics_verbosity({"verbosity": "Standard"}) == "minimal"
    assert resolve_metrics_verbosity({"verbosity": " full", "lite": False}) == "full"
    assert resolve_metrics_verbosity({"verbosity": "Full", "lite": True}) == "minimal"


def test_validated_routes_refuse_an_off_list_verbosity():
    """1.15.0 note: /mcp/ and REST check_working_state validate, so a value they
    once ignored is now a validation error, as 1.8.0 did for cirs_protocol."""
    import pydantic

    from src.mcp_handlers.schemas.core import GetGovernanceMetricsParams

    for value in ("Standard", "bogus"):
        with pytest.raises(pydantic.ValidationError):
            GetGovernanceMetricsParams.model_validate({"verbosity": value})
