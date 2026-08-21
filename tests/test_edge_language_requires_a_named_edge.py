"""No agent-facing surface may claim edge proximity without naming the edge.

2026-08-20. The same conflation appeared on four surfaces: `basin` (where the
EISV state sits in state-space) and `margin`/`nearest_edge` (distance to a
decision threshold) are different facts, and each surface in turn reported the
former using the latter's language.

  #1774  monitor_decision   BOUNDARY basin hardcoded margin='tight'
  #1775  envelope hint      fixed one return...
  #1776  envelope hint      ...but not the one the live path takes
  here   reflection         `borderline` ORed into the nearest_edge branch

Each fix made the next visible, because the surfaces were fixed one at a time
as they surfaced. This file asserts the INVARIANT across surfaces instead, so a
fifth cannot appear silently: if a string claims proximity to an edge, an edge
must be nameable.
"""
import pytest

from src.mcp_handlers.updates.enrichments import _has_tight_margin
from src.mcp_handlers.middleware import envelope_step as ES

# Language that asserts proximity to a decision threshold.
EDGE_CLAIMS = ("close to a", "near an edge", "near the edge")


def claims_edge_proximity(text) -> bool:
    return isinstance(text, str) and any(c in text.lower() for c in EDGE_CLAIMS)


# --- the predicate itself ----------------------------------------------------

def test_tight_margin_predicate_is_false_for_a_comfortable_margin():
    assert _has_tight_margin({"decision": {"margin": "comfortable"}}) is False
    assert _has_tight_margin({"decision": {"margin": "settling"}}) is False
    assert _has_tight_margin({"decision": {"margin": "tight"}}) is True


# --- the recovery hint (envelope) -------------------------------------------

@pytest.mark.parametrize("payload", [
    {"verdict": {"value": "guide"}, "margin": "comfortable", "risk_score": 0.05},
    {"verdict": {"value": "guide"}, "risk_score": 0.05},
    {"verdict": "guide", "decision": {"action": "proceed"}, "margin": "comfortable",
     "risk_score": 0.05},
])
def test_recovery_hint_never_claims_an_edge_without_one(payload):
    h = ES._recovery_hint(payload, risk=payload.get("risk_score"), coherence=0.49)
    assert not claims_edge_proximity(h), f"hint asserts edge proximity: {h!r}"


def test_recovery_hint_keeps_edge_language_when_the_margin_is_real():
    h = ES._recovery_hint(
        {"verdict": {"value": "guide"}, "margin": "tight", "risk_score": 0.05},
        risk=0.05, coherence=0.49)
    assert claims_edge_proximity(h)


# --- the reflection ----------------------------------------------------------

def _reflection(decision, state=None, verdict="safe"):
    from src.mcp_handlers.updates import enrichments as E
    fn = getattr(E, "_reflection_line", None) or getattr(E, "_build_reflection", None)
    if fn is None:                      # name drift -- assert the source instead
        import inspect
        src = inspect.getsource(E)
        i = src.index("You're close to a governance edge")
        pytest.skip("reflection builder not directly callable; source asserted elsewhere")
    return fn({"decision": decision, "verdict": verdict}, state)


def test_borderline_basin_is_not_described_as_an_edge_in_source():
    """The boundary basin must not reach the nearest_edge branch."""
    import inspect
    from src.mcp_handlers.updates import enrichments as E
    src = inspect.getsource(E)
    assert '_has_tight_margin(response_data) or (isinstance(state, dict) and state.get("borderline"))' not in src, (
        "borderline (a basin fact) is ORed into the edge-proximity branch again"
    )
    assert "Your state sits near a basin boundary." in src


def test_no_generic_governance_edge_fallback_remains():
    """The old fallback fired whenever nearest_edge was None -- edge proximity
    asserted with nothing to name."""
    import inspect
    from src.mcp_handlers.updates import enrichments as E
    assert "You're close to a governance edge." not in inspect.getsource(E)
