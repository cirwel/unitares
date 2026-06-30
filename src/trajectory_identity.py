"""
Trajectory Identity Integration for UNITARES Governance.

Thin integration layer that:
1. Stores genesis signatures (Σ₀) at agent onboarding
2. Compares trajectory signatures on updates
3. Detects anomalies via trajectory deviation

Based on the internal/unpublished UNITARES paper "Trajectory Identity: A
Mathematical Framework for Enactive AI Self-Hood" (project-authored; not an
external publication). For external prior-art positioning of this approach, see
docs/ontology/trajectory-identity-prior-art-2026-06.md.

This is a lightweight integration - trajectory data is optional and non-blocking.
Agents can operate without providing trajectory signatures; this is additive.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import math

from src.logging_utils import get_logger

logger = get_logger(__name__)

_TRUST_TIER_NAMES = {
    0: "unknown",
    1: "emerging",
    2: "established",
    3: "verified",
}

# Paper Definition 2.2: Viability Envelope (EISV bounds)
VIABILITY_BOUNDS = {
    "E": (0.1, 0.9),
    "I": (0.3, 1.0),
    "S": (0.0, 0.6),
    "V": (-0.2, 0.15),
}


def bhattacharyya_similarity(
    mu1: List[float], cov1: List[List[float]],
    mu2: List[float], cov2: List[List[float]],
) -> float:
    """Bhattacharyya coefficient between two Gaussian distributions.

    Paper Section 4.2:
    D_B = (1/8)(mu1-mu2)^T Sigma_avg^{-1} (mu1-mu2)
        + (1/2) ln(|Sigma_avg| / sqrt(|Sigma1| * |Sigma2|))
    sim = exp(-D_B)

    Falls back to center-only distance if matrices are singular.
    """
    n = len(mu1)
    eps = 1e-6

    # Average covariance with epsilon regularization
    s_avg = [[(cov1[i][j] + cov2[i][j]) / 2.0 + (eps if i == j else 0.0)
              for j in range(n)] for i in range(n)]
    s1_reg = [[cov1[i][j] + (eps if i == j else 0.0) for j in range(n)] for i in range(n)]
    s2_reg = [[cov2[i][j] + (eps if i == j else 0.0) for j in range(n)] for i in range(n)]

    det_avg = _det(s_avg)
    det1 = _det(s1_reg)
    det2 = _det(s2_reg)

    if det_avg <= 0 or det1 <= 0 or det2 <= 0:
        dist = sum((a - b)**2 for a, b in zip(mu1, mu2)) ** 0.5
        return math.exp(-dist * 2)

    inv_avg = _inv(s_avg)
    if inv_avg is None:
        dist = sum((a - b)**2 for a, b in zip(mu1, mu2)) ** 0.5
        return math.exp(-dist * 2)

    diff = [a - b for a, b in zip(mu1, mu2)]
    mahal = sum(diff[i] * inv_avg[i][j] * diff[j] for i in range(n) for j in range(n)) / 8.0
    det_term = 0.5 * math.log(det_avg / math.sqrt(det1 * det2))
    d_b = mahal + det_term
    return max(0.0, min(1.0, math.exp(-d_b)))


def _det(m: List[List[float]]) -> float:
    """Determinant via LU elimination."""
    n = len(m)
    mat = [row[:] for row in m]
    det = 1.0
    for i in range(n):
        if abs(mat[i][i]) < 1e-12:
            for j in range(i + 1, n):
                if abs(mat[j][i]) > 1e-12:
                    mat[i], mat[j] = mat[j], mat[i]
                    det *= -1
                    break
            else:
                return 0.0
        det *= mat[i][i]
        for j in range(i + 1, n):
            factor = mat[j][i] / mat[i][i]
            for k in range(i, n):
                mat[j][k] -= factor * mat[i][k]
    return det


def _inv(m: List[List[float]]) -> Optional[List[List[float]]]:
    """Inverse via Gauss-Jordan elimination."""
    n = len(m)
    aug = [m[i][:] + [1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
    for col in range(n):
        max_row = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if max_row != col:
            aug[col], aug[max_row] = aug[max_row], aug[col]
        pivot = aug[col][col]
        if abs(pivot) < 1e-12:
            return None
        for j in range(2 * n):
            aug[col][j] /= pivot
        for row in range(n):
            if row != col:
                factor = aug[row][col]
                for j in range(2 * n):
                    aug[row][j] -= factor * aug[col][j]
    return [[aug[i][j + n] for j in range(n)] for i in range(n)]


def homeostatic_similarity(eta1: Dict[str, Any], eta2: Dict[str, Any]) -> float:
    """Compute Eta (Homeostatic Identity) similarity (paper Section 3.6).

    Combines set-point proximity, recovery dynamics, and viability margin.
    """
    scores = []
    weights = []

    sp1 = eta1.get("set_point")
    sp2 = eta2.get("set_point")
    if sp1 and sp2 and len(sp1) == len(sp2):
        bs1 = eta1.get("basin_shape")
        bs2 = eta2.get("basin_shape")
        if bs1 and bs2:
            scores.append(bhattacharyya_similarity(sp1, bs1, sp2, bs2))
        else:
            dist = sum((a - b)**2 for a, b in zip(sp1, sp2)) ** 0.5
            scores.append(math.exp(-dist * 2))
        weights.append(0.4)

    tau1 = eta1.get("recovery_tau")
    tau2 = eta2.get("recovery_tau")
    if tau1 and tau2 and tau1 > 0 and tau2 > 0:
        scores.append(math.exp(-abs(math.log(tau1 / tau2))))
        weights.append(0.3)

    bounds1 = eta1.get("viability_bounds", VIABILITY_BOUNDS)
    bounds2 = eta2.get("viability_bounds", VIABILITY_BOUNDS)
    if sp1 and sp2:
        m1 = _viability_margin(sp1, bounds1)
        m2 = _viability_margin(sp2, bounds2)
        if m1 is not None and m2 is not None:
            scores.append(1.0 - abs(m1 - m2))
            weights.append(0.3)

    if not scores:
        return 0.5
    total = sum(weights)
    return sum(s * w for s, w in zip(scores, weights)) / total


def _viability_margin(set_point: List[float], bounds: Dict[str, Any]) -> Optional[float]:
    """How safely centered is the set-point within viability bounds."""
    dim_keys = list(bounds.keys())
    if len(set_point) != len(dim_keys):
        return None
    margins = []
    for i, key in enumerate(dim_keys):
        lo, hi = bounds[key]
        span = hi - lo
        if span <= 0:
            continue
        margins.append(max(0.0, min(set_point[i] - lo, hi - set_point[i]) / span))
    return sum(margins) / len(margins) if margins else None


def _dtw_distance(s1: List[float], s2: List[float]) -> float:
    """Dynamic Time Warping distance between two 1D time series.

    O(n*m) DP. For typical governance history lengths (100 samples),
    this is ~10,000 operations — trivially fast.

    Returns normalized distance in [0, inf). Lower = more similar.
    """
    n, m = len(s1), len(s2)
    if n == 0 or m == 0:
        return float("inf")

    dtw = [[float("inf")] * (m + 1) for _ in range(n + 1)]
    dtw[0][0] = 0.0

    for i in range(1, n + 1):
        for j in range(1, m + 1):
            cost = abs(s1[i - 1] - s2[j - 1])
            dtw[i][j] = cost + min(dtw[i - 1][j], dtw[i][j - 1], dtw[i - 1][j - 1])

    return dtw[n][m] / max(n, m)


def _dtw_similarity(s1: List[float], s2: List[float]) -> float:
    """Convert DTW distance to similarity score in [0, 1].

    Uses exponential kernel: sim = exp(-distance * 2.0).
    """
    dist = _dtw_distance(s1, s2)
    if dist == float("inf"):
        return 0.0
    return math.exp(-dist * 2.0)


def _eisv_trajectory_similarity(
    sig1: "TrajectorySignature",
    sig2: "TrajectorySignature",
) -> Optional[float]:
    """Compare EISV trajectory shapes using per-dimension DTW.

    Looks for E_trajectory, I_trajectory, S_trajectory, V_trajectory
    in the attractor dict. Returns None if insufficient data (< 10 samples).
    """
    a1 = sig1.attractor or {}
    a2 = sig2.attractor or {}

    dimensions = ["E", "I", "S", "V"]
    sims = []

    for dim in dimensions:
        key = f"{dim}_trajectory"
        t1 = a1.get(key, [])
        t2 = a2.get(key, [])
        if len(t1) >= 10 and len(t2) >= 10:
            sims.append(_dtw_similarity(t1, t2))

    if not sims:
        return None

    return sum(sims) / len(sims)


@dataclass
class TrajectorySignature:
    """
    Minimal trajectory signature for governance integration.

    Full computation happens upstream in the agent; UNITARES receives and
    stores the computed signature for comparison and anomaly detection.
    """
    # Core components (as computed upstream; see agent's trajectory module)
    preferences: Dict[str, Any] = field(default_factory=dict)   # Π
    beliefs: Dict[str, Any] = field(default_factory=dict)       # B
    attractor: Optional[Dict[str, Any]] = None                  # A
    recovery: Dict[str, Any] = field(default_factory=dict)      # R
    relational: Dict[str, Any] = field(default_factory=dict)    # Δ
    homeostatic: Optional[Dict[str, Any]] = None                # Η

    # Metadata
    computed_at: Optional[str] = None
    observation_count: int = 0
    stability_score: float = 0.0
    identity_confidence: float = 0.0

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TrajectorySignature":
        """Create from dictionary (e.g., from MCP tool argument)."""
        return cls(
            preferences=data.get("preferences", {}),
            beliefs=data.get("beliefs", {}),
            attractor=data.get("attractor"),
            recovery=data.get("recovery", {}),
            relational=data.get("relational", {}),
            homeostatic=data.get("homeostatic"),
            computed_at=data.get("computed_at"),
            observation_count=data.get("observation_count", 0),
            stability_score=data.get("stability_score", 0.0),
            identity_confidence=data.get("identity_confidence", 0.0),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for storage."""
        return {
            "preferences": self.preferences,
            "beliefs": self.beliefs,
            "attractor": self.attractor,
            "recovery": self.recovery,
            "relational": self.relational,
            "homeostatic": self.homeostatic,
            "computed_at": self.computed_at,
            "observation_count": self.observation_count,
            "stability_score": self.stability_score,
            "identity_confidence": self.identity_confidence,
        }

    def similarity(self, other: "TrajectorySignature") -> float:
        """
        Compute similarity using the paper's six-component model.

        Weights: Pi=0.15, Beta=0.15, Alpha=0.25, Rho=0.20, Delta=0.10, Eta=0.15
        Components are skipped when data is unavailable; weights renormalize.
        """
        scores = []
        weights = []

        # Pi: Preference similarity (cosine)
        pv1 = self.preferences.get("vector")
        pv2 = other.preferences.get("vector")
        if pv1 and pv2 and len(pv1) == len(pv2):
            sim = self._cosine_similarity(pv1, pv2)
            if sim is not None:
                scores.append((sim + 1) / 2)
                weights.append(0.15)

        # Beta: Belief similarity (cosine)
        if self.beliefs.get("values") and other.beliefs.get("values"):
            v1, v2 = self.beliefs["values"], other.beliefs["values"]
            if len(v1) == len(v2) and len(v1) > 0:
                sim = self._cosine_similarity(v1, v2)
                if sim is not None:
                    scores.append((sim + 1) / 2)
                    weights.append(0.15)

        # Alpha: Attractor similarity (Bhattacharyya when covariance available)
        if self.attractor and other.attractor:
            c1 = self.attractor.get("center", [])
            c2 = other.attractor.get("center", [])
            if c1 and c2 and len(c1) == len(c2):
                cov1 = self.attractor.get("covariance")
                cov2 = other.attractor.get("covariance")
                if cov1 and cov2 and len(cov1) == len(c1) and len(cov2) == len(c2):
                    scores.append(bhattacharyya_similarity(c1, cov1, c2, cov2))
                else:
                    dist = sum((a - b)**2 for a, b in zip(c1, c2)) ** 0.5
                    scores.append(math.exp(-dist * 2))
                weights.append(0.25)

        # Rho: Recovery tau similarity (log-ratio)
        t1 = self.recovery.get("tau_estimate")
        t2 = other.recovery.get("tau_estimate")
        if t1 and t2 and t1 > 0 and t2 > 0:
            scores.append(math.exp(-abs(math.log(t1 / t2))))
            weights.append(0.20)

        # Delta: Relational similarity (valence L1)
        rv1 = self.relational.get("valence_tendency")
        rv2 = other.relational.get("valence_tendency")
        if rv1 is not None and rv2 is not None:
            scores.append(max(0.0, 1 - abs(rv1 - rv2) / 2))
            weights.append(0.10)

        # Eta: Homeostatic similarity
        if self.homeostatic and other.homeostatic:
            scores.append(homeostatic_similarity(self.homeostatic, other.homeostatic))
            weights.append(0.15)

        if not scores:
            return 0.5

        total_weight = sum(weights)
        return sum(s * w for s, w in zip(scores, weights)) / total_weight

    def trajectory_shape_similarity(self, other: "TrajectorySignature") -> Optional[float]:
        """DTW-based trajectory shape comparison (governance extension, not in paper).

        Supplemental discrimination signal. Not part of the six-component model.
        """
        return _eisv_trajectory_similarity(self, other)

    def _cosine_similarity(self, v1: List[float], v2: List[float]) -> Optional[float]:
        """Cosine similarity between vectors."""
        if len(v1) != len(v2) or len(v1) == 0:
            return None
        dot = sum(a * b for a, b in zip(v1, v2))
        norm1 = sum(a * a for a in v1) ** 0.5
        norm2 = sum(b * b for b in v2) ** 0.5
        if norm1 == 0 or norm2 == 0:
            return None
        return dot / (norm1 * norm2)


