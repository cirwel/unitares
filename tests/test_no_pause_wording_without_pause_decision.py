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
  agent (it refuses at risk 0.65 and above) after recording its reflection;
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


def test_cold_start_warning_follows_the_verdict_not_the_risk_band():
    """Task-type adjustment (exploration/introspection subtract 0.08) can put
    risk_score below 0.7 while the verdict stays high-risk; the guard still
    deferred a pause, so the warning must still appear."""
    source = _guided_cold_start_check_in()
    source["metrics"]["risk_score"] = 0.64
    hint = _envelope(source, "auto")["recovery_hint"]
    assert hint.startswith("Cold start")
    assert "can pause on this reading" in hint


def test_low_risk_cold_start_hint_does_not_threaten_a_pause():
    """A cold start that never produced a pause: caution verdict, nothing
    deferred by the guard."""
    source = _guided_cold_start_check_in()
    source["metrics"]["risk_score"] = 0.45
    source["metrics"]["verdict"] = "caution"
    source["policy_evaluation"]["inputs"] = {"verdict": "caution", "risk_score": 0.45}
    for key in ("original_action", "original_sub_action", "cold_start_epistemic_deferred"):
        source["decision"].pop(key)
    source["status"] = source["health_status"] = "caution"
    source["metrics"]["health_status"] = "caution"
    hint = _envelope(source, "auto")["recovery_hint"]
    assert hint.startswith("Cold start")
    assert "can pause" not in hint


def test_high_risk_continue_decision_is_not_told_to_pause_or_routed_past_the_gate():
    """Not paused: the hint must not say pause, and must not route the agent
    to a review that refuses at or above MAX_RISK_FOR_SELF_RECOVERY after recording
    the reflection (27 refused reflections in 60 days, 2026-09-24). Below
    the gate a guide decision gets the same conditional advice as a proceed."""
    below = ES._recovery_hint({"decision": {"action": "guide"}}, None, 0.55)
    assert "does not block" in below
    assert "pause" not in below.lower()
    assert "only if work stalls" in below
    above = ES._recovery_hint({"decision": {"action": "guide"}}, None, 0.70)
    assert "self_recovery(" not in above
    assert "does not block" in above


def test_review_gate_boundary_matches_self_recovery():
    """handle_self_recovery_review admits only risk < 0.65, so it refuses AT
    the limit; the hint must not route there at exactly the limit."""
    from src.mcp_handlers.lifecycle.self_recovery import MAX_RISK_FOR_SELF_RECOVERY

    limit = MAX_RISK_FOR_SELF_RECOVERY
    decision = {"decision": {"action": "guide"}}
    under = ES._recovery_hint(decision, None, limit - 0.01)
    at = ES._recovery_hint(decision, None, limit)
    assert "only if work stalls" in under
    assert "refuses at risk" not in under
    assert f"refuses at risk {limit:.2f} and above" in at
    assert "self_recovery(" not in at


def test_unrecognised_action_above_the_gate_is_not_routed_to_review():
    """A bare "high-risk" verdict (a read with no decision, e.g. a
    waiting_input agent) between the gate and 0.7 is not severe, and fell
    through to "otherwise self_recovery(action='review')"."""
    hint = ES._recovery_hint({"verdict": "high-risk", "risk_score": 0.68}, None, 0.68)
    assert "self_recovery(" not in hint
    assert "request_review" in hint


def test_production_guide_shape_at_refusal_risk_is_not_routed_to_self_recovery():
    """The monitor emits proceed + sub_action guide, which makes the attention
    branch fire first; above the review gate it must still not advise
    self_recovery."""
    payload = {"decision": {"action": "proceed", "sub_action": "guide"}}
    for risk in (0.67, 0.75):
        hint = ES._recovery_hint(payload, None, risk)
        assert "self_recovery(" not in hint, risk
        assert "does not block" in hint, risk
    # Below the review gate the existing advisory wording stays.
    assert "only if work stalls" in ES._recovery_hint(payload, None, 0.5)


def test_critical_state_guidance_does_not_tell_the_agent_to_pause():
    """interpret_state knows no decision, and a standard sync_state or
    metrics read surfaces its guidance (the envelope uses it as next_action
    when no next_action is set). A guided cold-start agent at risk 0.79 was
    told "Circuit breaker imminent. Pause and reassess." there."""
    from src.governance_state import GovernanceState

    guidance = GovernanceState._generate_guidance(
        None, health="critical", basin="low", mode="stalled",
        trajectory="stable", task_type="mixed", borderline={},
    )
    assert "pause" not in guidance.lower()
    assert "imminent" not in guidance.lower()

    formatted = format_response(_guided_cold_start_check_in(), {"response_mode": "standard"})
    # interpret_state's text lands at state.guidance (top-level guidance is
    # the decision's own); read the field that actually carries it.
    state_guidance = formatted["state"]["guidance"]
    assert "critical band" in state_guidance
    assert "pause" not in state_guidance.lower()
    env = build_experience_envelope(
        "check_working_state", "get_governance_metrics",
        {"success": True, "guidance": guidance}, {},
    )
    assert env.get("next_action") == guidance


