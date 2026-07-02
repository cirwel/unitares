"""Cohort behavioral priors — shadow, read-only warm-start for per-agent baselines.

Council-reviewed "Site B" of the exponential/growth-dynamics scoping
(`docs/proposals/exponential-growth-dynamics-v0.md`). This is the ONE growth-shaped
move the review endorsed: a sharper starting prior *reduces* surprise, so it is
compatible with the contractive governor and never touches the verdict path.

What this module is
-------------------
A pure, read-only aggregator. It pools many characterized agents' Welford
baselines (`src.agent_behavioral_baseline.AgentBehavioralBaseline`) into a
per-signal cohort prior, then hands out a *seed* an operator could use to
warm-start a fresh agent's baseline so it starts near-calibrated instead of
cold ("~30 check-ins from scratch every time" — `docs/UNIFIED_ARCHITECTURE.md` §2).

What this module is NOT
-----------------------
It is not wired into any live path. It does not persist, does not mutate any
existing baseline, and is not called by any handler. Wiring it into the live
baseline-load path is a *separate*, coupled single-writer surface (see
`CLAUDE.md` → identity/onboarding) and is intentionally out of scope here so the
prior can be validated in shadow first.

The anti-poisoning invariant (the whole point)
----------------------------------------------
`WelfordStats.z_score` returns ``0.0`` while ``count < 5``
(`agent_behavioral_baseline.py`). That gate is what stops an
uncharacterized agent from being scored against statistics it has not earned.
The council's condition — "seed the prior mean/std, never flip the agent to
'baselined'" — reduces to a hard, testable rule here:

    a cohort seed MUST keep count below that activation gate.

So a warm-started agent carries the cohort mean/std but STILL cannot z-score
until it has logged its own real observations. That prevents a bad or
cohort-correlated cohort from silencing a newcomer's early drift signal — the
seed biases the *mean estimate*, never the *decision to start deciding*.
`seed_welford` enforces this bound and raises if a caller asks to exceed it.
"""

from __future__ import annotations

import os
from typing import Dict, Iterable, Optional, Tuple

from src.agent_behavioral_baseline import (
    AgentBehavioralBaseline,
    WelfordStats,
)

# The count below which WelfordStats.z_score is inert (returns 0.0). Kept in
# sync with `agent_behavioral_baseline.WelfordStats.z_score` by
# `test_cohort_prior.py::test_activation_gate_matches_welford` — if that gate
# ever moves, the test fails rather than this constant silently drifting.
Z_SCORE_ACTIVATION_COUNT = 5

# A seed must stay strictly below the gate. A pseudo-count of 2 is the smallest
# value for which Welford variance is defined (needs count >= 2), so it is the
# gentlest non-degenerate prior.
DEFAULT_PSEUDO_COUNT = 2

# Extra widening applied to the pooled std before seeding. >= 1.0. The council
# said "only widen seeded std": a wider prior is conservative (it makes the
# seeded agent slower to call something anomalous, never faster).
DEFAULT_WIDEN = 1.5

# Per-signal, only pool agents that are themselves characterized, and require
# more than one contributor so a single agent can never *be* the cohort prior.
DEFAULT_MIN_AGENT_COUNT = Z_SCORE_ACTIVATION_COUNT
MIN_CONTRIBUTORS = 2


def cohort_prior_enabled() -> bool:
    """Whether cohort-prior warm-start is active at all. Default OFF.

    Enabling this alone runs in *observe* mode (see ``cohort_prior_mode``): the
    cold-start path logs the seed an agent would get but does not mutate.
    Enable with UNITARES_COHORT_PRIOR in {1, true, on, yes}.
    """
    val = os.getenv("UNITARES_COHORT_PRIOR")
    if val is None:
        return False
    return val.strip().lower() in {"1", "true", "on", "yes"}


def cohort_prior_mode() -> str:
    """Behavior when cohort priors are enabled: 'observe' (default) or 'apply'.

    'observe' — shadow: log the seed a cold-start agent WOULD receive; do not
    mutate the baseline. This is the default even when the feature is enabled.

    'apply' — live: actually seed the fresh baseline from the class cohort prior.
    Requires the explicit second opt-in ``UNITARES_COHORT_PRIOR_MODE=apply`` so an
    operator validates calibration lift from the observe logs *before* seeding for
    real. Any other value (including unset) falls back to 'observe'. The
    anti-poisoning invariant holds in both modes: a seeded baseline still cannot
    z-score until the agent logs its own observations past the activation gate.
    """
    val = os.getenv("UNITARES_COHORT_PRIOR_MODE")
    if val is None:
        return "observe"
    return "apply" if val.strip().lower() == "apply" else "observe"


