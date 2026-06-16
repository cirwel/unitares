"""Episode ABC and confidence elicitation.

An Episode knows its ground truth *by construction*: it emits a python script
whose exit code is deterministic, so the grader's verdict is decided before the
agent ever "attempts" it. That is what makes the harness a calibration fixture
rather than a behavioral measurement.
"""
from __future__ import annotations

import random
from abc import ABC, abstractmethod


class Episode(ABC):
    """One bounded, ground-truth-known task."""

    def __init__(self, target_bin: tuple[float, float], index: int, tag: str = "") -> None:
        self.target_bin = target_bin
        self.index = index
        self.tag = tag

    @property
    def label(self) -> str:
        lo, hi = self.target_bin
        suffix = f":{self.tag}" if self.tag else ""
        return f"{self.kind}[{lo:.1f}-{hi:.1f}]#{self.index}{suffix}"

    @property
    @abstractmethod
    def kind(self) -> str:
        """Short stable kind name, e.g. 'clean_control'."""

    @property
    @abstractmethod
    def expected_bad(self) -> bool:
        """Ground-truth outcome: True if this episode is constructed to fail."""

    @abstractmethod
    def build_source(self) -> str:
        """Return deterministic python source whose exit code encodes the truth."""


def elicit_confidence(target_bin: tuple[float, float], rng: random.Random) -> float:
    """Draw a stated confidence inside the target bin (synthetic fixture).

    NOTE: process_agent_update registers a *transformed* confidence, not this
    raw value (observed: 0.90 -> 0.9148). The harness submits this to land
    roughly in-bin; report.py reads the registered ``reported_confidence`` back
    from the DB for the actual ECE/AUC computation rather than trusting this.
    """
    lo, hi = target_bin
    # keep a hair inside the edges so rounding doesn't cross a boundary
    span = hi - lo
    return round(lo + 0.1 * span + rng.random() * 0.8 * span, 4)
