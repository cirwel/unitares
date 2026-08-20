"""Margin reporting: the coherence edge must not be judged when it is unmeasurable.

Background (2026-08-20). `compute_proprioceptive_margin` reports how close an
agent is to a decision edge. Coherence derives from the ODE V that 69ee5a79
demoted and never re-sourced, so it rests in roughly [0.455, 0.504] for every
agent -- its margin above the 0.40 gate is a near-constant ~0.08 and no band
separates "near the floor" from "normal", because normal IS near the floor.

Evidence, stated at the scope it was measured. Fleet-wide 7d non-synthetic
n=10,071 rows, but 83% of those are four resident agents (one contributes 3,296
rows spanning a coherence range of 0.0008), so the honest population is the
interactive tail: 173 identities, 1,674 rows, max 0.5039 -- none reach the
`coherence > 0.55` the old fixed 0.15 band required to read "comfortable".
Replaying real check-ins shows the >=10-history adaptive branch firing zero
times over the same week.

So the fix gates on PROVENANCE (coherence_role), matching src/pattern_analysis.py,
and reports the edge as unmeasurable rather than silently calling it comfortable.
"""
import pytest

from config.governance_config import GovernanceConfig as G

HEALTHY_COH = 0.4819       # a real observed value; interactive-tail p50 ~0.4879
LEGACY = "ode_control_feedback"
REPAIRED = G.COHERENCE_INTERPRETABLE_ROLE


def margin(**kw):
    kw.setdefault("risk_score", 0.0)
    kw.setdefault("coherence", HEALTHY_COH)
    kw.setdefault("void_active", False)
    return G.compute_proprioceptive_margin(**kw)


# --- the reported defect -----------------------------------------------------

@pytest.mark.parametrize("n", [3, 5, 9, 12, 40])
def test_legacy_coherence_is_reported_unmeasurable_at_every_history_length(n):
    """The old gate keyed on history length, so the SAME state read tight at 9
    samples and comfortable at 10. Provenance does not vary with age."""
    r = margin(coherence_history=[HEALTHY_COH] * n, coherence_role=LEGACY)
    assert r["unmeasurable_edges"] == ["coherence"]
    assert r["nearest_edge"] != "coherence"


def test_unknown_provenance_fails_toward_unknown_not_toward_fine():
    r = margin(coherence_history=[HEALTHY_COH] * 12, coherence_role=None)
    assert "coherence" in r["unmeasurable_edges"]


def test_healthy_agent_is_not_told_it_is_near_an_edge():
    r = margin(coherence_history=[HEALTHY_COH] * 5, coherence_role=LEGACY)
    assert r["margin"] == "comfortable"


def test_age_does_not_change_the_verdict_on_unchanged_state():
    before = margin(coherence_history=[HEALTHY_COH] * 9, coherence_role=LEGACY)
    after = margin(coherence_history=[HEALTHY_COH] * 10, coherence_role=LEGACY)
    assert before["margin"] == after["margin"]


# --- the edge must re-admit itself when the metric is repaired ---------------

def test_repaired_provenance_re_admits_the_coherence_edge():
    """The canary. A history-length gate would have retired this lever
    permanently; a provenance gate returns it once coherence is re-sourced AND
    the history window is known to be same-provenance."""
    r = margin(coherence_history=[HEALTHY_COH] * 12, coherence_role=REPAIRED,
               coherence_history_role=REPAIRED)
    assert r["unmeasurable_edges"] == []
    assert r["details"]["coherence_tight_threshold"] == pytest.approx(
        max(HEALTHY_COH * 0.10, 0.03)
    )


def test_repaired_coherence_still_catches_decline_against_own_baseline():
    r = margin(coherence=0.435, coherence_history=[0.52] * 6 + [0.44] * 6,
               coherence_role=REPAIRED, coherence_history_role=REPAIRED)
    assert r["nearest_edge"] == "coherence"
    assert r["margin"] == "tight"


# --- the safety path is untouched -------------------------------------------

