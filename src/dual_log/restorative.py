"""
Restorative Balance Monitor

Monitors for overload conditions and suggests slowdown.
From the patent: "restorative balance triggers".

When an agent is updating too frequently or showing high divergence,
this suggests they need to slow down and reflect.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional
import json

from .continuity import ContinuityMetrics

from src.logging_utils import get_logger
logger = get_logger(__name__)


@dataclass
class RestorativeStatus:
    """Status from restorative balance check."""
    needs_restoration: bool
    reason: Optional[str] = None
    suggested_cooldown_seconds: int = 0
    activity_rate: float = 0.0
    cumulative_divergence: float = 0.0
    
    def to_dict(self) -> dict:
        return {
            'needs_restoration': self.needs_restoration,
            'reason': self.reason,
            'suggested_cooldown_seconds': self.suggested_cooldown_seconds,
            'activity_rate': self.activity_rate,
            'cumulative_divergence': self.cumulative_divergence,
        }


class RestorativeBalanceMonitor:
    """
    Monitors for overload conditions and suggests slowdown.
    
    Checks two conditions:
    1. Activity rate - too many updates in a short window
    2. Cumulative divergence - repeated misalignment between logs
    
    Usage:
        monitor = RestorativeBalanceMonitor(agent_id, redis_client)
        monitor.record(continuity_metrics)
        status = monitor.check()
        if status.needs_restoration:
            # Suggest slowdown to agent
    """
    
    # Redis key prefix
    KEY_PREFIX = "dual_log:restorative:"
    
    def __init__(
        self,
        agent_id: str,
        redis_client=None,
        activity_threshold: int = 15,      # Updates per window
        divergence_threshold: float = 0.4,  # Cumulative divergence
        window_seconds: int = 300           # 5 minute window
    ):
        self.agent_id = agent_id
        self.redis = redis_client
        self.activity_threshold = activity_threshold
        self.divergence_threshold = divergence_threshold
        self.window_seconds = window_seconds
        
        # In-memory fallback
        self._timestamps: List[datetime] = []
        self._divergences: List[float] = []
    
    def record(self, metrics: ContinuityMetrics):
        """
        Record a continuity metrics update.
        
        Call this after each process_update from ContinuityLayer.
        """
        now = datetime.now()
        
        if self.redis:
            try:
                key = f"{self.KEY_PREFIX}{self.agent_id}"
                entry = {
                    'timestamp': metrics.timestamp.isoformat(),
                    'divergence': metrics.complexity_divergence
                }
                score = metrics.timestamp.timestamp()
                self.redis.zadd(key, {json.dumps(entry): score})
                
                # Prune old entries
                cutoff = now.timestamp() - self.window_seconds
                self.redis.zremrangebyscore(key, '-inf', cutoff)
                
                # Set TTL
                self.redis.expire(key, self.window_seconds * 2)
            except Exception as e:
                logger.warning(f"Failed to record restorative entry: {e}")
                self._record_memory(metrics)
        else:
            self._record_memory(metrics)
    
    def _record_memory(self, metrics: ContinuityMetrics):
        """In-memory fallback for recording."""
        now = datetime.now()
        cutoff = now.timestamp() - self.window_seconds
        
        # Add new
        self._timestamps.append(metrics.timestamp)
        self._divergences.append(metrics.complexity_divergence)
        
        # Prune old
        valid_indices = [
            i for i, ts in enumerate(self._timestamps)
            if ts.timestamp() > cutoff
        ]
        self._timestamps = [self._timestamps[i] for i in valid_indices]
        self._divergences = [self._divergences[i] for i in valid_indices]
    
    def check(self) -> RestorativeStatus:
        """
        Check if restorative balance is needed.
        
        Returns RestorativeStatus with:
        - needs_restoration: bool
        - reason: explanation if True
        - suggested_cooldown_seconds: how long to wait
        """
        if self.redis:
            activity_rate, cumulative_divergence = self._check_redis()
        else:
            activity_rate = len(self._timestamps)
            cumulative_divergence = sum(self._divergences)
        
        reasons = []
        
        if activity_rate > self.activity_threshold:
            reasons.append(
                f"high activity ({activity_rate} updates in {self.window_seconds}s, "
                f"threshold: {self.activity_threshold})"
            )
        
        if cumulative_divergence > self.divergence_threshold:
            # Descriptive, not interrogative — matches the neutralized mirror
            # complexity-calibration line. The divergence is calibration data,
            # not a demand to justify difficulty. (2026-06-03.)
            reasons.append(
                f"complexity divergence ({cumulative_divergence:.2f} cumulative, "
                f"logged for calibration)"
            )
        
        if reasons:
            # Cooldown scales with severity
            base_cooldown = 30
            activity_penalty = max(0, activity_rate - self.activity_threshold) * 5
            divergence_penalty = max(0, (cumulative_divergence - self.divergence_threshold) * 30)
            cooldown = int(base_cooldown + activity_penalty + divergence_penalty)
            
            return RestorativeStatus(
                needs_restoration=True,
                reason="; ".join(reasons),
                suggested_cooldown_seconds=min(cooldown, 300),  # Cap at 5 minutes
                activity_rate=activity_rate,
                cumulative_divergence=cumulative_divergence
            )
        
        return RestorativeStatus(
            needs_restoration=False,
            activity_rate=activity_rate,
            cumulative_divergence=cumulative_divergence
        )
    
    def _check_redis(self) -> tuple:
        """Check counts from Redis."""
        try:
            key = f"{self.KEY_PREFIX}{self.agent_id}"
            now = datetime.now()
            cutoff = now.timestamp() - self.window_seconds
            
            # Get entries in window
            entries = self.redis.zrangebyscore(key, cutoff, '+inf')
            
            activity_rate = len(entries)
            cumulative_divergence = 0.0
            
            for entry_str in entries:
                try:
                    entry = json.loads(entry_str)
                    cumulative_divergence += entry.get('divergence', 0)
                except:
                    pass
            
            return activity_rate, cumulative_divergence
        except Exception as e:
            logger.warning(f"Failed to check restorative status: {e}")
            return 0, 0.0
    
    def clear(self):
        """Clear recorded data for this agent."""
        if self.redis:
            try:
                key = f"{self.KEY_PREFIX}{self.agent_id}"
                self.redis.delete(key)
            except Exception as e:
                logger.warning(f"Failed to clear restorative data: {e}")
        
        self._timestamps = []
        self._divergences = []