def test_never_checked_in_agent_is_not_reported_as_proceeding():
    wrapped = explain_verdict(
        "uninitialized", evidence_source="ode_fallback",
        decision_action=_last_decision_action(_Meta("active", [], total_updates=0)),
    )
    assert "decision_action" not in wrapped
    assert ES._decision_action({"verdict": wrapped}) == "uninitialized"


def test_resumed_wording_claims_no_decision():
    wrapped = explain_verdict("high-risk", decision_action="resumed")
    assert "The decision was" not in wrapped["next_action"]
    assert "resumed" in wrapped["next_action"]


def test_a_real_pause_keeps_its_stop_and_recovery_directives():
    source = _guided_cold_start_check_in()
    source["decision"] = {"action": "pause", "sub_action": "risk_pause"}
    source["policy_evaluation"]["action"] = "pause"
    source["policy_evaluation"]["sub_action"] = "risk_pause"
    source["enforcement"] = {"requested": True, "applied": True}
    env = _envelope(source, "auto")
    assert env["action_summary"]["action"] == "pause"
    assert "stop this line of work" in env["next_action"]
    # At 0.79 reviewed self-recovery refuses, so the stop routes elsewhere.
    assert "pause this line of work" in env["recovery_hint"]
    assert "request_review" in env["recovery_hint"]
    assert "self_recovery(" not in env["recovery_hint"]


def test_a_stopped_agent_is_routed_to_review_only_below_its_gate():
    """Review records the reflection and then refuses at or above
    MAX_RISK_FOR_SELF_RECOVERY; a stop below that limit keeps the directive."""
    from src.mcp_handlers.lifecycle.self_recovery import MAX_RISK_FOR_SELF_RECOVERY

    limit = MAX_RISK_FOR_SELF_RECOVERY
    paused = {"decision": {"action": "pause"}}
    below = ES._recovery_hint(paused, None, limit - 0.01)
    above = ES._recovery_hint(paused, None, limit)
    assert "self_recovery(action='review'" in below
    assert "self_recovery(" not in above
    assert "request_review" in above


# --- _recovery_hint directly ------------------------------------------------

def test_unknown_decision_with_high_risk_is_still_severe():
    """No decision anywhere: behavior unchanged."""
    hint = ES._recovery_hint({"risk_score": 0.8}, None, 0.8)
    assert "pause this line of work" in hint
    assert "request_review" in hint


def test_metrics_read_shape_follows_the_verdicts_decision_action():
    proceeding = {"verdict": explain_verdict("high-risk", decision_action="proceed")}
    paused = {"verdict": explain_verdict("high-risk", decision_action="pause")}
    assert "pause" not in ES._recovery_hint(proceeding, None, 0.79).lower()
    assert "pause this line of work" in ES._recovery_hint(paused, None, 0.79)


def test_mirror_shape_escalated_after_policy_evaluation_reports_the_pause():
    """Priority of decision fields: anything that rewrites decision.action after
    policy_evaluation was built leaves the policy record stale, and mirror mode
    drops the decision, so the verdict's decision_action must outrank it.

    The only such rewriter is the post-ODE dialectic escalation, which this
    branch caps at guide, so a pause of this shape no longer arises from it.
    The test pins the field priority, not the escalation."""
    source = _guided_cold_start_check_in()
    source["decision"] = {"action": "pause", "sub_action": "dialectic_condition"}
    # policy_evaluation still says proceed: it predates the escalation.
    env = _envelope(source, "mirror")
    assert env["action_summary"]["action"] == "pause"
    assert "pause this line of work" in env["recovery_hint"]


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
    def __init__(self, status="active", recent_decisions=None, total_updates=None):
        self.status = status
        self.recent_decisions = recent_decisions
        self.total_updates = (
            len(recent_decisions or []) if total_updates is None else total_updates
        )


def test_last_decision_action_prefers_a_paused_lifecycle_status():
    assert _last_decision_action(_Meta("paused", ["proceed"])) == "pause"
    assert _last_decision_action(_Meta("active", ["pause", "proceed"])) == "proceed"
    # Never checked in: no decision, keep the "uninitialized" wording.
    assert _last_decision_action(_Meta("active", [], total_updates=0)) is None
    # Checked in before but no history: a recovery/resume cleared it or the
    # identity record carries none, so no resume is claimed.
    assert _last_decision_action(_Meta("active", [], total_updates=4)) == "not_paused"
    # A recorded stop under an active status was lifted: that is a resume.
    assert _last_decision_action(_Meta("active", ["proceed", "pause"])) == "resumed"


