"""Agent baseline operations mixin for PostgresBackend."""

from __future__ import annotations

from typing import Any, Dict, Optional

from src.logging_utils import get_logger

logger = get_logger(__name__)


class BaselineMixin:
    """Agent baseline (ethical drift persistence) operations."""

    async def save_agent_baseline(self, baseline_dict: Dict[str, Any]) -> bool:
        """UPSERT agent baseline into core.agent_baselines."""
        from config.governance_config import GovernanceConfig
        agent_id = baseline_dict.get('agent_id')
        if not agent_id:
            return False
        try:
            async with self.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO core.agent_baselines (
                        agent_id, baseline_coherence, baseline_confidence, baseline_complexity,
                        prev_coherence, prev_confidence, prev_complexity,
                        recent_decisions, decision_consistency, update_count, alpha, updated_at, epoch
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, now(), $12)
                    ON CONFLICT (agent_id) DO UPDATE SET
                        baseline_coherence = EXCLUDED.baseline_coherence,
                        baseline_confidence = EXCLUDED.baseline_confidence,
                        baseline_complexity = EXCLUDED.baseline_complexity,
                        prev_coherence = EXCLUDED.prev_coherence,
                        prev_confidence = EXCLUDED.prev_confidence,
                        prev_complexity = EXCLUDED.prev_complexity,
                        recent_decisions = EXCLUDED.recent_decisions,
                        decision_consistency = EXCLUDED.decision_consistency,
                        update_count = EXCLUDED.update_count,
                        alpha = EXCLUDED.alpha,
                        updated_at = now(),
                        epoch = EXCLUDED.epoch
                    """,
                    agent_id,
                    baseline_dict.get('baseline_coherence', 0.5),
                    baseline_dict.get('baseline_confidence', 0.6),
                    baseline_dict.get('baseline_complexity', 0.4),
                    baseline_dict.get('prev_coherence'),
                    baseline_dict.get('prev_confidence'),
                    baseline_dict.get('prev_complexity'),
                    baseline_dict.get('recent_decisions', []),
                    baseline_dict.get('decision_consistency', 0.8),
                    baseline_dict.get('update_count', 0),
                    baseline_dict.get('alpha', 0.1),
                    GovernanceConfig.CURRENT_EPOCH,
                )
                return True
        except Exception as e:
            logger.warning(f"Failed to save agent baseline for {agent_id}: {e}")
            return False

    async def load_agent_baseline(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """Load agent baseline from core.agent_baselines."""
        from config.governance_config import GovernanceConfig
        try:
            async with self.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT * FROM core.agent_baselines WHERE agent_id = $1 AND epoch = $2",
                    agent_id, GovernanceConfig.CURRENT_EPOCH,
                )
                if row is None:
                    return None
                d = dict(row)
                d.setdefault('recent_decisions', [])
                d.pop('updated_at', None)
                return d
        except Exception as e:
            logger.warning(f"Failed to load agent baseline for {agent_id}: {e}")
            return None

    # --- Behavioral baselines (Welford stats) ---

    async def save_behavioral_baseline(self, agent_id: str, stats_dict: Dict[str, Any]) -> bool:
        """UPSERT Welford stats into core.agent_behavioral_baselines."""
        import json as _json
        try:
            async with self.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO core.agent_behavioral_baselines (agent_id, stats, updated_at)
                    VALUES ($1, $2::jsonb, now())
                    ON CONFLICT (agent_id) DO UPDATE SET
                        stats = EXCLUDED.stats,
                        updated_at = now()
                    """,
                    agent_id,
                    _json.dumps(stats_dict),
                )
                return True
        except Exception as e:
            logger.warning(f"Failed to save behavioral baseline for {agent_id}: {e}")
            return False

    async def load_behavioral_baseline(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """Load Welford stats from core.agent_behavioral_baselines."""
        import json as _json
        try:
            async with self.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT stats FROM core.agent_behavioral_baselines WHERE agent_id = $1",
                    agent_id,
                )
                if row is None:
                    return None
                stats = row["stats"]
                # asyncpg returns JSONB as a string or dict depending on version
                if isinstance(stats, str):
                    return _json.loads(stats)
                return dict(stats) if stats else None
        except Exception as e:
            logger.warning(f"Failed to load behavioral baseline for {agent_id}: {e}")
            return None

    async def load_all_behavioral_baselines(self) -> Dict[str, Dict[str, Any]]:
        """Bulk-load every agent's Welford stats as ``{agent_id: stats}``.

        Read-only. Feeds the shadow cohort-prior aggregator
        (``src/cohort_prior_source.py``), which pools these into per-class priors.
        Returns ``{}`` on error so a callsite degrades to "no cohort prior"
        rather than raising.
        """
        import json as _json
        out: Dict[str, Dict[str, Any]] = {}
        try:
            async with self.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT agent_id, stats FROM core.agent_behavioral_baselines"
                )
            for row in rows:
                stats = row["stats"]
                if isinstance(stats, str):
                    stats = _json.loads(stats)
                if stats:
                    out[row["agent_id"]] = dict(stats)
        except Exception as e:
            logger.warning(f"Failed to bulk-load behavioral baselines: {e}")
        return out
