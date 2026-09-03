"""The margin reporter must quote the void gate, not invent a second threshold.

2026-08-20. `check_void_state` decides `void_active` against an adaptive
threshold (mean|V| + 2sigma, clamped to [0.10, 0.30]) or a class override --
0.30 for `embodied` and `resident_persistent`, whose substrate-state asymmetry
puts steady-state |V| past 0.15 by construction. The 2026-05-01 Steward
auto-pause incident is why that override exists.

`compute_proprioceptive_margin` measured the void edge against the static
VOID_THRESHOLD_INITIAL = 0.15 regardless, so the two could contradict each other
about the same agent on the same tick. Measured over 7 days of non-synthetic
core.agent_state: 5,160 of 10,093 rows sit past 0.15 -- and ALL 5,160 carry
`verdict: safe`. Replaying with each agent's runtime threshold moves that to
3,406 rows, concentrated on the two resident agents the override targets
(Lumen 3,099 -> 2,211, Steward 1,905 -> 1,192).

Not uniformly a relaxation: an agent whose |V| sits near zero gets an adaptive
threshold clamped to VOID_THRESHOLD_MIN = 0.10, stricter than the old static
0.15, where the reporter had been too lenient.
"""
import numpy as np
import pytest

from config.governance_config import GovernanceConfig as G

HEALTHY_COH = 0.4819
LEGACY = "ode_control_feedback"


def margin(**kw):
    kw.setdefault("risk_score", 0.0)
    kw.setdefault("coherence", HEALTHY_COH)
    kw.setdefault("coherence_history", [HEALTHY_COH] * 5)
    kw.setdefault("coherence_role", LEGACY)
    return G.compute_proprioceptive_margin(**kw)


# --- the invariant: the reporter cannot contradict the gate -------------------

@pytest.mark.parametrize("v,thr", [
    (0.28, 0.30),   # embodied resident, inside its own gate
    (0.29, 0.30),   # resident_persistent, just inside
    (0.09, 0.10),   # adaptive floor, near-zero |V| agent
    (0.14, 0.15),   # static fallback
])
def test_gate_says_inactive_implies_reporter_never_calls_it_crossed(v, thr):
    """void_active is `|V| > threshold`, so quoting the same threshold makes
    agreement structural rather than tuned: inactive => margin >= 0."""
    r = margin(void_active=False, void_value=v, void_threshold=thr)
    assert r["details"]["void_margin"] >= 0
    assert r["margin"] not in ("warning", "critical")


def test_resident_inside_its_class_override_is_no_longer_critical():
    """|V| = 0.28 for an embodied agent: past the static 0.15, inside the 0.30
    the gate actually used. Was critical/void; is not."""
    old = margin(void_active=False, void_value=0.28)                      # no threshold -> static
    new = margin(void_active=False, void_value=0.28, void_threshold=0.30)
    assert old["margin"] == "critical" and old["nearest_edge"] == "void"
    assert new["margin"] not in ("warning", "critical")


def test_alignment_is_not_uniformly_a_relaxation():
    """Near-zero-|V| agents get an adaptive threshold clamped to 0.10, which is
    STRICTER than the old static 0.15. The reporter was too lenient there."""
    v = 0.12
    assert margin(void_active=False, void_value=v)["details"]["void_margin"] > 0
    tightened = margin(void_active=True, void_value=v, void_threshold=0.10)
    assert tightened["margin"] == "critical"


def test_active_void_still_reports_crossed_at_any_threshold():
    r = margin(void_active=True, void_value=0.35, void_threshold=0.30)
    assert r["nearest_edge"] == "void"
    assert r["margin"] in ("warning", "critical")


def test_band_scales_with_the_threshold_actually_used():
    """A band fixed to the static value would be the wrong width for a 0.30 gate."""
    wide = margin(void_active=False, void_value=0.0, void_threshold=0.30)
    assert wide["details"]["void_threshold"] == 0.30
    # comfortable: full margin 0.30, band 0.30*fraction -- proportional either way
    assert wide["margin"] == "comfortable"


def test_absent_threshold_falls_back_to_the_static_value():
    """Before the gate has run there is nothing to quote."""
    r = margin(void_active=False, void_value=0.05)
    assert r["details"]["void_threshold"] == G.VOID_THRESHOLD_INITIAL


# --- the gate publishes what it decided against ------------------------------

def test_check_void_state_publishes_its_threshold():
    from src.monitor_void import check_void_state

    class S:
        V = 0.28
        V_history = [0.28] * 20
        void_active = False
        agent_class = "embodied"

    st = S()
    active = check_void_state(st)
    assert st.void_threshold_effective == pytest.approx(0.30)
    assert active is False, "0.28 is inside the embodied override"


def test_published_threshold_and_reporter_agree_end_to_end():
    """The whole point: gate and reporter reach the same verdict on one state."""
    from src.monitor_void import check_void_state

    class S:
        V = 0.28
        V_history = [0.28] * 20
        void_active = False
        agent_class = "embodied"

    st = S()
    active = check_void_state(st)
    r = margin(void_active=active, void_value=st.V,
               void_threshold=st.void_threshold_effective)
    assert not active
    assert r["details"]["void_margin"] >= 0


# --- dialectic condition 4: a comfortable result must say what it covers ------

def test_comfortable_is_qualified_when_an_edge_was_never_assessed():
    """"comfortable" is a claim about the edges that could be MEASURED. Reported
    bare it reads as "no limit is near", which is a stronger claim than the data
    supports whenever an edge was excluded."""
    r = margin(void_active=False, void_value=0.0)   # legacy coherence -> unmeasurable
    assert r["margin"] == "comfortable"
    assert r["unmeasurable_edges"] == ["coherence"]
    assert r["margin_scope"] == "measured_edges_only"


def test_scope_is_all_edges_when_nothing_was_excluded():
    r = margin(void_active=False, void_value=0.0,
               coherence_history=[HEALTHY_COH] * 12,
               coherence_role=G.COHERENCE_INTERPRETABLE_ROLE,
               coherence_history_role=G.COHERENCE_INTERPRETABLE_ROLE)
    assert r["unmeasurable_edges"] == []
    assert r["margin_scope"] == "all_edges"


def test_qualification_reaches_the_agent_envelope():
    """`details` is stripped before an agent sees it, so the qualification has to
    ride on the lifted fields or it is not a report."""
    from src.mcp_handlers.middleware import envelope_step
    import inspect
    src = inspect.getsource(envelope_step)
    assert '"margin_scope"' in src and '"unmeasurable_edges"' in src, \
        "margin_scope/unmeasurable_edges must be lifted alongside margin"


def test_qualification_survives_the_real_decision_path_end_to_end():
    """Regression for a gap the unit tests missed: monitor_decision delegates the
    common safe/approve case to GovernanceConfig.make_decision, which builds its
    OWN decision dicts. Patching only monitor_decision left a bare "comfortable"
    reaching agents on the most-travelled path. Exercise the monitor, not the
    margin function."""
    from src.governance_monitor import UNITARESMonitor

    m = UNITARESMonitor(agent_id="scope-e2e")
    m.state.unitaires_state.V = 0.02
    m.state.V_history = [0.02] * 20
    m.state.coherence_history = [HEALTHY_COH] * 6   # legacy role -> unmeasurable
    m.check_void_state()
    d = m.make_decision(risk_score=0.0, unitares_verdict="safe")

    assert d["margin"] == "comfortable"
    assert d["margin_scope"] == "measured_edges_only"
    assert d["unmeasurable_edges"] == ["coherence"]
