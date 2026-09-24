"""No agent-facing text may tell an agent to pause unless the decision paused it.

2026-09-24. The behavioral verdict and the policy decision are separate: the
cold-start guard (#1591, #1819) and gap suppression turn a pause into
proceed/guide while the verdict stays "high-risk". Three surfaces kept the
verdict's voice anyway:

- mirror mode (the `auto` default for an actionable check-in) drops `decision`,
  so the envelope read verdict "high-risk", aliased it to pause, and told a
  guided agent "Paused - stop this line of work";
- `_recovery_hint` treated risk >= 0.7 as severe whatever the decision, so it
  said "pause and call self_recovery", and reviewed recovery then refused the
  agent (it gates on risk < 0.65) after recording its reflection;
- the glossary's high-risk next_action says "Pause, reflect, ...", and a
  metrics read wraps that verdict with no decision at all.

A real pause keeps every directive it had.
"""
from copy import deepcopy

from src.governance_glossary import explain_verdict
from src.mcp_handlers.middleware import envelope_step as ES
from src.mcp_handlers.middleware.envelope_step import build_experience_envelope
from src.mcp_handlers.response_formatter import format_response
from src.mcp_handlers.updates.phases import _rewrap_behavioral_verdict
from src.services.runtime_queries import _last_decision_action
from src.tool_modes import build_server_instructions


def _guided_cold_start_check_in() -> dict:
    """The canonical sync_state payload after the cold-start guard applied."""
    return {
        "success": True,
        "agent_id": "a",
        "status": "high-risk",
        "health_status": "high-risk",
        "decision": {
            "action": "proceed",
            "sub_action": "guide",
            "reason": "Cold start: not enough history to assess this agent yet.",
            "original_action": "pause",
            "original_sub_action": "risk_pause",
            "cold_start_epistemic_deferred": True,
            "margin": "comfortable",
        },
        "metrics": {
            "coherence": 0.5,
            "risk_score": 0.79,
            "verdict": "high-risk",
            "health_status": "high-risk",
            "primary_eisv_source": "ode_fallback",
        },
        "policy_evaluation": {
            "policy_name": "monitor_decision",
            "action": "proceed",
            "sub_action": "guide",
            "inputs": {"verdict": "high-risk", "risk_score": 0.79},
        },
        "enforcement": {"requested": False, "applied": False},
        "risk_attribution": {
            "primary_driver": "phi_cold_start",
            "discriminability": {"non_discriminative": True},
        },
    }


def _envelope(source: dict, mode: str) -> dict:
    formatted = format_response(deepcopy(source), {"response_mode": mode})
    return build_experience_envelope(
        "sync_state", "process_agent_update", formatted, {"response_mode": mode}
    )


def _agent_text(env: dict) -> str:
    parts = [
        str(env.get("next_action", "")),
        str(env.get("recovery_hint", "")),
        str(env.get("action_summary", {}).get("headline", "")),
    ]
    return " ".join(parts).lower()


# --- the glossary -------------------------------------------------------------

def test_high_risk_verdict_on_a_proceed_decision_does_not_say_pause():
    wrapped = explain_verdict("high-risk", decision_action="proceed")
    assert not wrapped["next_action"].lower().startswith("pause")
    assert "does not block" in wrapped["next_action"]
    # Honest, not soothing: a reading this high can still pause later.
    assert "can still pause a later check-in" in wrapped["next_action"]
    assert wrapped["decision_action"] == "proceed"


def test_a_stop_decision_under_a_steady_verdict_never_says_continue():
    """The converse: a void/coherence/CIRS pause can ride a 'safe' verdict."""
    for verdict in ("safe", "caution", "proceed"):
        wrapped = explain_verdict(verdict, decision_action="pause")
        assert "continue" not in wrapped["next_action"].lower(), verdict
        assert "self_recovery(action='check')" in wrapped["next_action"], verdict
        # A post-ODE escalation decides pause without actuating it, so the
        # text must not claim a hold is in force.
        assert "writes hold" not in wrapped["next_action"], verdict


def test_high_risk_verdict_on_a_pause_decision_keeps_its_directive():
    wrapped = explain_verdict("high-risk", decision_action="pause")
    assert wrapped["next_action"] == "Pause, reflect, or request dialectic review."
    assert wrapped["decision_action"] == "pause"


def test_omitting_the_decision_leaves_the_wrap_byte_identical():
    wrapped = explain_verdict("high-risk", evidence_source="ode_fallback")
    assert "decision_action" not in wrapped
    assert wrapped["next_action"] == "Pause, reflect, or request dialectic review."


# --- the sync_state envelope, every response mode ----------------------------

