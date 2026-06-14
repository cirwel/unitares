"""Per-agent behavioral baselines using Welford's online algorithm.

Tracks rolling statistics (mean, variance) for each agent's behavioral signals.
Anomalous values (>N std from agent's own baseline) can trigger mild entropy.

Welford stats are persisted to PostgreSQL (core.agent_behavioral_baselines)
so baselines survive server restarts.
"""

import asyncio
import logging
import math
from typing import Dict, Optional

_logger = logging.getLogger(__name__)


class WelfordStats:
    """Online mean/variance using Welford's algorithm. No numpy needed."""

    __slots__ = ("count", "mean", "m2")

    def __init__(self):
        self.count: int = 0
        self.mean: float = 0.0
        self.m2: float = 0.0

    def update(self, value: float) -> None:
        self.count += 1
        delta = value - self.mean
        self.mean += delta / self.count
        delta2 = value - self.mean
        self.m2 += delta * delta2

    @property
    def variance(self) -> float:
        if self.count < 2:
            return 0.0
        return self.m2 / (self.count - 1)

    @property
    def std(self) -> float:
        return math.sqrt(self.variance)

    def z_score(self, value: float, min_std: float = 0.0) -> float:
        """Z-score of value relative to running stats. Returns 0.0 if insufficient data.

        ``min_std`` floors the denominator at a minimum meaningful resolution.
        Without it, an ultra-stable signal (tiny baseline variance) turns a
        small, characteristically-irrelevant change into a many-sigma event.
        Default 0.0 keeps the bare ``1e-9`` div-by-zero guard for callers that
        score signals with no meaningful absolute scale.
        """
        if self.count < 5:
            return 0.0
        effective_std = max(self.std, min_std)
        if effective_std < 1e-9:
            return 0.0
        return (value - self.mean) / effective_std

    def to_dict(self) -> dict:
        return {"count": self.count, "mean": self.mean, "m2": self.m2}

    @classmethod
    def from_dict(cls, d: dict) -> "WelfordStats":
        s = cls()
        s.count = d.get("count", 0)
        s.mean = d.get("mean", 0.0)
        s.m2 = d.get("m2", 0.0)
        return s


class AgentBehavioralBaseline:
    """Rolling statistics for an agent's behavioral signals."""

    # Signals tracked per agent
    TRACKED_SIGNALS = (
        "tool_error_rate",
        "tool_call_velocity",
        "complexity_divergence",
        "coherence",
    )

    def __init__(self):
        self._stats: Dict[str, WelfordStats] = {
            name: WelfordStats() for name in self.TRACKED_SIGNALS
        }

    def update(self, signal_name: str, value: float) -> None:
        """Record a new observation for this signal."""
        if signal_name in self._stats:
            self._stats[signal_name].update(value)

    def z_score(self, signal_name: str, value: float) -> float:
        """Z-score of current value relative to agent's own baseline."""
        stats = self._stats.get(signal_name)
        if stats is None:
            return 0.0
        return stats.z_score(value)

    def is_anomalous(self, signal_name: str, value: float, threshold: float = 2.0) -> bool:
        """True if value is >threshold std deviations from agent's baseline."""
        return abs(self.z_score(signal_name, value)) > threshold

    @property
    def sample_count(self) -> int:
        """Minimum observation count across all tracked signals."""
        if not self._stats:
            return 0
        return min(s.count for s in self._stats.values())

    def to_dict(self) -> dict:
        return {name: stats.to_dict() for name, stats in self._stats.items()}

    @classmethod
    def from_dict(cls, d: dict) -> "AgentBehavioralBaseline":
        b = cls()
        for name in cls.TRACKED_SIGNALS:
            if name in d:
                b._stats[name] = WelfordStats.from_dict(d[name])
        return b


# --- Global registry (in-memory, backed by PostgreSQL) ---

_baselines: Dict[str, AgentBehavioralBaseline] = {}

# Strong refs to in-flight save tasks. The event loop only keeps weak refs to
# tasks, so a fire-and-forget create_task() can be GC'd mid-await before the DB
# write completes. Holding the ref until done (then discarding) closes that race.
_save_tasks: set = set()


def get_agent_behavioral_baseline(agent_id: str) -> AgentBehavioralBaseline:
    """Get or create the behavioral baseline for an agent.

    Returns the in-memory cached baseline immediately.  If missing from cache,
    creates a fresh one.  Call ``ensure_baseline_loaded(agent_id)`` first from
    an async context to hydrate from PostgreSQL on cache miss.
    """
    if agent_id not in _baselines:
        _baselines[agent_id] = AgentBehavioralBaseline()
    return _baselines[agent_id]


async def ensure_baseline_loaded(agent_id: str) -> AgentBehavioralBaseline:
    """Load a behavioral baseline from PostgreSQL if not already cached.

    Should be called once when an agent resumes (e.g. at the start of
    ``get_agent_behavioral_baseline`` usage in an async handler).
    """
    if agent_id in _baselines:
        return _baselines[agent_id]

    try:
        from src.db import get_db
        db = get_db()
        stats_dict = await db.load_behavioral_baseline(agent_id)
        if stats_dict:
            _baselines[agent_id] = AgentBehavioralBaseline.from_dict(stats_dict)
            _logger.debug("Loaded behavioral baseline from DB for %s", agent_id)
            return _baselines[agent_id]
    except Exception:
        _logger.debug("Could not load behavioral baseline from DB for %s", agent_id, exc_info=True)

    # Fall through: create fresh baseline
    _baselines[agent_id] = AgentBehavioralBaseline()
    return _baselines[agent_id]


def schedule_baseline_save(agent_id: str) -> None:
    """Fire-and-forget: persist the current baseline to PostgreSQL.

    Safe to call from sync code inside an async event loop.
    """
    baseline = _baselines.get(agent_id)
    if baseline is None:
        return

    async def _do_save():
        try:
            from src.db import get_db
            db = get_db()
            await db.save_behavioral_baseline(agent_id, baseline.to_dict())
        except Exception:
            _logger.debug("Behavioral baseline save failed for %s", agent_id, exc_info=True)

    try:
        loop = asyncio.get_running_loop()
        task = loop.create_task(_do_save())
        _save_tasks.add(task)
        task.add_done_callback(_save_tasks.discard)
    except RuntimeError:
        pass  # No event loop — skip persistence (e.g. tests, CLI)


def compute_anomaly_entropy(
    baseline: AgentBehavioralBaseline,
    signals: Dict[str, Optional[float]],
    threshold: float = 2.0,
    penalty_per_anomaly: float = 0.05,
) -> float:
    """Compute entropy penalty from anomalous signals.

    Returns additional noise_S to add to the ODE update.
    Per-signal z_score already returns 0.0 with insufficient data (<5 samples),
    so anomaly detection is per-signal, not gated on all signals having data.
    """
    penalty = 0.0
    for signal_name, value in signals.items():
        if value is not None and baseline.is_anomalous(signal_name, value, threshold):
            penalty += penalty_per_anomaly
    return penalty
