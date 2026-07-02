"""Tests for cohort_prior.py — shadow, read-only cohort warm-start priors.

The load-bearing test is `test_seed_stays_inert_until_agent_earns_it`: it proves
the anti-poisoning invariant the council required — a warm-started agent still
cannot z-score until it has logged its own real observations.
"""

import math

import pytest

from src.agent_behavioral_baseline import AgentBehavioralBaseline, WelfordStats
from src.cohort_prior import (
    CohortPrior,
    Z_SCORE_ACTIVATION_COUNT,
    DEFAULT_PSEUDO_COUNT,
    cohort_prior_enabled,
    cohort_prior_mode,
)

SIGNAL = "coherence"


def _baseline_with(signal: str, values) -> AgentBehavioralBaseline:
    b = AgentBehavioralBaseline()
    for v in values:
        b.update(signal, v)
    return b


def _cohort(n_agents=4, per_agent=8, base=0.5, spread=0.1):
    """A cohort of characterized agents whose `coherence` means straddle `base`."""
    baselines = []
    for i in range(n_agents):
        centre = base + (i - (n_agents - 1) / 2) * spread
        vals = [centre + 0.01 * (j - (per_agent - 1) / 2) for j in range(per_agent)]
        baselines.append(_baseline_with(SIGNAL, vals))
    return baselines


class TestActivationGate:
    def test_activation_gate_matches_welford(self):
        """Our gate constant must equal WelfordStats' real inert threshold.

        If z_score's `count < 5` guard ever moves, this fails loudly instead of
        letting Z_SCORE_ACTIVATION_COUNT silently drift out of sync.
        """
        s = WelfordStats()
        s.mean = 0.5
        s.m2 = 1.0  # non-trivial variance
        # Inert at gate-1, active at the gate.
        s.count = Z_SCORE_ACTIVATION_COUNT - 1
        assert s.z_score(10.0) == 0.0
        s.count = Z_SCORE_ACTIVATION_COUNT
        assert s.z_score(10.0) != 0.0


class TestBuild:
    def test_pools_grand_mean(self):
        prior = CohortPrior.build(_cohort(base=0.5, spread=0.0))
        mean, std, total, n = prior.prior_for(SIGNAL)
        assert mean == pytest.approx(0.5, abs=1e-9)
        assert n == 4
        assert total == 32

    def test_pooled_std_includes_between_agent_spread(self):
        """A cohort whose agents disagree yields a wider prior than any single agent."""
        baselines = _cohort(spread=0.2)
        prior = CohortPrior.build(baselines)
        _, pooled_std, _, _ = prior.prior_for(SIGNAL)
        worst_single = max(b._stats[SIGNAL].std for b in baselines)
        assert pooled_std > worst_single

    def test_excludes_undercharacterized_agents(self):
        """Agents below min_agent_count don't contribute."""
        rich = _cohort(n_agents=2, per_agent=8)
        thin = [_baseline_with(SIGNAL, [0.9, 0.9])]  # count=2 < gate
        prior = CohortPrior.build(rich + thin)
        _, _, total, n = prior.prior_for(SIGNAL)
        assert n == 2  # thin agent excluded
        assert total == 16

    def test_requires_min_contributors(self):
        """A single characterized agent is not enough to form a prior."""
        prior = CohortPrior.build([_baseline_with(SIGNAL, [0.5] * 8)])
        assert prior.prior_for(SIGNAL) is None

    def test_empty_cohort_has_no_priors(self):
        prior = CohortPrior.build([])
        assert prior.signals == ()
        assert prior.prior_for(SIGNAL) is None