async def store_genesis_signature(
    agent_id: str,
    signature: TrajectorySignature,
) -> bool:
    """
    Store genesis signature (Σ₀) for an agent.

    Called at onboarding or first trajectory submission.
    Genesis is immutable - once set, cannot be changed.

    Returns True if stored, False if already exists.
    """
    try:
        from src.db import get_db
        db = get_db()

        # Get current metadata
        identity = await db.get_identity(agent_id)
        if not identity:
            logger.warning(f"[Trajectory] Agent {agent_id[:8]}... not found")
            return False

        metadata = identity.metadata or {}

        # Check if genesis already exists
        existing_genesis = metadata.get("trajectory_genesis")
        if existing_genesis:
            # Allow reseed at tier 1 (emerging) if new signature has
            # substantially higher confidence — early genesis from 10 data points
            # may not be representative. Once at tier 2+, genesis is immutable.
            trust = metadata.get("trust_tier", {})
            tier = trust.get("tier", 0) if isinstance(trust, dict) else 0
            existing_confidence = existing_genesis.get("identity_confidence", 0.0)
            new_confidence = signature.identity_confidence

            if tier >= 2:
                logger.debug(f"[Trajectory] Genesis immutable at tier {tier} for {agent_id[:8]}...")
                return False

            # Also reseed if lineage is below tier-2 threshold (0.7) — stale genesis
            # traps agents in "emerging" forever. Compare current to genesis to check.
            # Only check when signatures have comparable data (attractor/beliefs);
            # similarity() returns 0.5 for empty data, which isn't a real signal.
            lineage_low = False
            try:
                g = TrajectorySignature.from_dict(existing_genesis)
                has_comparable_data = (g.attractor is not None or g.beliefs.get("values"))
                if has_comparable_data:
                    lineage_sim = signature.similarity(g)
                    lineage_low = lineage_sim < 0.7
            except Exception:
                pass

            if not lineage_low and new_confidence <= existing_confidence * 1.5:
                # New signature isn't sufficiently better and lineage is fine — keep existing
                logger.debug(f"[Trajectory] Genesis reseed skipped: {new_confidence:.2f} <= {existing_confidence:.2f}*1.5")
                return False

            logger.info(
                f"[Trajectory] Reseeding genesis for {agent_id[:8]}... "
                f"(tier={tier}, confidence {existing_confidence:.2f} → {new_confidence:.2f})"
            )

        # Store genesis signature
        metadata["trajectory_genesis"] = signature.to_dict()
        metadata["trajectory_genesis_at"] = datetime.now(timezone.utc).isoformat()

        await db.update_identity_metadata(agent_id, metadata)
        logger.info(f"[Trajectory] Stored genesis Σ₀ for {agent_id[:8]}... (confidence={signature.identity_confidence:.2f})")
        return True

    except Exception as e:
        logger.error(f"[Trajectory] Failed to store genesis: {e}")
        return False