# (mean, std, total_count, n_contributors)
SignalPrior = Tuple[float, float, int, int]


class CohortPrior:
    """Per-signal pooled prior over a set of agent baselines.

    Pooled variance combines *within*-agent variance and *between*-agent mean
    spread (the standard parallel-variance / Chan combination), so the prior is
    naturally at least as wide as any single contributor — pooling never
    fabricates false confidence.
    """

    def __init__(self, priors: Dict[str, SignalPrior]):
        self._priors = priors

    @property
    def signals(self) -> Tuple[str, ...]:
        return tuple(self._priors.keys())

    def prior_for(self, signal: str) -> Optional[SignalPrior]:
        return self._priors.get(signal)

    @classmethod
    def build(
        cls,
        baselines: Iterable[AgentBehavioralBaseline],
        *,
        min_agent_count: int = DEFAULT_MIN_AGENT_COUNT,
        min_contributors: int = MIN_CONTRIBUTORS,
    ) -> "CohortPrior":
        """Pool characterized agents into a per-signal prior.

        Only an agent whose per-signal ``count >= min_agent_count`` contributes
        to that signal. A signal with fewer than ``min_contributors`` qualifying
        agents gets no prior (absent from the result) rather than a thin one.
        """
        baselines = list(baselines)
        priors: Dict[str, SignalPrior] = {}

        for signal in AgentBehavioralBaseline.TRACKED_SIGNALS:
            stats = [
                b._stats[signal]
                for b in baselines
                if signal in b._stats and b._stats[signal].count >= min_agent_count
            ]
            if len(stats) < min_contributors:
                continue

            total_count = sum(s.count for s in stats)
            if total_count < 2:
                continue

            grand_mean = sum(s.count * s.mean for s in stats) / total_count
            # Combined M2 = sum of within-agent M2 + between-agent mean spread.
            combined_m2 = sum(
                s.m2 + s.count * (s.mean - grand_mean) ** 2 for s in stats
            )
            pooled_variance = combined_m2 / (total_count - 1)
            pooled_std = pooled_variance ** 0.5
            priors[signal] = (grand_mean, pooled_std, total_count, len(stats))

        return cls(priors)

    def seed_welford(
        self,
        signal: str,
        *,
        pseudo_count: int = DEFAULT_PSEUDO_COUNT,
        widen: float = DEFAULT_WIDEN,
    ) -> Optional[WelfordStats]:
        """Build a seed WelfordStats for one signal, or None if no prior exists.

        The returned stats carry the cohort mean and a (widened) cohort std but a
        deliberately small ``count`` that MUST stay below the z-score activation
        gate — so the seed cannot make an un-observed agent start scoring.
        """
        if not (2 <= pseudo_count < Z_SCORE_ACTIVATION_COUNT):
            raise ValueError(
                f"pseudo_count must be in [2, {Z_SCORE_ACTIVATION_COUNT}) to stay "
                f"below the z-score activation gate; got {pseudo_count}. Seeding at "
                f"or above the gate would let an un-observed agent z-score against "
                f"cohort stats it has not earned (baseline poisoning)."
            )
        if widen < 1.0:
            raise ValueError(f"widen must be >= 1.0 (only widen seeded std); got {widen}")

        prior = self._priors.get(signal)
        if prior is None:
            return None
        mean, std, _total, _n = prior

        s = WelfordStats()
        s.count = pseudo_count
        s.mean = mean
        seed_variance = (std * widen) ** 2
        s.m2 = seed_variance * (pseudo_count - 1)
        return s

    def seed_baseline(
        self,
        *,
        pseudo_count: int = DEFAULT_PSEUDO_COUNT,
        widen: float = DEFAULT_WIDEN,
    ) -> AgentBehavioralBaseline:
        """Build a fresh baseline warm-started on every signal that has a prior.

        Signals without a cohort prior are left as cold Welford stats. The
        result is a shadow object; the caller decides whether to use it. Nothing
        here persists or mutates existing state.
        """
        baseline = AgentBehavioralBaseline()
        for signal in baseline.TRACKED_SIGNALS:
            seed = self.seed_welford(signal, pseudo_count=pseudo_count, widen=widen)
            if seed is not None:
                baseline._stats[signal] = seed
        return baseline