class TestSeed:
    def test_seed_carries_mean_and_widened_std(self):
        prior = CohortPrior.build(_cohort(base=0.5, spread=0.05))
        mean, std, _, _ = prior.prior_for(SIGNAL)
        seed = prior.seed_welford(SIGNAL, pseudo_count=3, widen=2.0)
        assert seed.mean == pytest.approx(mean)
        assert seed.std == pytest.approx(std * 2.0, rel=1e-9)
        assert seed.count == 3

    def test_seed_absent_signal_is_none(self):
        prior = CohortPrior.build([])
        assert prior.seed_welford(SIGNAL) is None

    def test_pseudo_count_at_or_above_gate_rejected(self):
        prior = CohortPrior.build(_cohort())
        with pytest.raises(ValueError, match="activation gate"):
            prior.seed_welford(SIGNAL, pseudo_count=Z_SCORE_ACTIVATION_COUNT)
        with pytest.raises(ValueError):
            prior.seed_welford(SIGNAL, pseudo_count=1)  # variance undefined

    def test_widen_below_one_rejected(self):
        prior = CohortPrior.build(_cohort())
        with pytest.raises(ValueError, match="widen"):
            prior.seed_welford(SIGNAL, widen=0.5)

    def test_seed_stays_inert_until_agent_earns_it(self):
        """THE anti-poisoning invariant.

        A seeded baseline carries the cohort mean/std but must still return a
        z-score of 0 for any value until the agent has logged >= gate of its OWN
        observations. Otherwise a cohort-correlated bias would silence a
        newcomer's early drift.
        """
        prior = CohortPrior.build(_cohort(base=0.5, spread=0.05))
        seeded = prior.seed_baseline(pseudo_count=DEFAULT_PSEUDO_COUNT)

        # Wildly off-baseline value scores 0 while still seeded-not-earned.
        assert seeded.z_score(SIGNAL, 100.0) == 0.0
        assert not seeded.is_anomalous(SIGNAL, 100.0)

        # Feed real observations until the agent's OWN count crosses the gate.
        needed = Z_SCORE_ACTIVATION_COUNT - DEFAULT_PSEUDO_COUNT
        for _ in range(needed - 1):
            seeded.update(SIGNAL, 0.5)
            assert seeded.z_score(SIGNAL, 100.0) == 0.0  # still inert
        seeded.update(SIGNAL, 0.5)  # now count == gate
        assert seeded.z_score(SIGNAL, 100.0) != 0.0  # now it can flag drift

    def test_seed_baseline_leaves_prior_less_signals_cold(self):
        """Signals with no cohort prior are left as fresh Welford stats."""
        # Cohort only characterizes `coherence`; other signals have no prior.
        prior = CohortPrior.build(_cohort())
        seeded = prior.seed_baseline()
        assert seeded._stats["coherence"].count == DEFAULT_PSEUDO_COUNT
        assert seeded._stats["tool_error_rate"].count == 0


class TestFlag:
    def test_default_off(self, monkeypatch):
        monkeypatch.delenv("UNITARES_COHORT_PRIOR", raising=False)
        assert cohort_prior_enabled() is False

    def test_opt_in(self, monkeypatch):
        monkeypatch.setenv("UNITARES_COHORT_PRIOR", "on")
        assert cohort_prior_enabled() is True

    def test_mode_defaults_to_observe(self, monkeypatch):
        monkeypatch.delenv("UNITARES_COHORT_PRIOR_MODE", raising=False)
        assert cohort_prior_mode() == "observe"

    def test_mode_apply_is_explicit(self, monkeypatch):
        monkeypatch.setenv("UNITARES_COHORT_PRIOR_MODE", "apply")
        assert cohort_prior_mode() == "apply"

    def test_mode_unknown_falls_back_to_observe(self, monkeypatch):
        monkeypatch.setenv("UNITARES_COHORT_PRIOR_MODE", "whatever")
        assert cohort_prior_mode() == "observe"

    def test_seed_never_persists(self):
        """Building/seeding must not touch the global baseline registry."""
        from src import cohort_prior  # noqa: F401
        from src.agent_behavioral_baseline import _baselines

        before = dict(_baselines)
        prior = CohortPrior.build(_cohort())
        prior.seed_baseline()
        assert dict(_baselines) == before
