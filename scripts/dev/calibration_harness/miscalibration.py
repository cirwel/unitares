"""Injectable miscalibration model — the v1.1 fix (council architect #1).

v1's outcomes were independent of confidence, so ECE/AUC had no ground truth to
recover. Here the outcome is drawn from a KNOWN calibration curve, so the report
can check "I injected error X — did the channel recover ≈ X?". That is the actual
measurement-plumbing test, distinct from the binding smoke test.

Model: an overconfident agent. Its TRUE success probability at stated confidence
`c` is `c - gap` (clamped to [0,1]). With gap > 0 the agent claims more than it
delivers, so a correct ECE estimate should recover ≈ gap (over the region where
the clamp is inactive), and AUC should exceed 0.5 because success now rises
monotonically with confidence.
"""
from __future__ import annotations

import math


def true_accuracy(confidence: float, gap: float) -> float:
    """Injected calibration curve: P(success | confidence) = clamp(confidence - gap)."""
    return min(1.0, max(0.0, confidence - gap))


def injected_ece(confidences: list[float], gap: float) -> float:
    """Bias-free target: per-sample |confidence - true_accuracy(confidence)|, averaged.

    The miscalibration we actually injected, independent of binning or sampling.
    """
    if not confidences:
        return 0.0
    return sum(abs(c - true_accuracy(c, gap)) for c in confidences) / len(confidences)


def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _folded_normal_mean(mu: float, sigma: float) -> float:
    """E|X| for X ~ N(mu, sigma^2). The per-bin |mean_conf - empirical_acc|."""
    if sigma <= 0:
        return abs(mu)
    return sigma * math.sqrt(2.0 / math.pi) * math.exp(-(mu**2) / (2 * sigma**2)) + mu * (1 - 2 * _phi(-mu / sigma))


def expected_recovered_ece(confidences: list[float], gap: float, bins: list[tuple[float, float]]) -> float:
    """Bias-AWARE target: what a binned ECE estimator should report at finite n.

    Binned empirical accuracy is a noisy estimate of mean true accuracy, and ECE
    sums |mean_conf - accuracy|, so each bin contributes a folded-normal mean that
    is positive even at perfect calibration. This models that floor, so the
    recovered ECE can be checked against a principled target at ANY gap (including
    gap=0, where injected_ece=0 but a finite-sample estimator cannot report 0).
    """
    total = len(confidences)
    if not total:
        return 0.0
    out = 0.0
    for lo, hi in bins:
        members = [c for c in confidences if (lo <= c < hi) or (hi == 1.0 and c == 1.0)]
        if not members:
            continue
        n_b = len(members)
        ps = [true_accuracy(c, gap) for c in members]
        mean_conf = sum(members) / n_b
        mean_acc = sum(ps) / n_b
        var_acc = sum(p * (1 - p) for p in ps) / (n_b * n_b)  # var of the bin mean
        out += (n_b / total) * _folded_normal_mean(mean_conf - mean_acc, math.sqrt(var_acc))
    return out