def test_empty_history_does_not_claim_a_resume():
    """An active agent that has checked in but carries an empty
    recent_decisions (cleared by a recovery/resume, or never persisted on its
    identity record) proves no resume; no text may say it was resumed."""
    action = _last_decision_action(_Meta("active", [], total_updates=12))
    wrapped = explain_verdict("high-risk", decision_action=action)
    assert "resumed" not in wrapped["next_action"]
    assert "nothing blocks it now" in wrapped["next_action"]
    env_payload = {"verdict": wrapped, "metrics": {"risk_score": 0.79}}
    hint = ES._recovery_hint(env_payload, None, 0.79)
    assert "pause" not in hint.lower()
    assert "resumed" not in hint.lower()
    assert ES._decision_action(env_payload) == "not_paused"


def test_above_gate_route_names_only_exits_that_accept_the_agent():
    """Operator resume refuses above its hard limit (force or not), and a
    pause auto-initiates a dialectic session by default, so request_review
    answers SESSION_EXISTS; the route names the open session first."""
    from src.mcp_handlers.lifecycle.self_recovery import OPERATOR_RESUME_HARD_RISK_LIMIT

    limit = OPERATOR_RESUME_HARD_RISK_LIMIT
    paused = {"decision": {"action": "pause"}}
    under = ES._recovery_hint(paused, None, limit)
    over = ES._recovery_hint(paused, None, limit + 0.05)
    for hint in (under, over):
        assert "dialectic(action='get', agent_id=" in hint
        assert hint.index("dialectic(action='get'") < hint.index("request_review")
    assert "An operator can resume you unless a void is active" in under
    assert "An operator can resume" not in over
    assert f"Operator resume refuses above risk {limit:.2f}" in over
    assert "expiry" in over


def test_review_refusal_names_the_legacy_exception():
    hint = ES._recovery_hint({"decision": {"action": "pause"}}, None, 0.79)
    assert "legacy cold-start trap" in hint
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
    assert _last_decision_action(_Meta("active", ["proceed", "pause"])) == "resumed"
    assert _last_decision_action(_Meta("active", ["reject"])) == "resumed"
    wrapped = explain_verdict(
        "high-risk", decision_action=_last_decision_action(_Meta("active", ["pause"]))
    )
    assert not wrapped["next_action"].startswith("Pause")


# --- the server instructions ------------------------------------------------

def test_instructions_state_how_a_pause_actually_ends():
    """The exits in the code, stated as they are: self_recovery (not at high
    risk), dialectic resolution, an operator or the automatic safety nets,
    and expiry."""
    text = build_server_instructions("progressive")
    sentence = text[text.index("a pause holds governed writes"):]
    sentence = sentence[:sentence.index("expires.") + len("expires.")]
    for exit_route in ("self_recovery (not at high risk)", "dialectic",
                       "an operator", "a safety net", "or it expires"):
        assert exit_route in sentence, exit_route
    # agent(action='resume') has no ownership, risk or void gate and a paused
    # agent can call it on itself; it must never be advertised to agents.
    assert "agent(action='resume')" not in text


def test_instructions_paragraph_stays_inside_the_client_cutoff():
    """Claude Code truncates MCP server instructions at 2048 characters.
    Before this change the reading-paths sentence ended at 2016; the pause
    sentence must not push it past the cutoff."""
    text = build_server_instructions("progressive")
    marker = "Core workflow and advanced capabilities are reading paths, not tool filters."
    assert text.index(marker) + len(marker) <= 2048


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
    action = _last_decision_action(_Meta("active", [], total_updates=3))
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


def test_resumed_agent_hints_claim_no_decision():
    """No check-in decided anything after a resume: the hint must not say
    'this decision'."""
    wrapped = explain_verdict("high-risk", decision_action="resumed")
    for risk in (0.55, 0.75):
        hint = ES._recovery_hint({"verdict": wrapped}, None, risk)
        assert "this decision" not in hint, (risk, hint)
        assert "nothing blocks you now" in hint, (risk, hint)


def test_verification_floor_verdict_is_not_called_the_prior():
    payload = {
        "decision": {"action": "proceed", "sub_action": "guide"},
        "metrics": {"risk_score": 0.55, "primary_eisv_source": "ode_fallback"},
        "risk_attribution": {"primary_driver": "independent_verification_floor"},
    }
    hint = ES._recovery_hint(payload, None, 0.55)
    assert not hint.startswith("Cold start")
    assert "the prior" not in hint


def test_full_metrics_read_follows_the_last_decision():
    """check_working_state(lite=false) keeps the raw verdict string; the
    decision it rode on travels beside it and must win over the alias."""
    payload = {"verdict": "high-risk", "risk_score": 0.79,
               "primary_eisv_source": "ode_fallback",
               "last_decision_action": "proceed"}
    assert ES._decision_action(payload) == "proceed"
    assert "pause and call" not in ES._recovery_hint(payload, None, 0.79)


def test_default_metrics_read_keeps_the_guide_sub_action():
    """recent_decisions stores a bare 'proceed'; a caution verdict still means
    a guided proceed."""
    payload = {"verdict": explain_verdict("caution", decision_action="proceed"),
               "risk_score": 0.5}
    summary = ES._action_summary(payload, 0.5)
    assert (summary["action"], summary["sub_action"]) == ("proceed", "guide")
