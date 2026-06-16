"""Per-episode loop: check-in -> attempt (sandboxed) -> grade -> outcome.

Binding is the whole point: the prediction_id from check-in is threaded into
the outcome so the registered confidence (not a temporal proxy) is what scores.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from .client import GovernanceClient, Identity
from .episodes import Episode, elicit_confidence
from .grader import grade_script


@dataclass
class RunRow:
    label: str
    kind: str
    tag: str
    target_lo: float
    target_hi: float
    stated_confidence: float
    is_bad: bool
    exit_code: int
    prediction_id: str
    agent_uuid: str
    evidence_weight: float | None


def run_episode(client: GovernanceClient, ident: Identity, ep: Episode, rng: random.Random) -> RunRow:
    conf = elicit_confidence(ep.target_bin, rng)
    pred_id = client.check_in(
        ident,
        confidence=conf,
        response_text=f"[{ep.label}] attempting bounded task; constructed to {'fail' if ep.expected_bad else 'pass'}",
        task_label=ep.label,
    )

    grade = grade_script(ep.build_source(), label=ep.label)
    # Sanity: the grader must agree with the construction, else the fixture lies.
    if grade.is_bad != ep.expected_bad:
        raise RuntimeError(
            f"{ep.label}: grader/construction mismatch (is_bad={grade.is_bad}, expected={ep.expected_bad})"
        )

    out = client.record_outcome(
        ident,
        prediction_id=pred_id,
        is_bad=grade.is_bad,
        outcome_score=grade.score,
        detail=grade.detail,
    )
    ew = out.get("evidence_weight")
    return RunRow(
        label=ep.label,
        kind=ep.kind,
        tag=ep.tag,
        target_lo=ep.target_bin[0],
        target_hi=ep.target_bin[1],
        stated_confidence=conf,
        is_bad=grade.is_bad,
        exit_code=grade.exit_code,
        prediction_id=pred_id,
        agent_uuid=ident.agent_uuid,
        evidence_weight=float(ew) if ew is not None else None,
    )
