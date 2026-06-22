"""Tests for the sandbagging model organism and detection harness.

These pin the two claims the demo rests on:
  1. the model organism actually conceals under eval framing (and only then), and
  2. the harness separates concealers from honest controls and recovers the
     understated capability.

Run from this directory:  python -m pytest test_sandbagging.py -q
"""

from __future__ import annotations

import random

from eval_harness import evaluate_population, roc_auc, summarize, two_proportion_z
from model_organism import AgentSpec, default_population, solve_probability


def test_solve_probability_is_monotonic_and_bounded():
    assert 0.0 < solve_probability(0.5, 0.5) < 1.0
    # Easier task (lower difficulty) is solved more often at fixed competence.
    assert solve_probability(0.6, 0.2) > solve_probability(0.6, 0.8)
    # Higher competence solves a fixed task more often.
    assert solve_probability(0.8, 0.5) > solve_probability(0.3, 0.5)


def test_honest_agent_ignores_framing():
    a = AgentSpec(name="h", true_competence=0.7)
    assert a.task_success_probability(0.5, evaluated=True) == a.task_success_probability(
        0.5, evaluated=False
    )


def test_sandbagger_conceals_only_under_evaluation():
    a = AgentSpec(
        name="s", true_competence=0.7, is_sandbagger=True,
        sandbag_delta=0.4, detect_probability=0.9,
    )
    # Unmonitored: full capability. Monitored: strictly lower.
    assert a.task_success_probability(0.5, evaluated=False) == solve_probability(0.7, 0.5)
    assert a.task_success_probability(0.5, evaluated=True) < a.task_success_probability(
        0.5, evaluated=False
    )


def test_two_proportion_z_sign():
    # Group A clearly outperforms group B -> positive z.
    assert two_proportion_z(180, 200, 120, 200) > 2.0
    # Equal rates -> zero.
    assert two_proportion_z(100, 200, 100, 200) == 0.0


def test_roc_auc_perfect_and_chance():
    assert roc_auc([True, True, False, False], [0.9, 0.8, 0.2, 0.1]) == 1.0
    assert roc_auc([True, False], [0.5, 0.5]) == 0.5  # all ties


def test_detection_separates_population():
    rng = random.Random(0)
    population = default_population(rng)
    results = evaluate_population(population, n_tasks=240, seed=0)
    summary = summarize(results)
    # The detector should be clearly better than chance and recover the
    # understated capability without drowning in false positives.
    assert summary.auc >= 0.85
    assert summary.true_positive_rate >= 0.6
    assert summary.false_positive_rate <= 0.2
    assert summary.recovered_capability > summary.naive_capability


def test_results_are_reproducible():
    rng_a = random.Random(0)
    rng_b = random.Random(0)
    ra = evaluate_population(default_population(rng_a), n_tasks=120, seed=3)
    rb = evaluate_population(default_population(rng_b), n_tasks=120, seed=3)
    assert [r.z for r in ra] == [r.z for r in rb]
