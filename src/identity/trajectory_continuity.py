"""R1 score_trajectory_continuity — single-channel per-dimension DTW similarity
plausibility score for declared lineage claims.

 v3.1 + v3.3.

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
- Public KG emission writes the redacted `_build_public_payload` projection to
  `knowledge.discoveries`; audit remains the durable full record

Calibration status: every score record stamps `calibration_status='seeded'` by
default until operator transitions the lifecycle to `earned` or
`calibration_failed` (v3.3-C). Consumers under `calibration_failed` MUST
degrade verdict to `inconclusive` regardless of what the primitive returned —
that gating lives at the consumer layer (PR 3), not here.
"""

from __future__ import annotations

import json
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

# UUIDv5 namespace for the public KG node ID per (parent, successor) pair.
# Stable across processes — derived from NAMESPACE_OID + a fixed name. The
# resulting node ID is `f"r1_score:{uuid5(...)}"`; ON CONFLICT (id) DO UPDATE
# in `kg_add_discovery` gives v3.2-D dedupe-by-pair for free.
_R1_KG_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_OID, "unitares.r1.trajectory_continuity_score")

# Class tags recognized for R1's `class_tag` stamping (v3.3-G). Order matters
# — most-specific first so calibration analyses can partition without
# ambiguity. Substrate classes (embodied, persistent) are most specific;
# engaged_ephemeral is S8a Phase-2's specialized cohort; ephemeral is the
# Phase-1 default stamp.
#
# Stored tags only — `_CLASS_TAG_PRIORITY` mirrors the *atomic* tags written
# by src/identity/substrate.py SUBSTRATE_CLASS_TAGS, src/grounding/
# onboard_classifier.py defaults, and src/grounding/class_promotion.py
# Phase-2 promotion. Derived class labels (e.g. `resident_persistent` =
# `persistent` + `autonomous`, computed at analysis time) are NOT in this
# tuple — calibration analyses partitioning by resident cohort must read
# `class_tag='persistent'` AND join the parent's `metadata->'tags' @> ARRAY['autonomous']`.
#
# `session_like` is a *reserved* tag — named in the v3.3-F known-limitation
# discussion + S8a Phase-2 plan, but no current stamp path emits it. Kept in
# the priority order so when S8a Phase-2 grows the cohort, the stamp surface
# is already correct. Until then, this branch is dead code (harmless).
_CLASS_TAG_PRIORITY = (
    "embodied",
    "persistent",
    "engaged_ephemeral",
    "session_like",  # reserved; no current stamp path — see comment above
    "ephemeral",
)


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


def _public_kg_node_id(parent_id: str, successor_id: str) -> str:
    """Deterministic node ID per (parent, successor) pair.

    Per v3.2-D dedupe-by-pair: the N-th score for the same pair overwrites
    the (N-1)-th in the public KG. ON CONFLICT (id) DO UPDATE in
    `kg_add_discovery` (`src/db/mixins/knowledge_graph.py:82`) gives this
    semantics for free as long as the id is deterministic from the pair.

    Prefix `r1_score:` makes the id self-describing in logs/queries; the
    UUIDv5 suffix is the deterministic-and-stable part.
    """
    return f"r1_score:{uuid.uuid5(_R1_KG_NAMESPACE, f'{parent_id}:{successor_id}')}"


