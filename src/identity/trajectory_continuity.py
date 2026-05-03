"""R1 score_trajectory_continuity — single-channel per-dimension DTW similarity
plausibility score for declared lineage claims.

Per docs/ontology/r1-verify-lineage-claim.md v3.1 + v3.3.

A fresh process-instance declaring `parent_agent_id=<uuid>` is making a *claim*,
not a fact. This primitive scores how well the successor's observed EISV
trajectory matches the parent's, giving a plausibility in [0, 1].

Single-channel: per-dimension DTW similarity over EISV trajectories
reconstructed server-side from `core.agent_state` rows. No weights. No
composition. Four per-dimension similarities, averaged over those that have
data on both sides.

Non-goals (explicit per spec):
- Not authentication (that remains bearer-token + process-fingerprint)
- Not a security primitive (an adversary with KG read access can forge a
  passing trajectory; this primitive detects honest over-claims)
- Not an identity issuer (output is a plausibility score; policy decides what
  to do with it)

Side effects:
- Awaited write to `audit.r1_score_audit` (full record) — score_id is the
  join key into the redacted public KG payload (v3.3-A)
- Public KG emission is via `_build_public_payload` here; the actual KG write
  is wired in PR 3 alongside consumer patches

Calibration status: every score record stamps `calibration_status='seeded'` by
default until operator transitions the lifecycle to `earned` or
`calibration_failed` (v3.3-C). Consumers under `calibration_failed` MUST
degrade verdict to `inconclusive` regardless of what the primitive returned —
that gating lives at the consumer layer (PR 3), not here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional

from src.trajectory_identity import _dtw_similarity


# ---------------------------------------------------------------------------
# Constants — seeded thresholds per v3.1 §"Plausibility → verdict thresholds"
# ---------------------------------------------------------------------------

_THRESHOLD_PLAUSIBLE = 0.70
_THRESHOLD_UNSUPPORTED = 0.55
_DEFAULT_MIN_OBSERVATIONS = 5
_DEFAULT_WINDOW = timedelta(days=30)
_DIMENSIONS = ("E", "I", "S", "V")


# ---------------------------------------------------------------------------
# Dataclass — full record returned to internal callers (v3.1 §"Input signature")
# ---------------------------------------------------------------------------

Verdict = Literal["plausible", "inconclusive", "unsupported"]
CalibrationStatus = Literal["seeded", "earned", "calibration_failed"]


@dataclass
class TrajectoryContinuityScore:
    """Full per-call score record. Internal callers (policy layer) see this
    dataclass; the public KG sees only `_build_public_payload(score)`."""
    score_id: str
    plausibility: float
    verdict: Verdict
    observations: Dict[str, Dict[str, int]]      # {"parent": {"E": 30, ...}, "successor": {"E": 10, ...}}
    components: Dict[str, Optional[float]]       # per-dim similarity OR None when skipped
    reasons: List[str]
    parent_mature: bool
    calibration_status: CalibrationStatus
    n_dims_used: int                             # number of components that contributed to plausibility


# ---------------------------------------------------------------------------
# Public payload builder — v3.3-A strict redaction
# ---------------------------------------------------------------------------

def _build_public_payload(score: TrajectoryContinuityScore) -> Dict[str, Any]:
    """Redact the full score down to the four fields v3.3-A allows on the
    public KG: verdict, calibration_status, n_dims_used, score_id.

    No plausibility scalar. No per-dim observations. No parent_mature. No
    reasons. The score_id is the join key into `audit.r1_score_audit` for
    operator-only forensic access; no other field of the full record leaks
    into the public KG.
    """
    return {
        "verdict": score.verdict,
        "calibration_status": score.calibration_status,
        "n_dims_used": score.n_dims_used,
        "score_id": score.score_id,
    }


# ---------------------------------------------------------------------------
# Primary primitive
# ---------------------------------------------------------------------------

async def score_trajectory_continuity(
    claimed_parent_id: str,
    successor_id: str,
    *,
    min_observations: int = _DEFAULT_MIN_OBSERVATIONS,
    window: timedelta = _DEFAULT_WINDOW,
) -> TrajectoryContinuityScore:
    """Score how well the successor's observed EISV trajectory matches the
    declared parent's, over the given window.

    See module docstring for the full contract. Returns a
    `TrajectoryContinuityScore` dataclass. Side effect: awaited write to
    `audit.r1_score_audit` (the public KG emission is built but the actual
    write lives in PR 3 — `_build_public_payload(score)` produces the
    redacted shape callers can use today).

    Threshold cuts (v3.1, seeded — calibration is shadow-mode work in PR 3+):
    - successor < min_observations rows OR parent_mature=False → inconclusive
    - plausibility >= 0.70 → plausible
    - 0.55 <= plausibility < 0.70 → inconclusive
    - plausibility < 0.55 → unsupported

    Per v3.3-H.C4: per-dimension absence (empty list from
    reconstruct_eisv_series) excludes that dimension from the plausibility
    average rather than scoring 0.0.
    """
    # Lazy import — CLAUDE.md anyio pattern + avoids import cycle through
    # src.db when this module is loaded by tests using mock_backend.
    from src.db import get_db
    backend = get_db()

    parent_series = await backend.reconstruct_eisv_series(
        agent_id=claimed_parent_id, window=window,
    )
    successor_series = await backend.reconstruct_eisv_series(
        agent_id=successor_id, window=window,
    )

    parent_obs = {dim: len(parent_series[dim]) for dim in _DIMENSIONS}
    successor_obs = {dim: len(successor_series[dim]) for dim in _DIMENSIONS}
    observations = {"parent": parent_obs, "successor": successor_obs}

    parent_mature = max(parent_obs.values()) >= min_observations
    successor_total = max(successor_obs.values())

    components: Dict[str, Optional[float]] = {}
    reasons: List[str] = []

    for dim in _DIMENSIONS:
        p_count = parent_obs[dim]
        s_count = successor_obs[dim]
        # Skip-not-zero contract (v3.3-H.C4): missing data on either side
        # excludes the dimension from the average; it does not contribute 0.0.
        if p_count < min_observations or s_count < min_observations:
            components[dim] = None
            reasons.append(
                f"{dim} skipped: parent={p_count}, successor={s_count} "
                f"(min_observations={min_observations})"
            )
            continue
        sim = _dtw_similarity(parent_series[dim], successor_series[dim])
        components[dim] = float(sim)

    contributing = [v for v in components.values() if v is not None]
    n_dims_used = len(contributing)
    plausibility = (sum(contributing) / n_dims_used) if contributing else 0.0

    verdict = _classify_verdict(
        plausibility=plausibility,
        successor_total=successor_total,
        parent_mature=parent_mature,
        n_dims_used=n_dims_used,
        min_observations=min_observations,
    )
    if not parent_mature:
        reasons.append(
            f"parent_mature=False (max parent rows in any dim "
            f"= {max(parent_obs.values())} < min_observations={min_observations})"
        )
    if successor_total < min_observations:
        reasons.append(
            f"successor below min_observations "
            f"(max={successor_total} < {min_observations})"
        )

    score = TrajectoryContinuityScore(
        score_id=str(uuid.uuid4()),
        plausibility=plausibility,
        verdict=verdict,
        observations=observations,
        components=components,
        reasons=reasons,
        parent_mature=parent_mature,
        calibration_status="seeded",  # v3.3-C: default until operator transitions
        n_dims_used=n_dims_used,
    )

    # Side effect: persist the full record to audit.r1_score_audit. Awaited
    # so the score_id is durably present before any caller publishes the
    # redacted KG payload that references it (v3.3-A join-semantics contract).
    await backend.record_r1_score_audit(_to_audit_record(
        score=score,
        parent_id=claimed_parent_id,
        successor_id=successor_id,
    ))

    return score


def _classify_verdict(
    *,
    plausibility: float,
    successor_total: int,
    parent_mature: bool,
    n_dims_used: int,
    min_observations: int,
) -> Verdict:
    """Apply v3.1 threshold cuts."""
    if successor_total < min_observations:
        return "inconclusive"
    if not parent_mature:
        return "inconclusive"
    if n_dims_used == 0:
        return "inconclusive"
    if plausibility >= _THRESHOLD_PLAUSIBLE:
        return "plausible"
    if plausibility >= _THRESHOLD_UNSUPPORTED:
        return "inconclusive"
    return "unsupported"


def _to_audit_record(
    *,
    score: TrajectoryContinuityScore,
    parent_id: str,
    successor_id: str,
) -> Dict[str, Any]:
    """Shape the full record for `record_r1_score_audit`. class_tag is left
    None here; the v3.3-G class-tag stamp will be wired in PR 3 (it requires
    reading parent's class metadata at scoring time)."""
    return {
        "score_id": score.score_id,
        "parent_id": parent_id,
        "successor_id": successor_id,
        "recorded_at": datetime.now(timezone.utc),
        "plausibility": score.plausibility,
        "components": score.components,
        "observations": score.observations,
        "parent_mature": score.parent_mature,
        "reasons": score.reasons,
        "class_tag": None,
        "calibration_status": score.calibration_status,
    }
