"""Behavioral EISV: observation-first agent state without ODE dynamics.

EMA-smoothed observations of agent behavior. No universal attractor, no
contraction — each agent's state reflects its actual observables.

After a warmup phase (~30 updates), per-agent behavioral baselines are
established using Welford's algorithm. Assessment then uses z-score deviation
from the agent's own characteristic operating point instead of fixed thresholds.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from src.agent_behavioral_baseline import WelfordStats


# Per-dimension EMA alphas.
# At 30s cadence: half-life = -30 / ln(1 - alpha)
#   E: alpha=0.12 → ~220s half-life (capacity changes slowly)
#   I: alpha=0.08 → ~350s half-life (integrity is conservative)
#   S: alpha=0.15 → ~175s half-life (entropy responds faster)
#   V: alpha=0.10 → ~270s half-life (imbalance is medium-term)
DEFAULT_ALPHAS = {"E": 0.12, "I": 0.08, "S": 0.15, "V": 0.10}

# Bootstrap defaults — neutral starting point
BOOTSTRAP_E = 0.5
BOOTSTRAP_I = 0.5
BOOTSTRAP_S = 0.2
BOOTSTRAP_V = 0.0

# History cap
MAX_HISTORY = 100

# Number of updates before full confidence in behavioral state
BOOTSTRAP_UPDATES = 10

# Number of updates before behavioral baseline is considered stable
# (~15 min at 30s cadence). Welford needs >=5 for z_score, but
# 30 gives stable mean/std estimates.
BASELINE_WARMUP_UPDATES = 30

# Behavioral-V formula version. Bumped when the V (Valence) computation changes
# in a way that invalidates a previously-converged _baseline_V, so loads from an
# older version trigger a one-time baseline re-seed (see _reseed_v_baseline).
#   v1: double-smoothed — raw_v = self.E - self.I (gap of the already-EMA'd E,I)
#   v2: single-EMA of raw imbalance — raw_v = E_obs - I_obs (one fewer smoothing
#       stage; less lag). Gated on validate_valence_formula.py against real
#       check-in traces (2026-06-19: 0 verdict flips / 0 healthy regressions on
#       18 real agents incl. all 5 residents; resident Watcher would spike 3.12σ
#       > 3.0σ budget against its stale baseline WITHOUT the reset below).
V_FORMULA_VERSION = 2

# Minimum meaningful standard deviation for EISV self-relative scoring.
#
# This is an EMPIRICAL constant calibrated against the 2026-06-13 Sentinel
# false-pause trace — NOT a value derived from EISV [0,1] semantics. Note the
# baseline std is computed over EMA-SMOOTHED E/I/S/V (see _baseline updates
# below), so a steady agent's std collapses toward ~0.01 partly because the
# EMA has already eaten the raw observation noise. Without a floor, that
# artifact makes z = Δ/σ explode: an ultra-stable monitor turns a small,
# absolutely-healthy fluctuation into a many-sigma "severe deviation" and gets
# falsely flagged high-risk → cirs_block → paused. The floor caps that
# sensitivity while leaving genuine multi-tenth moves and the absolute safety
# floors (E/I<0.30, S>0.70, |V|>0.50) untouched. Empirically: the Sentinel
# pause (E 0.77→0.66, I 0.68→0.66 — both healthy) scored risk 0.94 (high-risk)
# with no floor vs 0.33 (safe) at 0.05; genuine degradations are unchanged.
#
# As of issue #689 this flat floor is SECONDARY. The principled fix — gating
# self-relative deviation risk by absolute basin health — lives in
# behavioral_assessment._basin_health_gate: inside the healthy basin the
# self-relative components are multiplied by 0, so a tight-σ agent's small,
# absolutely-healthy wobble raises no risk regardless of how many σ it spans.
# That gate, not this constant, is what now keeps the Sentinel trace safe.
#
# The floor is retained as defense-in-depth: it bounds the raw z-magnitude in
# the boundary region (where the gate is partially open) so a collapsed σ cannot
# produce an absurd z there. It does NOT touch σ for unstable agents (it only
# binds when std < the floor), so it never blunts meaningful variance. Default
# alphas preserve the original 0.05 guard exactly; tuned per-agent alphas scale
# the guard proportionally so the secondary floor follows the EMA smoothing step
# restored from persistence.
MIN_MEANINGFUL_EISV_STD = 0.05


def eisv_min_std_for_dimension(dimension: str, alphas: Optional[Dict[str, float]] = None) -> float:
    """Minimum z-score denominator scaled by the dimension's EMA alpha.

    Default alphas preserve the #686/#696 floor exactly. If an agent tunes a
    dimension to a slower or faster EMA, scale the secondary floor with that
    persisted alpha so the boundary-region guard follows the smoothing step
    instead of silently falling back to the fleet defaults.
    """
    default_alpha = DEFAULT_ALPHAS.get(dimension)
    if not default_alpha:
        return MIN_MEANINGFUL_EISV_STD
    alpha = (alphas or DEFAULT_ALPHAS).get(dimension, default_alpha)
    if alpha <= 0:
        alpha = default_alpha
    return MIN_MEANINGFUL_EISV_STD * (alpha / default_alpha)


@dataclass
class BehavioralEISV:
    """EMA-smoothed behavioral EISV state.

    No ODE. No attractor. Just observations smoothed over time.
    V is the EMA-smoothed E-I imbalance — a derived, sign-actionable readout
    (positive = running hot, negative = running careful), NOT a true integral
    (that is governance_core's separate ODE void integral).

    After BASELINE_WARMUP_UPDATES, behavioral baselines (Welford mean/std per
    dimension) enable self-relative assessment — deviation from YOUR pattern,
    not universal thresholds.
    """

    E: float = BOOTSTRAP_E
    I: float = BOOTSTRAP_I
    S: float = BOOTSTRAP_S
    V: float = BOOTSTRAP_V

    update_count: int = 0
    last_update_time: Optional[float] = None  # monotonic seconds

    # Per-dimension EMA alphas (can be tuned per agent)
    alphas: Dict[str, float] = field(default_factory=lambda: dict(DEFAULT_ALPHAS))

    # History for trend detection
    E_history: List[float] = field(default_factory=list)
    I_history: List[float] = field(default_factory=list)
    S_history: List[float] = field(default_factory=list)
    V_history: List[float] = field(default_factory=list)

    # Raw observation history (pre-EMA) for dimensionality analysis
    obs_history: List[List[float]] = field(default_factory=list)

    # Per-dimension running statistics for behavioral baseline (Welford's algorithm)
    _baseline_E: WelfordStats = field(default_factory=WelfordStats)
    _baseline_I: WelfordStats = field(default_factory=WelfordStats)
    _baseline_S: WelfordStats = field(default_factory=WelfordStats)
    _baseline_V: WelfordStats = field(default_factory=WelfordStats)

    def _raw_valence(self, E_obs: float, I_obs: float) -> float:
        """Pre-EMA valence (E-I imbalance) input fed into V's own EMA.

        V_FORMULA_VERSION 2: the gap of the RAW observations (E_obs - I_obs),
        smoothed once by V's own EMA — so V is single-smoothed and tracks the
        imbalance with less lag. The previous v1 default took the gap of the
        already-EMA'd E,I (double-smoothing). The flip was gated on a trace
        replay against real check-in traces via
        scripts/dev/validate_valence_formula.py (2026-06-19): verdict-neutral
        (0 flips, 0 healthy regressions on 18 real agents incl. all 5
        residents); the one-time migration discontinuity it creates for already-
        converged agents is absorbed by _reseed_v_baseline on load. Kept as an
        override seam so the harness can still A/B v1 (return self.E - self.I)
        against v2 through the real update + assessment without forking.
        """
        return E_obs - I_obs

    def update(
        self,
        E_obs: float,
        I_obs: float,
        S_obs: float,
    ) -> None:
        """Update behavioral state from observations.

        Args:
            E_obs: Observed energy [0, 1] — from tool success, decision quality
            I_obs: Observed integrity [0, 1] — from calibration accuracy, coherence
            S_obs: Observed entropy [0, 1] — from drift, instability, divergence
        """
        # Clamp inputs
        E_obs = max(0.0, min(1.0, E_obs))
        I_obs = max(0.0, min(1.0, I_obs))
        S_obs = max(0.0, min(1.0, S_obs))

        # Record raw observations before EMA smoothing
        self.obs_history.append([E_obs, I_obs, S_obs])
        if len(self.obs_history) > MAX_HISTORY:
            self.obs_history = self.obs_history[-MAX_HISTORY:]

        # During bootstrap, ramp alpha from 0.5 (fast catch-up) down to configured value
        if self.update_count < BOOTSTRAP_UPDATES:
            ramp = 1.0 - (self.update_count / BOOTSTRAP_UPDATES)
            bootstrap_boost = 0.5 - 0.0  # max extra alpha during bootstrap
            alpha_E = self.alphas["E"] + bootstrap_boost * ramp
            alpha_I = self.alphas["I"] + bootstrap_boost * ramp
            alpha_S = self.alphas["S"] + bootstrap_boost * ramp
            alpha_V = self.alphas["V"] + bootstrap_boost * ramp
        else:
            alpha_E = self.alphas["E"]
            alpha_I = self.alphas["I"]
            alpha_S = self.alphas["S"]
            alpha_V = self.alphas["V"]

        # EMA update: new = (1 - alpha) * old + alpha * observation
        self.E = (1.0 - alpha_E) * self.E + alpha_E * E_obs
        self.I = (1.0 - alpha_I) * self.I + alpha_I * I_obs
        self.S = (1.0 - alpha_S) * self.S + alpha_S * S_obs

        # V: EMA-smoothed E-I imbalance — a derived, sign-actionable readout,
        # not a true integral. As of V_FORMULA_VERSION 2 this is the gap of the
        # RAW observations smoothed once (single-EMA, less lag), replacing the
        # earlier double-smoothing. The formula lives behind _raw_valence; see
        # scripts/dev/validate_valence_formula.py for the trace-replay gate.
        raw_v = self._raw_valence(E_obs, I_obs)
        self.V = (1.0 - alpha_V) * self.V + alpha_V * raw_v

        # Clamp to valid ranges
        self.E = max(0.0, min(1.0, self.E))
        self.I = max(0.0, min(1.0, self.I))
        self.S = max(0.0, min(1.0, self.S))
        self.V = max(-1.0, min(1.0, self.V))

        # Record history
        self.E_history.append(self.E)
        self.I_history.append(self.I)
        self.S_history.append(self.S)
        self.V_history.append(self.V)

        # Trim history
        if len(self.E_history) > MAX_HISTORY:
            self.E_history = self.E_history[-MAX_HISTORY:]
            self.I_history = self.I_history[-MAX_HISTORY:]
            self.S_history = self.S_history[-MAX_HISTORY:]
            self.V_history = self.V_history[-MAX_HISTORY:]

        # Feed smoothed values to baseline stats
        self._baseline_E.update(self.E)
        self._baseline_I.update(self.I)
        self._baseline_S.update(self.S)
        self._baseline_V.update(self.V)

        self.update_count += 1
        self.last_update_time = time.monotonic()

    def _reseed_v_baseline(self) -> None:
        """One-time re-seed of _baseline_V after a V-formula migration.

        A baseline converged under an older V formula (v1 double-smoothing) is
        stale once _raw_valence changes: the new V gets z-scored against the old
        mean/std and spikes (real resident Watcher: 3.12σ > the 3.0σ migration
        budget — validate_valence_formula.py 2026-06-19). Re-seed so the first
        post-migration z is ~0:
          * obs_history present -> replay it under the CURRENT formula and
            rebuild _baseline_V from the reconstructed V trajectory (accurate
            mean AND std), and align the live V with that trajectory.
          * obs_history absent (the lean DB-row restore drops history) -> seed a
            single sample at the current V so mean==V (z~0); std rebuilds as
            updates arrive. The deviation() min-std floor bounds it meanwhile.
        """
        fresh = WelfordStats()
        if self.obs_history:
            v = BOOTSTRAP_V
            replayed: List[float] = []
            for idx, row in enumerate(self.obs_history):
                e_obs = max(0.0, min(1.0, float(row[0])))
                i_obs = max(0.0, min(1.0, float(row[1])))
                if idx < BOOTSTRAP_UPDATES:
                    alpha_v = self.alphas["V"] + 0.5 * (1.0 - idx / BOOTSTRAP_UPDATES)
                else:
                    alpha_v = self.alphas["V"]
                raw_v = e_obs - i_obs  # v2 formula (reseed only runs post-flip)
                v = max(-1.0, min(1.0, (1.0 - alpha_v) * v + alpha_v * raw_v))
                replayed.append(v)
            # Drop the cold-start bootstrap transient from the baseline when the
            # history is long enough to spare it; the live V still warms through
            # those steps. Keep all samples for short histories.
            seed = replayed[BOOTSTRAP_UPDATES:] if len(replayed) > BOOTSTRAP_UPDATES + 5 else replayed
            for x in seed:
                fresh.update(x)
            self.V = replayed[-1]
        else:
            fresh.update(self.V)
        self._baseline_V = fresh

    @property
    def confidence(self) -> float:
        """Confidence in behavioral state — ramps from 0 to 1 over bootstrap period."""
        if self.update_count >= BOOTSTRAP_UPDATES:
            return 1.0
        return self.update_count / BOOTSTRAP_UPDATES

    @property
    def baseline_confidence(self) -> float:
        """How stable is the behavioral baseline. 0 = no data, 1 = fully characterized."""
        if self.update_count >= BASELINE_WARMUP_UPDATES:
            return 1.0
        if self.update_count < 5:
            return 0.0
        return (self.update_count - 5) / (BASELINE_WARMUP_UPDATES - 5)

    @property
    def is_baselined(self) -> bool:
        """True when behavioral baseline is stable enough for self-relative scoring."""
        return self.baseline_confidence >= 0.8

    @property
    def baseline_profile(self) -> Dict[str, Dict]:
        """The agent's characteristic EISV operating point.

        Returns mean/std/count per dimension. Empty dict if not yet baselined.
        """
        if not self.is_baselined:
            return {}
        return {
            "E": {"mean": round(self._baseline_E.mean, 4), "std": round(self._baseline_E.std, 4), "count": self._baseline_E.count},
            "I": {"mean": round(self._baseline_I.mean, 4), "std": round(self._baseline_I.std, 4), "count": self._baseline_I.count},
            "S": {"mean": round(self._baseline_S.mean, 4), "std": round(self._baseline_S.std, 4), "count": self._baseline_S.count},
            "V": {"mean": round(self._baseline_V.mean, 4), "std": round(self._baseline_V.std, 4), "count": self._baseline_V.count},
        }

    def deviation(self, dimension: str) -> float:
        """Z-score of current value from agent's own behavioral baseline.

        Returns 0.0 if warmup is incomplete or std is too small.
        Positive = above baseline, negative = below baseline.
        """
        stats = getattr(self, f"_baseline_{dimension}", None)
        if stats is None or not self.is_baselined:
            return 0.0
        current = getattr(self, dimension, 0.5)
        return stats.z_score(current, min_std=eisv_min_std_for_dimension(dimension, self.alphas))

    def trend(self, dimension: str, window: int = 5) -> float:
        """Simple slope of recent history for a dimension.

        Returns positive for improving, negative for declining.
        """
        history = getattr(self, f"{dimension}_history", [])
        if len(history) < 2:
            return 0.0
        recent = history[-window:]
        if len(recent) < 2:
            return 0.0
        n = len(recent)
        x_mean = (n - 1) / 2.0
        y_mean = sum(recent) / n
        num = sum((i - x_mean) * (v - y_mean) for i, v in enumerate(recent))
        den = sum((i - x_mean) ** 2 for i in range(n))
        if den == 0:
            return 0.0
        return num / den

    def to_dict(self) -> Dict:
        """Export current state for inclusion in governance responses."""
        if self.update_count == 0:
            phase = "uninitialized"
        elif self.update_count < BOOTSTRAP_UPDATES:
            phase = "bootstrapping"
        elif self.update_count < BASELINE_WARMUP_UPDATES:
            phase = "warming_up"
        else:
            phase = "baselined"

        d = {
            "E": round(self.E, 4),
            "I": round(self.I, 4),
            "S": round(self.S, 4),
            "V": round(self.V, 4),
            "confidence": round(self.confidence, 2),
            "updates": self.update_count,
            "warmup": {
                "phase": phase,
                "updates_completed": self.update_count,
                "baseline_target": BASELINE_WARMUP_UPDATES,
                "baseline_confidence": round(self.baseline_confidence, 2),
                "is_baselined": self.is_baselined,
            },
        }
        if self.is_baselined:
            d["baseline_profile"] = self.baseline_profile
            d["baseline_confidence"] = round(self.baseline_confidence, 2)
        return d

    def to_dict_for_persistence(self) -> Dict:
        """Lean snapshot for the append-only DB path (core.agent_state.state_json).

        Carries everything needed to restore baseline maturity across a restart —
        EMA scalars, alphas, update_count, and the Welford baseline_stats — but
        OMITS the up-to-100-entry E/I/S/V/obs history arrays. The full
        ``to_dict_with_history`` (~5KB) is fine for the JSON file (one overwritten
        file per agent) but would bloat the DB, which appends a row per check-in.
        Histories rebuild within a few updates after restore; baseline_stats and
        update_count are what gate ``is_baselined`` and drive z-scoring.
        """
        d = self.to_dict()
        d["alphas"] = dict(self.alphas)
        d["v_formula_version"] = V_FORMULA_VERSION
        d["baseline_stats"] = {
            "E": self._baseline_E.to_dict(),
            "I": self._baseline_I.to_dict(),
            "S": self._baseline_S.to_dict(),
            "V": self._baseline_V.to_dict(),
        }
        # This check-in's RAW (pre-EMA) observation [E_obs, I_obs, S_obs]. Just
        # the latest triple — NOT the full obs_history — so the append-only DB
        # row carries the un-smoothed input at ~3 floats/row (negligible, unlike
        # the ~5KB full history). Across successive rows this reconstructs the raw
        # per-agent series, which the smoothed E/I/S/V cannot: it is the input the
        # honest persistence/AR(1) self-predictability test needs (the
        # individuality axiom cannot be earned against a pre-smoothed signal — see
        # scripts/analysis/eisv_self_predictability.py scope limit). Absent before
        # the first update() (no observation recorded yet).
        if self.obs_history:
            d["raw_obs"] = [round(v, 4) for v in self.obs_history[-1]]
        return d

    def to_dict_with_history(self) -> Dict:
        """Export state with history for persistence."""
        d = self.to_dict()
        d["E_history"] = [round(v, 4) for v in self.E_history[-MAX_HISTORY:]]
        d["I_history"] = [round(v, 4) for v in self.I_history[-MAX_HISTORY:]]
        d["S_history"] = [round(v, 4) for v in self.S_history[-MAX_HISTORY:]]
        d["V_history"] = [round(v, 4) for v in self.V_history[-MAX_HISTORY:]]
        d["obs_history"] = [[round(v, 4) for v in row] for row in self.obs_history[-MAX_HISTORY:]]
        d["alphas"] = dict(self.alphas)
        d["v_formula_version"] = V_FORMULA_VERSION
        # Persist baseline statistics for cross-restart continuity
        d["baseline_stats"] = {
            "E": self._baseline_E.to_dict(),
            "I": self._baseline_I.to_dict(),
            "S": self._baseline_S.to_dict(),
            "V": self._baseline_V.to_dict(),
        }
        return d

    @classmethod
    def from_dict(cls, data: Dict) -> BehavioralEISV:
        """Restore from persisted dict."""
        state = cls()
        state.E = float(data.get("E", BOOTSTRAP_E))
        state.I = float(data.get("I", BOOTSTRAP_I))
        state.S = float(data.get("S", BOOTSTRAP_S))
        state.V = float(data.get("V", BOOTSTRAP_V))
        state.update_count = int(data.get("updates", 0))
        state.E_history = [float(v) for v in data.get("E_history", [])]
        state.I_history = [float(v) for v in data.get("I_history", [])]
        state.S_history = [float(v) for v in data.get("S_history", [])]
        state.V_history = [float(v) for v in data.get("V_history", [])]
        state.obs_history = [[float(v) for v in row] for row in data.get("obs_history", [])]
        if "alphas" in data:
            state.alphas = {k: float(v) for k, v in data["alphas"].items()}
        # Restore baseline statistics (backward compat: missing = fresh WelfordStats)
        # Also accept legacy "dna_stats" key for data persisted before rename
        baseline_data = data.get("baseline_stats", data.get("dna_stats", {}))
        if "E" in baseline_data:
            state._baseline_E = WelfordStats.from_dict(baseline_data["E"])
        if "I" in baseline_data:
            state._baseline_I = WelfordStats.from_dict(baseline_data["I"])
        if "S" in baseline_data:
            state._baseline_S = WelfordStats.from_dict(baseline_data["S"])
        if "V" in baseline_data:
            state._baseline_V = WelfordStats.from_dict(baseline_data["V"])
        # One-time V-baseline migration: a state persisted under an older V
        # formula carries a _baseline_V that no longer matches how V is now
        # computed. Re-seed it once so the new-formula V is not judged against a
        # stale baseline (would spike — see _reseed_v_baseline). Only when the
        # baseline is already mature; an immature one rebuilds correctly anyway.
        persisted_version = int(data.get("v_formula_version", 1))
        if persisted_version < V_FORMULA_VERSION and state.is_baselined:
            state._reseed_v_baseline()
        return state
