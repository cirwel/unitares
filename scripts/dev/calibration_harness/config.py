"""Calibration-harness configuration: bins, ratios, transport, quarantine tags.

All knobs live here so run_v1 and the report stay declarative.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

# --- Stratification (the whole point of the sampler) -----------------------
# Five confidence bins. v1.1 stratifies CONFIDENCE across these; the pass/fail
# outcome is drawn in the runner from the injected curve (see miscalibration.py),
# not assigned per bin — so there is no fixed fail-ratio knob anymore.
BINS: list[tuple[float, float]] = [
    (0.0, 0.2),
    (0.2, 0.4),
    (0.4, 0.6),
    (0.6, 0.8),
    (0.8, 1.0),
]
EPISODE_COUNT: int = 200

# --- Evidence-weight gate (mirror of the server constant) ------------------
# src/outcome_corroboration.py GRADE_WEIGHTS / observability/outcome_events.py
# _MIN_TACTICAL_EVIDENCE_WEIGHT = GRADE_WEIGHTS[TOOL_OBSERVED] = 0.65.
# An outcome below this weight is NOT registered into the tactical channel.
# external_signal => grade externally_verified => weight 1.0, which clears it.
MIN_TACTICAL_EVIDENCE_WEIGHT: float = 0.65

# --- Provenance / quarantine -----------------------------------------------
# locus is a human-readable tag only; it is NOT a calibration filter and the
# server has no per-agent calibration read scope. Quarantine is by isolated
# INSTANCE (governance_test), not by tag or agent_id.
LOCUS: str = "calibration_harness"

# --- Transport -------------------------------------------------------------
DEFAULT_BASE_URL = "http://127.0.0.1:8767"


@dataclass(frozen=True)
class Transport:
    base_url: str = field(
        default_factory=lambda: os.environ.get("GOVERNANCE_HTTP_URL", DEFAULT_BASE_URL)
    )
    token: str | None = field(
        default_factory=lambda: os.environ.get("UNITARES_HTTP_API_TOKEN")
    )
    timeout_s: float = 30.0


@dataclass(frozen=True)
class ClassSpec:
    """One agent class == one dedicated, quarantined governance identity."""

    key: str           # short tag, e.g. "harness_a"
    display_name: str  # onboarded agent display name


# v1 entrypoint runs 2 classes, ~200 episodes total (100 each).
CLASSES: list[ClassSpec] = [
    ClassSpec(key="harness_a", display_name="calib-harness-a"),
    ClassSpec(key="harness_b", display_name="calib-harness-b"),
]