@pytest.mark.parametrize("role", [LEGACY, None, REPAIRED])
def test_crossed_coherence_floor_fires_regardless_of_provenance(role):
    """Suppressing an unmeasurable BAND must never suppress an actual crossing.
    The floor has been crossed 9 times in 79,567 all-time rows (min 0.2880), so
    this regime is rare, not impossible."""
    r = margin(coherence=0.30, coherence_history=[0.30] * 5, coherence_role=role)
    assert r["nearest_edge"] == "coherence"
    assert r["margin"] in ("warning", "critical")
    assert r["distance_to_edge"] < 0


# --- the void band (the twin defect the coherence fix exposed) --------------

def test_inactive_void_is_comfortable_not_tight():
    """void_margin maxes at exactly VOID_THRESHOLD_INITIAL and the old shared
    band was the same 0.15, so `0.15 > 0.15` made a completely inactive void
    read tight -- comfortable was unreachable on this edge too."""
    r = margin(void_value=0.0, coherence_history=[HEALTHY_COH] * 5, coherence_role=LEGACY)
    assert r["margin"] == "comfortable"


def test_void_near_its_threshold_still_reads_tight():
    r = margin(void_value=0.14, coherence_history=[HEALTHY_COH] * 5, coherence_role=LEGACY)
    assert r["nearest_edge"] == "void"
    assert r["margin"] == "tight"


# --- other edges keep working while coherence is unmeasurable ---------------

def test_risk_edge_still_reported():
    r = margin(risk_score=0.60, coherence_history=[HEALTHY_COH] * 5, coherence_role=LEGACY)
    assert r["nearest_edge"] == "risk"
    assert r["margin"] == "tight"


@pytest.mark.parametrize("hist", [None, [], [HEALTHY_COH], [HEALTHY_COH] * 2])
def test_warmup_under_three_samples_still_settles(hist):
    r = margin(coherence_history=hist, coherence_role=LEGACY)
    assert r["margin"] == "settling"
    assert r["nearest_edge"] is None


def test_unmeasurable_is_surfaced_above_details_not_buried_in_it():
    """`details` is stripped before the envelope reaches an agent (verified
    against the live MCP), so a diagnostic placed there is a comment, not a
    report. The caller lifts this key onto the decision."""
    r = margin(coherence_history=[HEALTHY_COH] * 5, coherence_role=LEGACY)
    assert "unmeasurable_edges" in r


def test_risk_band_is_byte_unchanged_by_the_per_edge_rule():
    """The fraction is the historical band over the risk gate, taken exactly, so
    the risk edge keeps its 0.15 band and no risk_score changes classification.
    An earlier draft rounded to 0.2, which silently moved the risk boundary from
    0.55 to 0.56 -- a change nobody asked for, on the only edge that was working."""
    assert G.RISK_REVISE_THRESHOLD * G.TIGHT_BAND_FRACTION == pytest.approx(0.15)
    # the boundary the rounding would have moved
    assert margin(risk_score=0.551, coherence_history=[HEALTHY_COH] * 5,
                  coherence_role=LEGACY)["nearest_edge"] == "risk"


def test_repaired_role_with_legacy_history_still_fails_closed():
    """A role string alone must not re-admit a BASELINE-relative edge. Across a
    producer change the untagged window holds legacy samples, so the baseline
    would be computed from the very metric ruled ineligible -- legacy values
    bootstrapping the band for their own replacement. Fail closed until the
    producer resets or partitions history on role change."""
    r = margin(coherence_history=[HEALTHY_COH] * 12, coherence_role=REPAIRED,
               coherence_history_role=LEGACY)
    assert r["unmeasurable_edges"] == ["coherence"]
    assert r["details"]["coherence_tight_threshold"] is None

    untagged = margin(coherence_history=[HEALTHY_COH] * 12, coherence_role=REPAIRED,
                      coherence_history_role=None)
    assert untagged["unmeasurable_edges"] == ["coherence"]
