"""The BOUNDARY basin must not fabricate a tight margin it cannot explain.

2026-08-20. `basin` and `margin` are two different notions of "edge": basin is
an EISV state-space region, margin is distance to a decision threshold. The
BOUNDARY branch collapsed them -- it hardcoded margin='tight' because the state
sits near a BASIN boundary, then reported nearest_edge from margin_info, which
is None whenever the threshold margin is comfortable.

The result told an agent "you are close to a governance edge" while being unable
to name one, and violated this module's own documented contract: only
tight/warning/critical carry a non-null nearest_edge (_ACTIONABLE_MARGINS in
response_formatter). Since "tight" is in that actionable set, a comfortable
agent was surfaced as needing attention on an unnameable edge.
"""
import pytest

from config.governance_config import classify_basin
from src import monitor_decision as MD
from src.mcp_handlers.response_formatter import _ACTIONABLE_MARGINS


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


def decide(state=None, risk=0.0, verdict="safe"):
    return MD.make_decision(state or _State(), risk_score=risk,
                            unitares_verdict=verdict, response_tier="proceed")


def test_the_fixture_really_is_in_the_boundary_basin():
    """Otherwise the rest of this file tests nothing -- the branch never runs."""
    s = _State()
    assert classify_basin(E=s.E, I=s.I, S=s.S, V=s.V,
                          coherence=s.coherence, risk_score=0.0) == "boundary"
    assert decide()["basin"] == "boundary"


def test_boundary_basin_no_longer_fabricates_a_tight_margin():
    d = decide()
    assert d["margin"] == "comfortable", (
        "the threshold margin is comfortable; the basin is a separate fact"
    )


def test_the_documented_contract_holds_an_actionable_margin_names_its_edge():
    """This is the invariant that was broken: margin in _ACTIONABLE_MARGINS
    implies a non-null nearest_edge."""
    for risk in (0.0, 0.1, 0.2, 0.3, 0.4):
        d = decide(risk=risk)
        if d["margin"] in _ACTIONABLE_MARGINS:
            assert d["nearest_edge"] is not None, (
                f"risk={risk}: margin {d['margin']!r} is actionable but names no edge"
            )


def test_the_boundary_condition_is_still_reported():
    """Nothing is lost by dropping the fabricated margin -- the basin is carried
    by its own field, the sub_action, and the reason text."""
    d = decide()
    assert d["basin"] == "boundary"
    assert d["sub_action"] == "guide"
    assert "Boundary basin" in d["reason"]
    assert "basin boundary" in d["guidance"]


def test_a_genuinely_tight_threshold_in_a_boundary_basin_still_reports_tight():
    """The fix must not suppress a real edge, only stop inventing one. Risk 0.60
    puts the risk margin (0.10) inside its 0.15 band."""
    d = decide(risk=0.60)
    assert d["margin"] == "tight"
    assert d["nearest_edge"] == "risk"


def test_margin_scope_still_rides_along_on_this_path():
    d = decide()
    assert d["margin_scope"] == "measured_edges_only"
    assert d["unmeasurable_edges"] == ["coherence"]
