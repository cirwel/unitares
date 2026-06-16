"""Per-slot loop: draw confidence -> draw outcome from the injected curve ->
check-in -> grade the matching subprocess -> outcome event.

The outcome is decided by `Bernoulli(true_accuracy(confidence; gap))`, then a real
pass/fail subprocess is run to realize it — so confidence and outcome are coupled
by the known curve while the graded signal stays exogenous (a real exit code,
external_signal). Binding is via prediction_id.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from .client import GovernanceClient, Identity
from .episodes import CleanControl, SeededTestFail, elicit_confidence
from .grader import grade_script
from .miscalibration import true_accuracy
from .sampler import Slot


@dataclass
class RunRow:
    label: str
    target_lo: float
    target_hi: float
    stated_confidence: float
    injected_p_success: float
    is_bad: bool
    exit_code: int
    prediction_id: str
    agent_uuid: str
    evidence_weight: float | None


def run_slot(client: GovernanceClient, ident: Identity, slot: Slot, rng: random.Random, gap: float) -> RunRow:
    conf = elicit_confidence(slot.target_bin, rng)
    p_success = true_accuracy(conf, gap)
    should_pass = rng.random() < p_success
    label = f"[{slot.target_bin[0]:.1f}-{slot.target_bin[1]:.1f}]#{slot.index}{':'+slot.tag if slot.tag else ''}"

    pred_id = client.check_in(
        ident,
        confidence=conf,
        response_text=f"{label} confidence={conf:.3f} injected_p={p_success:.3f} -> {'pass' if should_pass else 'fail'}",
        task_label=label,
    )

    # Realize the drawn outcome with a real subprocess (exogenous exit code).
    src = (CleanControl if should_pass else SeededTestFail)(slot.target_bin, index=slot.index).build_source()
    grade = grade_script(src, label=label)
    if grade.is_bad == should_pass:  # pass-source must pass, fail-source must fail
        raise RuntimeError(f"{label}: subprocess outcome ({not grade.is_bad}) != drawn ({should_pass})")

    out = client.record_outcome(
        ident,
        prediction_id=pred_id,
        is_bad=grade.is_bad,
        outcome_score=grade.score,
        detail=grade.detail,
    )
    ew = out.get("evidence_weight")
    return RunRow(
        label=label,
        target_lo=slot.target_bin[0],
        target_hi=slot.target_bin[1],
        stated_confidence=conf,
        injected_p_success=round(p_success, 4),
        is_bad=grade.is_bad,
        exit_code=grade.exit_code,
        prediction_id=pred_id,
        agent_uuid=ident.agent_uuid,
        evidence_weight=float(ew) if ew is not None else None,
    )