def test_guided_cold_start_never_tells_the_agent_to_pause():
    for mode in ("auto", "mirror", "compact", "standard"):
        env = _envelope(_guided_cold_start_check_in(), mode)
        assert env["action_summary"]["action"] == "proceed", mode
        text = _agent_text(env)
        assert "paused" not in text, (mode, text)
        assert "pause and call" not in text, (mode, text)
        assert "self_recovery" not in env.get("recovery_hint", ""), mode


def test_guided_cold_start_hint_warns_the_next_authored_check_in_can_pause():
    """The guard covers only non-authored check-ins. The agent's own next
    sync_state is scored on the same prior and can pause - the one sequence
    that still paused at cold start (2026-09-21) - so the hint must say so
    rather than "keep working"."""
    env = _envelope(_guided_cold_start_check_in(), "auto")
    hint = env["recovery_hint"]
    assert hint.startswith("Cold start")
    assert "this decision does not block" in hint
    assert "your own sync_state is scored on the same prior and can pause" in hint


def test_low_risk_cold_start_hint_does_not_threaten_a_pause():
    source = _guided_cold_start_check_in()
    source["metrics"]["risk_score"] = 0.45
    hint = _envelope(source, "auto")["recovery_hint"]
    assert hint.startswith("Cold start")
    assert "can pause" not in hint


def test_high_risk_continue_decision_does_not_route_to_self_recovery():
    """Not paused: nothing to lift, and review refuses at risk >= 0.65 after
    recording the reflection (27 refused reflections in 60 days, 2026-09-24)."""
    hint = ES._recovery_hint({"decision": {"action": "guide"}}, None, 0.55)
    assert "self_recovery(" not in hint
    assert "does not block" in hint


def test_a_real_pause_keeps_its_stop_and_recovery_directives():
    source = _guided_cold_start_check_in()
    source["decision"] = {"action": "pause", "sub_action": "risk_pause"}
    source["policy_evaluation"]["action"] = "pause"
    source["policy_evaluation"]["sub_action"] = "risk_pause"
    source["enforcement"] = {"requested": True, "applied": True}
    env = _envelope(source, "auto")
    assert env["action_summary"]["action"] == "pause"
    assert "stop this line of work" in env["next_action"]
    assert "self_recovery(action='review'" in env["recovery_hint"]


# --- _recovery_hint directly ------------------------------------------------

def test_unknown_decision_with_high_risk_is_still_severe():
    """No decision anywhere: behavior unchanged."""
    hint = ES._recovery_hint({"risk_score": 0.8}, None, 0.8)
    assert "pause and call" in hint


def test_metrics_read_shape_follows_the_verdicts_decision_action():
    proceeding = {"verdict": explain_verdict("high-risk", decision_action="proceed")}
    paused = {"verdict": explain_verdict("high-risk", decision_action="pause")}
    assert "pause and call" not in ES._recovery_hint(proceeding, None, 0.79)
    assert "pause and call" in ES._recovery_hint(paused, None, 0.79)


def test_mirror_shape_escalated_after_policy_evaluation_reports_the_pause():
    """Post-ODE dialectic enforcement escalates decision.action to pause after
    policy_evaluation was built (updates/phases.py). Mirror mode drops the
    decision; the verdict's decision_action carries the final one and must
    outrank the stale policy record."""
    source = _guided_cold_start_check_in()
    source["decision"] = {"action": "pause", "sub_action": "dialectic_condition"}
    # policy_evaluation still says proceed: it predates the escalation.
    env = _envelope(source, "mirror")
    assert env["action_summary"]["action"] == "pause"
    assert "self_recovery(action='review'" in env["recovery_hint"]


def test_applied_runtime_enforcement_reads_as_pause():
    payload = {
        "policy_evaluation": {"action": "proceed"},
        "enforcement": {"requested": True, "applied": True},
        "verdict": {"value": "high-risk"},
    }
    assert ES._decision_action(payload) == "pause"


def test_decision_outranks_policy_evaluation_and_verdict():
    payload = {
        "decision": {"action": "pause"},
        "policy_evaluation": {"action": "proceed"},
        "verdict": {"value": "high-risk", "decision_action": "proceed"},
    }
    assert ES._decision_action(payload) == "pause"


# --- the metrics read -------------------------------------------------------

class _Meta:
    def __init__(self, status="active", recent_decisions=None):
        self.status = status
        self.recent_decisions = recent_decisions


def test_last_decision_action_prefers_a_paused_lifecycle_status():
    assert _last_decision_action(_Meta("paused", ["proceed"])) == "pause"
    assert _last_decision_action(_Meta("active", ["pause", "proceed"])) == "proceed"
    # _resume_with_persistence clears the history: still proceeding.
    assert _last_decision_action(_Meta("active", [])) == "proceed"
    assert _last_decision_action(None) is None