async def seed_genesis_from_parent(
    agent_id: str,
    parent_agent_id: str,
) -> Dict[str, Any]:
    """Seed an agent's genesis from its declared parent's `trajectory_current`.

 Ontology v2 primitive — R3-appendix Q2.
    Under v2 most process-instances die before accumulating the 50/200
    observations `compute_trust_tier` expects. When a fresh agent declares
    a `parent_agent_id` at onboard, seeding genesis from the parent's
    accumulated fingerprint gives the child a meaningful lineage baseline
    against which `update_current_signature` can compute similarity —
    instead of comparing the child's first ten samples against themselves.

    Behavior:
      - If the agent already has `trajectory_genesis` at tier >= 2,
        refuses to reseed (respects the immutability rule in
        `store_genesis_signature`).
      - If parent has no `trajectory_current`, no-op (nothing to seed
        from; parent's own genesis is a weaker baseline and is not used).
      - Otherwise, writes parent's current-signature dict as the child's
        genesis plus a `trajectory_genesis_source` marker for provenance.

    The caller is responsible for deciding *when* to invoke this —
    typically at onboard with `force_new=true, parent_agent_id=<uuid>`.

    Returns a dict:
      {"seeded": bool, "reason": str,
       "parent_agent_id": str, "source": Optional[str]}
    """
    try:
        from src.db import get_db
        db = get_db()

        child = await db.get_identity(agent_id)
        if not child:
            return {
                "seeded": False,
                "reason": "child identity not found",
                "parent_agent_id": parent_agent_id,
                "source": None,
            }

        child_metadata = child.metadata or {}
        existing_genesis = child_metadata.get("trajectory_genesis")
        if existing_genesis:
            trust = child_metadata.get("trust_tier", {})
            tier = trust.get("tier", 0) if isinstance(trust, dict) else 0
            if tier >= 2:
                return {
                    "seeded": False,
                    "reason": f"child has existing genesis at tier {tier} (immutable)",
                    "parent_agent_id": parent_agent_id,
                    "source": None,
                }

        parent = await db.get_identity(parent_agent_id)
        if not parent or not parent.metadata:
            return {
                "seeded": False,
                "reason": "parent identity not found or has no metadata",
                "parent_agent_id": parent_agent_id,
                "source": None,
            }

        parent_current = parent.metadata.get("trajectory_current")
        if not parent_current:
            return {
                "seeded": False,
                "reason": "parent has no trajectory_current to seed from",
                "parent_agent_id": parent_agent_id,
                "source": None,
            }

        # Write parent's current as child's genesis, with provenance.
        now_iso = datetime.now(timezone.utc).isoformat()
        child_metadata["trajectory_genesis"] = dict(parent_current)
        child_metadata["trajectory_genesis_at"] = now_iso
        child_metadata["trajectory_genesis_source"] = {
            "source": "parent_lineage",
            "parent_agent_id": parent_agent_id,
            "seeded_at": now_iso,
        }

        await db.update_identity_metadata(agent_id, child_metadata)
        logger.info(
            f"[Trajectory] Seeded genesis for {agent_id[:8]}... from parent "
            f"{parent_agent_id[:8]}... (lineage declared)"
        )
        return {
            "seeded": True,
            "reason": "seeded from parent trajectory_current",
            "parent_agent_id": parent_agent_id,
            "source": "parent_lineage",
        }

    except Exception as e:
        logger.error(f"[Trajectory] seed_genesis_from_parent failed: {e}")
        return {
            "seeded": False,
            "reason": f"error: {type(e).__name__}: {e}",
            "parent_agent_id": parent_agent_id,
            "source": None,
        }


