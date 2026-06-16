"""Stratified confidence-bin sampler.

v1.1: the sampler only stratifies CONFIDENCE across bins. The pass/fail outcome
is NO LONGER assigned here — it is drawn in the runner from the injected
calibration curve `true_accuracy(confidence; gap)`, so outcome and confidence are
coupled by a known relationship the report can recover. (In v1 the outcome was
assigned by position here, making it independent of confidence — the flaw the
council caught.)
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import BINS, EPISODE_COUNT


@dataclass
class Slot:
    target_bin: tuple[float, float]
    index: int
    tag: str = ""


def plan(n: int = EPISODE_COUNT) -> list[Slot]:
    slots: list[Slot] = []
    per_bin = n // len(BINS)
    idx = 0
    for lo, hi in BINS:
        for _ in range(per_bin):
            slots.append(Slot((lo, hi), index=idx))
            idx += 1
    return slots