def test_non_active_statuses_report_no_decision():
    """archived/deleted/waiting_input refuse or hold writes for their own
    reasons; a recorded proceed must not become "does not block"."""
    for status in ("archived", "deleted", "waiting_input"):
        assert _last_decision_action(_Meta(status, ["proceed"])) is None, status


def test_a_resumed_agents_stale_stop_is_not_reported_as_current():
    """Pause expiry and dialectic resolution set status=active but leave the
    last recorded decision at "pause". That stop is no longer in force; the
    agent proceeds, and the verdict wrap must not fall back to "Pause"."""
    assert _last_decision_action(_Meta("active", ["proceed", "pause"])) == "proceed"
    assert _last_decision_action(_Meta("active", ["reject"])) == "proceed"
    wrapped = explain_verdict(
        "high-risk", decision_action=_last_decision_action(_Meta("active", ["pause"]))
    )
    assert not wrapped["next_action"].startswith("Pause")


# --- the server instructions ------------------------------------------------

def test_instructions_state_how_a_pause_actually_ends():
    """The exits in the code, and the recovery cost, stated as they are:
    self_recovery (not always available), dialectic resolution, an operator
    or the automatic safety nets, and expiry. "Applied": a decided pause the breaker did not actuate holds
    nothing. Review's reflection is recorded in shared memory."""
    text = build_server_instructions("progressive")
    sentence = text[text.index("An applied pause is a hard stop"):]
    sentence = sentence[:sentence.index("expires.") + len("expires.")]
    for exit_route in ("self_recovery", "request_review", "an operator",
                       "automatic safety net", "or it expires"):
        assert exit_route in sentence, exit_route
    # agent(action='resume') has no ownership, risk or void gate and a paused
    # agent can call it on itself; it must never be advertised to agents.
    assert "agent(action='resume')" not in text
    assert "self_recovery refuses while risk stays high" in text
    assert "records your written reflection in shared memory" in text


# --- the nested behavioral verdict (response_mode='full') ---------------------

def test_post_ode_escalation_rewraps_the_nested_behavioral_verdict():
    """build_result wraps behavioral.assessment.verdict with the decision as it
    stood; a post-ODE dialectic escalation then replaces the decision. The
    nested wrap must follow the escalated one."""
    result = {
        "behavioral": {"assessment": {
            "verdict": explain_verdict("high-risk", decision_action="proceed"),
        }},
    }
    _rewrap_behavioral_verdict(result, {"action": "pause"})
    verdict = result["behavioral"]["assessment"]["verdict"]
    assert verdict["decision_action"] == "pause"
    assert verdict["next_action"] == "Pause, reflect, or request dialectic review."


def test_rewrap_tolerates_a_result_without_a_behavioral_block():
    result = {}
    _rewrap_behavioral_verdict(result, {"action": "pause"})
    assert result == {}


def test_simulate_update_escalation_rewraps_the_nested_verdict():
    """simulate_update applies the same post-ODE escalation (mcp_handlers/core.py)
    and must re-wrap the nested behavioral verdict the same way."""
    import inspect

    from src.mcp_handlers import core

    source = inspect.getsource(core.handle_simulate_update)
    escalation = source[source.index("escalated_decision is not decision"):]
    assert "_rewrap_behavioral_verdict(result, escalated_decision)" in escalation[:400]


def test_resumed_agent_with_cleared_history_is_not_told_to_pause():
    """Operator resume at risk 0.75 clears recent_decisions; the metrics read
    must not fall back to "Pause, reflect"."""
    action = _last_decision_action(_Meta("active", []))
    wrapped = explain_verdict("high-risk", decision_action=action)
    assert not wrapped["next_action"].startswith("Pause")
    hint = ES._recovery_hint({"verdict": wrapped}, None, 0.75)
    assert "pause and call" not in hint


def test_cold_start_hint_is_not_given_to_a_behavioral_reading():
    """A non-baselined behavioral verdict (check-ins 3-24) is provisional but
    not the prior; it must not be described as one."""
    payload = {
        "decision": {"action": "proceed", "sub_action": "guide"},
        "metrics": {"risk_score": 0.79, "primary_eisv_source": "behavioral"},
        "risk_attribution": {
            "primary_driver": "behavioral_assessment",
            "discriminability": {"non_discriminative": True},
        },
    }
    hint = ES._recovery_hint(payload, None, 0.79)
    assert not hint.startswith("Cold start")
    assert "the prior" not in hint
