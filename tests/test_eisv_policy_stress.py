"""Policy coherence invariants — label-free regression guards.

These assert properties the behavioral verdict policy should ALWAYS satisfy,
independent of outcome data: monotonicity in obvious badness, graded recall of a
constructed deterioration, and order-sensitivity (it claims to be a trajectory
governor). A change that breaks one of these is a policy-coherence regression.
"""
from scripts.analysis.eisv_policy_stress import (
    probe_injection_recall,
    probe_monotonicity,
    probe_time_shuffle,
)


def test_policy_monotone_in_obvious_badness():
    m = probe_monotonicity()
    assert m["mono_S"], m["rows_S"]
    assert m["mono_I"], m["rows_I"]


def test_injection_recall_is_graded_and_escalates():
    inj = probe_injection_recall()
    assert inj["graded"], inj["rows"]
    assert inj["escalates"], inj["rows"]
    # the zero-severity injection must not raise risk above the no-op floor
    assert inj["rows"][0][1] < 1e-6


def test_policy_is_order_sensitive():
    s = probe_time_shuffle()
    assert s["order_sensitive"], s
