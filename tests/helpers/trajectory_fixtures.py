"""Synthetic trajectory pair generators for R1 score_trajectory_continuity tests.

Per docs/ontology/r1-verify-lineage-claim.md §"Test fixture (synthetic)". Each
generator produces deterministic per-dimension EISV series for a parent and
successor pair given a seed. The score primitive's threshold cuts are
*regression-tested* against these fixtures (not calibrated against them — see
the design doc §"Plausibility → verdict thresholds" for the seeded-vs-earned
distinction).

Generators:
- genuine: parent stable, successor continues same dynamics
- divergent: parent stable, successor independent random walk
- drifted: parent stable, successor matched then diverges
- early: parent mature, successor too few rows to score
- immature: parent has too few rows
- dimensional_degradation: parent all-dim, successor only E

Output shape: `(parent_series, successor_series)` where each is
`Dict[str, List[float]]` with keys E, I, S, V — matches the return shape of
`StateMixin.reconstruct_eisv_series`.
"""

from __future__ import annotations

import math
import random
from typing import Dict, List, Tuple


_HIGH_BASIN_SEED = {
    "E": 0.70, "I": 0.80, "S": 0.20, "V": 0.05,
}
_LOW_BASIN_SEED = {           # opposite basin for divergent walks
    "E": 0.15, "I": 0.20, "S": 0.75, "V": -0.45,
}
_DRIFT_TARGET = {             # mid-distance drift target for drifted walks
    "E": 0.25, "I": 0.20, "S": 0.70, "V": 0.55,
}
_DRIFT_START_OFFSET = 0.40    # successor starts already partly drifted —
                              # 0.40 of the way to target — produces an
                              # inconclusive-band score (0.55-0.70) rather
                              # than a still-matched plausible score
_DRIFT_AMPLITUDE = 0.05       # small noise around stable-basin series
_DIVERGENT_AMPLITUDE = 0.10   # small noise around divergent low-basin series
                              # (large noise blurs the basin distinction)


def synthetic_trajectory_pair(
    seed: int,
    kind: str,
) -> Tuple[Dict[str, List[float]], Dict[str, List[float]]]:
    """Generate a (parent_series, successor_series) pair for the named kind.

    Deterministic given seed + kind. Series counts:
    - genuine, divergent, drifted: parent=30, successor=10
    - early: parent=30, successor=3
    - immature: parent=4, successor=10
    - dimensional_degradation: parent=30 (all dims), successor=10 (E only)

    All values are clamped to [0, 1] for E/I/S and [-1, 1] for V (matches the
    paper's EISV bounds; see governance_fundamentals).
    """
    if kind == "genuine":
        return _genuine(seed)
    if kind == "divergent":
        return _divergent(seed)
    if kind == "drifted":
        return _drifted(seed)
    if kind == "early":
        return _early(seed)
    if kind == "immature":
        return _immature(seed)
    if kind == "dimensional_degradation":
        return _dimensional_degradation(seed)
    raise ValueError(f"unknown kind: {kind}")


def _stable_walk(rng: random.Random, n: int) -> Dict[str, List[float]]:
    """Generate n samples around the high-basin seed with small noise."""
    out: Dict[str, List[float]] = {"E": [], "I": [], "S": [], "V": []}
    for _ in range(n):
        for dim, center in _HIGH_BASIN_SEED.items():
            v = center + rng.uniform(-_DRIFT_AMPLITUDE, _DRIFT_AMPLITUDE)
            out[dim].append(_clamp(dim, v))
    return out


def _independent_walk(rng: random.Random, n: int) -> Dict[str, List[float]]:
    """Generate n samples in the *opposite* basin from the high-basin parent.

    A truly divergent successor is one whose state vector lives in a different
    region of EISV space, not just one with random noise. Centering on
    `_LOW_BASIN_SEED` produces trajectories that are far from the parent's
    high-basin trajectory in DTW distance.
    """
    out: Dict[str, List[float]] = {"E": [], "I": [], "S": [], "V": []}
    for _ in range(n):
        for dim, center in _LOW_BASIN_SEED.items():
            v = center + rng.uniform(-_DIVERGENT_AMPLITUDE, _DIVERGENT_AMPLITUDE)
            out[dim].append(_clamp(dim, v))
    return out


def _drifting_walk(rng: random.Random, n: int) -> Dict[str, List[float]]:
    """Start partly drifted from high-basin, drift further toward _DRIFT_TARGET.

    Starting already offset (rather than starting matched and drifting) keeps
    DTW from finding tight alignment with the parent's stable basin, which
    pushes the score into the inconclusive band (0.55-0.70) rather than
    leaving it in the plausible band.
    """
    out: Dict[str, List[float]] = {"E": [], "I": [], "S": [], "V": []}
    for i in range(n):
        t = _DRIFT_START_OFFSET + (1 - _DRIFT_START_OFFSET) * (i / max(n - 1, 1))
        for dim, start in _HIGH_BASIN_SEED.items():
            target = _DRIFT_TARGET[dim]
            interp = start + t * (target - start)
            v = interp + rng.uniform(-_DRIFT_AMPLITUDE / 2, _DRIFT_AMPLITUDE / 2)
            out[dim].append(_clamp(dim, v))
    return out


def _genuine(seed: int):
    rng = random.Random(seed)
    parent = _stable_walk(rng, 30)
    successor = _stable_walk(rng, 10)
    return parent, successor


def _divergent(seed: int):
    rng = random.Random(seed)
    parent = _stable_walk(rng, 30)
    successor = _independent_walk(random.Random(seed + 1), 10)
    return parent, successor


def _drifted(seed: int):
    rng = random.Random(seed)
    parent = _stable_walk(rng, 30)
    successor = _drifting_walk(random.Random(seed + 2), 10)
    return parent, successor


def _early(seed: int):
    rng = random.Random(seed)
    parent = _stable_walk(rng, 30)
    successor = _stable_walk(rng, 3)  # < min_observations
    return parent, successor


def _immature(seed: int):
    rng = random.Random(seed)
    parent = _stable_walk(rng, 4)  # < min_observations on parent side
    successor = _stable_walk(rng, 10)
    return parent, successor


def _dimensional_degradation(seed: int):
    rng = random.Random(seed)
    parent = _stable_walk(rng, 30)
    successor_full = _stable_walk(rng, 10)
    # Successor has E dimension only; others are empty (no rows)
    successor: Dict[str, List[float]] = {
        "E": successor_full["E"],
        "I": [],
        "S": [],
        "V": [],
    }
    return parent, successor


def _clamp(dim: str, v: float) -> float:
    if dim == "V":
        return max(-1.0, min(1.0, v))
    return max(0.0, min(1.0, v))
