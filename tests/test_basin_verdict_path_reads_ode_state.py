"""The verdict-path basin must read ODE state, never the behavioral E that
contains the classifier's own prior verdicts.

2026-08-21. The source-level cycle E -> basin -> guide -> decision_e -> E is
real: BOUNDARY returns sub_action='guide' unconditionally
(monitor_decision.py, Priority 6), that string lands in decision_history and
scores 0.7 against 1.0 for proceed (behavioral_sensor._DECISION_SCORES), and
decision_e is the largest single term in behavioral E (0.35 with outcomes,
0.40 without) -- a standing penalty of 0.105 / 0.12 on a guide-pinned agent.

It does not close today for one structural reason: the verdict path classifies
the basin from the ODE GovernanceState (governance_monitor.make_decision hands
`self.state` to monitor_decision.make_decision, which calls classify_basin on
state.E/I/S/V). The behavioral reading reaches that state only through the
k_anchor spring. A read over the whole persisted telemetry record (2026-08-10 ->
2026-08-21, n=12,628 BOUNDARY rows) found ODE E min 0.618 -- zero E-conjunct
failures; BOUNDARY is held by S and I.

SEQUENCING RULE this file enforces: any change that feeds behavioral/primary
EISV to classify_basin on the verdict path must neutralize or re-score
decision_e in the SAME change. Otherwise the E >= 0.6 conjunct starts binding
on a signal that contains the classifier's own prior verdicts, and roughly half
the BOUNDARY population (primary E < 0.6 in 46% of those rows) crosses on day
one and is held there by its own verdicts. See the contract's tested-claims
ledger, "Decision self-loop at the basin".
"""
import pytest

from src import governance_monitor as GM
from src import monitor_decision as MD
from src.behavioral_sensor import _DECISION_SCORES, _compute_E

_REPOINT_MESSAGE = (
    "classify_basin on the verdict path was fed an E/I/S/V that is not the "
    "ODE state's. If this is a deliberate repoint to behavioral/primary EISV, "
    "decision_e (behavioral_sensor._compute_E) must be neutralized or "
    "re-scored in the SAME change -- behavioral E contains the classifier's "
    "own prior verdicts (guide scores 0.7), and the E >= 0.6 conjunct would "
    "start binding on them. Sequencing rule: contract ledger, 'Decision "
    "self-loop at the basin' (2026-08-21)."
)


class _State:
    """Minimal state a decision needs. Values put it in the BOUNDARY basin."""
    E, I, S, coherence = 0.72, 0.67, 0.35, 0.4888
    V = -0.01
    void_active = False
    agent_class = None
    void_threshold_effective = 0.15
    coherence_role = None
    coherence_history_role = None
    lambda1 = 0.1
    update_count = 20

    def __init__(self, **kw):
        self.V_history = [self.V] * 20
        self.coherence_history = [self.coherence] * 6
        self.risk_history = [0.0]
        for k, v in kw.items():
            setattr(self, k, v)


def test_make_decision_classifies_basin_from_the_state_it_is_handed(monkeypatch):
    seen = {}
    real = MD.classify_basin

    def spy(**kw):
        seen.update(kw)
        return real(**kw)

    monkeypatch.setattr(MD, "classify_basin", spy)
    s = _State()
    d = MD.make_decision(s, risk_score=0.0, unitares_verdict="safe",
                         response_tier="proceed")
    assert d["basin"] == "boundary"  # the branch under test actually ran
    assert (seen["E"], seen["I"], seen["S"], seen["V"]) == (s.E, s.I, s.S, s.V), (
        _REPOINT_MESSAGE
    )


def test_monitor_hands_its_ode_state_object_to_the_verdict_path(monkeypatch):
    """The object identity matters: `self.state` is the ODE GovernanceState,
    not `_behavioral_state`. A future primary-EISV view object handed here
    would pass the value test above by accident if E happened to coincide."""
    m = GM.UNITARESMonitor("test-basin-verdict-path-guard", load_state=False)
    captured = {}

    def fake(state, *args, **kwargs):
        captured["state"] = state
        return {"action": "proceed", "sub_action": "approve", "basin": "high"}

    monkeypatch.setattr(GM, "_make_decision", fake)
    m.make_decision(0.0, "safe", "proceed")
    assert captured["state"] is m.state, _REPOINT_MESSAGE
    assert captured["state"] is not getattr(m, "_behavioral_state", object()), (
        _REPOINT_MESSAGE
    )


@pytest.mark.parametrize(
    "outcome_history, weight",
    [
        (None, 0.40),                                  # no-outcome blend
        ([{"is_bad": False}] * 3, 0.35),               # with-outcome blend
    ],
)
def test_the_behavioral_E_the_basin_does_not_read_carries_the_verdict_penalty(
    outcome_history, weight,
):
    """Documents the magnitude the guard above exists for. The gap is exactly
    weight * (score[approve] - score[guide]) because every other E term is
    held constant here; a constant decision series makes decision_e equal the
    score itself. This asserts the relationship, not the score values, so a
    deliberate re-scoring of guide changes the gap without failing the test."""
    e_ok = _compute_E(["approve"] * 10, outcome_history=outcome_history)
    e_guide = _compute_E(["guide"] * 10, outcome_history=outcome_history)
    gap = _DECISION_SCORES["approve"] - _DECISION_SCORES["guide"]
    assert e_ok - e_guide == pytest.approx(weight * gap, abs=1e-9)