async def update_current_signature(
    agent_id: str,
    signature: TrajectorySignature,
) -> Dict[str, Any]:
    """
    Update current trajectory signature and check for anomalies.

    Returns comparison results including:
    - similarity to genesis (lineage check)
    - is_anomaly flag
    - recommendations
    """
    try:
        from src.db import get_db
        db = get_db()

        identity = await db.get_identity(agent_id)
        if not identity:
            return {"error": "Agent not found"}

        metadata = identity.metadata or {}

        # Store current signature
        metadata["trajectory_current"] = signature.to_dict()
        metadata["trajectory_updated_at"] = datetime.now(timezone.utc).isoformat()

        result = {
            "stored": True,
            "observation_count": signature.observation_count,
            "identity_confidence": signature.identity_confidence,
        }

        # Compare to genesis if exists
        genesis_data = metadata.get("trajectory_genesis")
        if genesis_data:
            genesis = TrajectorySignature.from_dict(genesis_data)
            lineage_sim = signature.similarity(genesis)

            result["lineage_similarity"] = round(lineage_sim, 4)
            result["lineage_threshold"] = 0.6
            result["is_anomaly"] = lineage_sim < 0.6

            if result["is_anomaly"]:
                result["warning"] = f"Trajectory drift detected: similarity to genesis is {lineage_sim:.2f} (threshold: 0.6)"
                logger.warning(f"[Trajectory] ANOMALY for {agent_id[:8]}...: lineage_sim={lineage_sim:.2f}")

            # Attempt genesis reseed at tier 1 (store_genesis_signature handles guards)
            trust = metadata.get("trust_tier", {})
            tier = trust.get("tier", 0) if isinstance(trust, dict) else 0
            if tier <= 1:
                reseeded = await store_genesis_signature(agent_id, signature)
                if reseeded:
                    result["genesis_reseeded"] = True
                    # Recompute lineage after reseed (it's now 1.0 by definition)
                    result["lineage_similarity"] = 1.0
                    result["is_anomaly"] = False
                    result.pop("warning", None)
                    # Refresh metadata after reseed, preserving fields that
                    # store_genesis_signature doesn't know about (trajectory_current,
                    # trust_tier).  Without this, the re-read loses trust_tier and
                    # the tier-change broadcast fires on every check-in.
                    prev_current = metadata.get("trajectory_current")
                    prev_updated_at = metadata.get("trajectory_updated_at")
                    prev_trust_tier = metadata.get("trust_tier")
                    identity = await db.get_identity(agent_id)
                    metadata = identity.metadata or {}
                    if prev_current:
                        metadata["trajectory_current"] = prev_current
                        metadata["trajectory_updated_at"] = prev_updated_at
                    if prev_trust_tier is not None:
                        metadata["trust_tier"] = prev_trust_tier

            # Drift alert: emit audit event + broadcast if anomaly persists after reseed
            if result.get("is_anomaly"):
                try:
                    from src.audit_db import append_audit_event_async
                    from src.broadcaster import broadcaster_instance
                    await append_audit_event_async({
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "event_type": "trajectory_drift",
                        "agent_id": agent_id,
                        "details": {
                            "lineage_similarity": lineage_sim,
                            "threshold": 0.6,
                            "trust_tier": tier,
                        },
                    })
                    await broadcaster_instance.broadcast_event(
                        "identity_drift",
                        agent_id=agent_id,
                        payload={"lineage_similarity": lineage_sim, "threshold": 0.6},
                    )
                except Exception as e:
                    logger.debug(f"Drift alert emission failed: {e}")
        else:
            # No genesis - store this as genesis
            await store_genesis_signature(agent_id, signature)
            result["genesis_created"] = True
            # Reflect genesis in local metadata so trust tier computation is correct
            metadata["trajectory_genesis"] = signature.to_dict()
            metadata["trajectory_genesis_at"] = datetime.now(timezone.utc).isoformat()

        # Compute and store trust tier before saving. Use the substrate-aware
        # resolver here; otherwise resident agents can be demoted by the raw
        # session-like trajectory thresholds before the canonical routing gets
        # a chance to recognize substrate-earned identity.
        old_tier = metadata.get("trust_tier", {}).get("tier", 0) if isinstance(metadata.get("trust_tier"), dict) else 0
        try:
            from src.identity.trust_tier_routing import resolve_trust_tier

            prefetched_tags = metadata.get("tags")
            prefetched_label = metadata.get("label")
            if (prefetched_tags is None or prefetched_label is None) and hasattr(db, "get_agent"):
                agent_record = await db.get_agent(agent_id)
                if isinstance(agent_record, dict):
                    if prefetched_tags is None:
                        prefetched_tags = agent_record.get("tags")
                    if prefetched_label is None:
                        prefetched_label = agent_record.get("label")

            if prefetched_tags is not None and prefetched_label is not None:
                trust_tier = await resolve_trust_tier(
                    agent_id,
                    metadata,
                    prefetched_tags=prefetched_tags,
                    prefetched_label=prefetched_label,
                )
            else:
                trust_tier = await resolve_trust_tier(agent_id, metadata)
        except Exception as e:
            logger.debug(f"Trust tier routing failed, using trajectory-only tier: {e}")
            trust_tier = compute_trust_tier(metadata)
        metadata["trust_tier"] = trust_tier
        result["trust_tier"] = trust_tier

        # Broadcast on trust tier change
        if trust_tier.get("tier", 0) != old_tier:
            try:
                from src.broadcaster import broadcaster_instance
                await broadcaster_instance.broadcast_event(
                    "identity_assurance_change",
                    agent_id=agent_id,
                    payload={
                        "old_tier": old_tier,
                        "new_tier": trust_tier["tier"],
                        "tier_name": trust_tier.get("name", "unknown"),
                    },
                )
            except Exception as e:
                logger.debug(f"Trust tier change broadcast failed: {e}")

        # Save updated metadata
        await db.update_identity_metadata(agent_id, metadata)

        return result

    except Exception as e:
        logger.error(f"[Trajectory] Failed to update signature: {e}")
        return {"error": str(e)}