async def _emit_public_kg_node(
    score: TrajectoryContinuityScore,
    *,
    parent_id: str,
    successor_id: str,
) -> bool:
    """Publish the v3.3-A redacted score to the public KG.

    Closes PR 2's deferred KG emission (commit 83e70aa: "KG public emission
    deferred to PR 3 alongside consumer patches" — PR 3 stayed score-side,
    so this is the actual write path).

    Per v3.3-A:
    - Public payload is exactly `{verdict, calibration_status, n_dims_used,
      score_id}` — `_build_public_payload` produces this shape.
    - Audit table is the canonical record; the public node is its redacted
      projection joined by `score_id`.

    Per v3.2-D:
    - Dedupe by (parent_id, successor_id). N-th score overwrites (N-1)-th.
      Achieved via deterministic node id + existing ON CONFLICT path.

    Fail-soft contract: emission is observability, not a durability gate.
    A failure here logs at warn but does not propagate — the audit row is
    already written by the time we reach this helper, which is what
    consumers needing forensic access read.

    Returns True on successful write, False otherwise (so tests can assert
    the side-effect path without parsing logs). False covers both the
    "no data to score" skip below and the fail-soft exception path.
    """
    # When n_dims_used == 0 the verdict is forced to "inconclusive" by
    # _classify_verdict's short-circuit (no channels had enough data to
    # score). Emitting "I couldn't score this" to the public KG is noise:
    # the audit row remains as the forensic anchor (canonical record per
    # the v3.3-A docstring contract above), and downstream R2/sweep
    # consumers operate on the audit table, not the KG node. Measured
    # 2026-05-30: 24 of 26 new R1 KG discoveries were n_dims=0 from
    # fresh-agent onboards that had no core.agent_state history yet.
    if score.n_dims_used == 0:
        return False
    try:
        from src.knowledge_graph import get_knowledge_graph
        from src.knowledge_graph import DiscoveryNode

        public = _build_public_payload(score)
        node = DiscoveryNode(
            id=_public_kg_node_id(parent_id, successor_id),
            agent_id=successor_id,
            type="trajectory_continuity_score",
            summary=(
                f"R1 lineage score: verdict={public['verdict']} "
                f"calibration={public['calibration_status']} "
                f"n_dims={public['n_dims_used']}"
            ),
            details=json.dumps(public),
            tags=[
                "r1",
                "trajectory_continuity",
                f"verdict:{public['verdict']}",
                f"calibration:{public['calibration_status']}",
            ],
            severity="low",
            status="open",
        )
        graph = await get_knowledge_graph()
        await graph.add_discovery(node)
        return True
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning(
            "[R1] public KG emission failed for score_id=%s parent=%s "
            "successor=%s: %s",
            score.score_id, parent_id[:8], successor_id[:8], exc,
        )
        return False


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
    `TrajectoryContinuityScore` dataclass. Side effects: awaited write to
    `audit.r1_score_audit`, followed by fail-soft public KG emission of the
    redacted `_build_public_payload(score)` projection.

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

    # v3.3-C: snapshot calibration_state at scoring time. Consumers under
    # `calibration_failed` degrade verdict to inconclusive (handled below
    # before the dataclass is built so the audit record matches).
    cal_state = await backend.read_r1_calibration_state()
    calibration_status: CalibrationStatus = cal_state.get("calibration_status", "seeded")

    # v3.3-G: read parent's class_tag from metadata at scoring time. Stored
    # on the audit record so calibration analyses partition on the parent
    # class state *at scoring time*, not at analysis time.
    class_tag = await _read_parent_class_tag(backend, claimed_parent_id)

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

    raw_verdict = _classify_verdict(
        plausibility=plausibility,
        successor_total=successor_total,
        parent_mature=parent_mature,
        n_dims_used=n_dims_used,
        min_observations=min_observations,
    )
    # v3.3-C consumer-degradation: under calibration_failed, the verdict is
    # forced to `inconclusive` regardless of plausibility. Stamping happens
    # here (before dataclass + audit write) so the audit row matches what
    # the consumer-facing primitive returns. raw_verdict is preserved on the
    # audit row separately (migration 033) for forensic access — threshold
    # drift in shadow-mode calibration would otherwise make verdict
    # reconstruction lossy.
    if calibration_status == "calibration_failed":
        verdict = "inconclusive"
        if raw_verdict != "inconclusive":
            reasons.append(
                f"verdict degraded from {raw_verdict!r} to 'inconclusive' "
                f"because calibration_status='calibration_failed' (v3.3-C)"
            )
    else:
        verdict = raw_verdict
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
        calibration_status=calibration_status,  # v3.3-C: snapshot at scoring time
        n_dims_used=n_dims_used,
    )

    # Side effect: persist the full record to audit.r1_score_audit. Awaited
    # so the score_id is durably present before any caller publishes the
    # redacted KG payload that references it (v3.3-A join-semantics contract).
    # If the write fails, we MUST refuse to return a score whose audit row is
    # absent — otherwise PR 4's KG emission could publish a dangling score_id.
    persisted = await backend.record_r1_score_audit(_to_audit_record(
        score=score,
        parent_id=claimed_parent_id,
        successor_id=successor_id,
        class_tag=class_tag,
        raw_verdict=raw_verdict,
    ))
    if not persisted:
        raise RuntimeError(
            f"R1 audit write failed for score_id={score.score_id}; "
            f"refusing to return a score whose audit anchor is absent "
            f"(v3.3-A join-key durability contract)."
        )

    # v3.3-A public KG emission: redacted projection of the audit row,
    # dedupe-by-pair via deterministic node id (v3.2-D). Fail-soft — audit
    # is the durable record; this is the public observability surface.
    await _emit_public_kg_node(
        score, parent_id=claimed_parent_id, successor_id=successor_id,
    )

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
    class_tag: Optional[str] = None,
    raw_verdict: Optional[str] = None,
) -> Dict[str, Any]:
    """Shape the full record for `record_r1_score_audit`. class_tag is the
    parent's class at scoring time (v3.3-G) or None when no recognized class
    tag is present in the parent's metadata. raw_verdict (migration 033) is
    the pre-degradation verdict — preserved separately from `verdict` so
    forensic access has both the original and the consumer-facing values."""
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
        "class_tag": class_tag,
        "calibration_status": score.calibration_status,
        "verdict": score.verdict,
        "raw_verdict": raw_verdict if raw_verdict is not None else score.verdict,
    }


async def _read_parent_class_tag(backend, parent_agent_id: str) -> Optional[str]:
    """Read the parent's class_tag from `core.identities.metadata.tags` at
    scoring time (v3.3-G).

    Returns the most-specific recognized class tag per `_CLASS_TAG_PRIORITY`,
    or None when (a) the parent has no recognized class tag, or (b) the
    parent identity row doesn't exist yet. Falls back to None on any error
    so a missing class tag never blocks a score from being recorded.
    """
    try:
        record = await backend.get_identity(parent_agent_id)
    except Exception as exc:
        # Narrow swallow — class_tag is a forensic anchor, not a correctness
        # gate. Log so a calibration-blocking pool issue is visible rather
        # than silently producing class_tag=NULL on every score.
        import logging
        logging.getLogger(__name__).debug(
            "[R1] _read_parent_class_tag get_identity failed for %s: %s (%s)",
            parent_agent_id, type(exc).__name__, exc,
        )
        return None
    if record is None:
        return None
    metadata = getattr(record, "metadata", None) or {}
    if not isinstance(metadata, dict):
        return None
    tags = metadata.get("tags")
    if not isinstance(tags, list):
        return None
    tag_set = {t for t in tags if isinstance(t, str)}
    for candidate in _CLASS_TAG_PRIORITY:
        if candidate in tag_set:
            return candidate
    return None
