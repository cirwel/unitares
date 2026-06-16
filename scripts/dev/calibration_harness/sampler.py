"""Stratified (confidence_bin x pass/fail) sampler — stratification is the point.

Each bin gets FAIL_RATIO injected failures so BOTH calibration error (per bin)
AND discrimination (AUC, needs bad_rate > 0) are measurable. An explicit
overconfidence cell (high bin + injected failure) is appended.
"""
from __future__ import annotations

from .config import BINS, EPISODE_COUNT, FAIL_RATIO
from .episodes import CleanControl, Episode, SeededTestFail


def plan(n: int = EPISODE_COUNT) -> list[Episode]:
    episodes: list[Episode] = []
    per_bin = n // len(BINS)
    idx = 0
    for lo, hi in BINS:
        n_fail = round(per_bin * FAIL_RATIO)
        for i in range(per_bin):
            cls = SeededTestFail if i < n_fail else CleanControl
            episodes.append(cls((lo, hi), index=idx))
            idx += 1
    # Explicit overconfidence probe: high bin + injected failure.
    hi_lo, hi_hi = BINS[-1]
    for _ in range(per_bin // 2):
        episodes.append(SeededTestFail((hi_lo, hi_hi), index=idx, tag="overconfidence_probe"))
        idx += 1
    return episodes