def compute_trust_tier(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """
    Compute trust tier from trajectory metadata stored in identity.metadata.

    Pure function: takes metadata dict, returns tier assessment.
    No DB access, no side effects.

    Uses hysteresis to prevent oscillation at tier boundaries: promoting
    requires meeting the full threshold, but demoting requires dropping
    below a lower margin (5% below the promotion threshold).

    Tiers:
        0 (unknown):     No trajectory data
        1 (emerging):    Has genesis, < 50 observations OR confidence < 0.5
        2 (established): 50+ observations, confidence >= 0.5, lineage > 0.7
        3 (verified):    200+ observations, confidence >= 0.7, lineage > 0.8
    """
    genesis = metadata.get("trajectory_genesis")
    current = metadata.get("trajectory_current")

    # Tier 0: No trajectory data at all
    if not genesis:
        return {
            "tier": 0,
            "name": "unknown",
            "observation_count": 0,
            "identity_confidence": 0.0,
            "lineage_similarity": None,
            "reason": "No trajectory data",
        }

    # Use current if available, otherwise genesis
    sig_data = current if current else genesis
    observation_count = sig_data.get("observation_count", 0)
    identity_confidence = sig_data.get("identity_confidence", 0.0)

    # Compute lineage similarity if both genesis and current exist
    lineage_similarity = None
    if genesis and current:
        try:
            g = TrajectorySignature.from_dict(genesis)
            c = TrajectorySignature.from_dict(current)
            lineage_similarity = round(c.similarity(g), 4)
        except Exception:
            pass

    # Current tier for hysteresis — if already at a tier, require a
    # larger drop to demote (prevents oscillation at boundaries).
    prev_tier = metadata.get("trust_tier", {})
    current_tier = prev_tier.get("tier", 0) if isinstance(prev_tier, dict) else 0

    def stabilize_demoted_tier(result: Dict[str, Any]) -> Dict[str, Any]:
        """Avoid tier-1 reseed churn for established resident identities."""
        if (
            current_tier >= 2
            and result.get("tier", 0) < 2
            and result.get("observation_count", 0) >= 50
            and result.get("identity_confidence", 0.0) >= 0.45
        ):
            stabilized = dict(result)
            stabilized["tier"] = 2
            stabilized["name"] = _TRUST_TIER_NAMES[2]
            stabilized["reason"] = (
                "Retaining established identity assurance; lineage drift is "
                "reported separately instead of resetting earned trust."
            )
            return stabilized
        return result

    # Hysteresis margins: demotion thresholds are 5% below promotion thresholds
    conf_3 = 0.70 if current_tier < 3 else 0.65   # promote at 0.70, demote at 0.65
    lin_3 = 0.80 if current_tier < 3 else 0.75
    conf_2 = 0.50 if current_tier < 2 else 0.45
    lin_2 = 0.70 if current_tier < 2 else 0.65

    # Tier 3: verified (200+ obs, confidence >= 0.7, lineage > 0.8)
    if (observation_count >= 200
            and identity_confidence >= conf_3
            and lineage_similarity is not None
            and lineage_similarity > lin_3):
        return {
            "tier": 3,
            "name": "verified",
            "observation_count": observation_count,
            "identity_confidence": round(identity_confidence, 4),
            "lineage_similarity": lineage_similarity,
            "reason": f"Strong behavioral continuity ({observation_count} obs, "
                      f"confidence={identity_confidence:.2f}, lineage={lineage_similarity:.2f})",
        }

    # Tier 2: established (50+ obs, confidence >= 0.5, lineage > 0.7)
    if (observation_count >= 50
            and identity_confidence >= conf_2
            and (lineage_similarity is None or lineage_similarity > lin_2)):
        return {
            "tier": 2,
            "name": "established",
            "observation_count": observation_count,
            "identity_confidence": round(identity_confidence, 4),
            "lineage_similarity": lineage_similarity,
            "reason": f"Stable identity ({observation_count} obs, "
                      f"confidence={identity_confidence:.2f})",
        }

    # Tier 1: emerging (has genesis but doesn't meet tier 2)
    return stabilize_demoted_tier({
        "tier": 1,
        "name": "emerging",
        "observation_count": observation_count,
        "identity_confidence": round(identity_confidence, 4),
        "lineage_similarity": lineage_similarity,
        "reason": f"Building identity ({observation_count} obs, "
                  f"confidence={identity_confidence:.2f})",
    })


async def get_trajectory_status(agent_id: str) -> Dict[str, Any]:
    """Get trajectory identity status for an agent."""
    try:
        from src.db import get_db
        db = get_db()

        identity = await db.get_identity(agent_id)
        if not identity:
            return {"error": "Agent not found"}

        metadata = identity.metadata or {}

        genesis = metadata.get("trajectory_genesis")
        current = metadata.get("trajectory_current")

        result = {
            "has_genesis": genesis is not None,
            "has_current": current is not None,
            "genesis_at": metadata.get("trajectory_genesis_at"),
            "updated_at": metadata.get("trajectory_updated_at"),
        }

        if genesis:
            g = TrajectorySignature.from_dict(genesis)
            result["genesis_confidence"] = g.identity_confidence
            result["genesis_observations"] = g.observation_count

        if current:
            c = TrajectorySignature.from_dict(current)
            result["current_confidence"] = c.identity_confidence
            result["current_observations"] = c.observation_count

            if genesis:
                g = TrajectorySignature.from_dict(genesis)
                result["lineage_similarity"] = round(c.similarity(g), 4)
                result["is_drifting"] = result["lineage_similarity"] < 0.7

        return result

    except Exception as e:
        logger.error(f"[Trajectory] Failed to get status: {e}")
        return {"error": str(e)}


async def verify_trajectory_identity(
    agent_id: str,
    signature: TrajectorySignature,
    coherence_threshold: float = 0.7,
    lineage_threshold: float = 0.6,
) -> Dict[str, Any]:
    """
    Two-tier identity verification as per paper Section 6.1.2.

    Tier 1 (Coherence): Compare to recent signature
    Tier 2 (Lineage): Compare to genesis signature

    Returns verification result with both tiers.
    """
    try:
        from src.db import get_db
        db = get_db()

        identity = await db.get_identity(agent_id)
        if not identity:
            return {"verified": False, "error": "Agent not found"}

        metadata = identity.metadata or {}

        result = {
            "agent_id": agent_id[:8] + "...",
            "verified": True,
            "tiers": {},
        }

        # Tier 1: Coherence (compare to recent)
        current_data = metadata.get("trajectory_current")
        if current_data:
            current = TrajectorySignature.from_dict(current_data)
            coherence_sim = signature.similarity(current)
            tier1_passed = coherence_sim >= coherence_threshold
            result["tiers"]["coherence"] = {
                "similarity": round(coherence_sim, 4),
                "threshold": coherence_threshold,
                "passed": tier1_passed,
            }
            if not tier1_passed:
                result["verified"] = False
        else:
            result["tiers"]["coherence"] = {"skipped": True, "reason": "No current signature"}

        # Tier 2: Lineage (compare to genesis)
        genesis_data = metadata.get("trajectory_genesis")
        if genesis_data:
            genesis = TrajectorySignature.from_dict(genesis_data)
            lineage_sim = signature.similarity(genesis)
            tier2_passed = lineage_sim >= lineage_threshold
            result["tiers"]["lineage"] = {
                "similarity": round(lineage_sim, 4),
                "threshold": lineage_threshold,
                "passed": tier2_passed,
            }
            if not tier2_passed:
                result["verified"] = False
        else:
            result["tiers"]["lineage"] = {"skipped": True, "reason": "No genesis signature"}

        # Overall verdict
        if not result["verified"]:
            failed_tiers = [t for t, v in result["tiers"].items() if isinstance(v, dict) and not v.get("passed", True)]
            result["failed_tiers"] = failed_tiers
            result["warning"] = f"Identity verification failed: {', '.join(failed_tiers)}"

        return result

    except Exception as e:
        logger.error(f"[Trajectory] Verification failed: {e}")
        return {"verified": False, "error": str(e)}
