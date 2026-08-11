"""
Continuity Layer - The Bridge Between Logs

Compares operational and reflective logs to derive grounded metrics.
This is THE GROUNDING MECHANISM that makes EISV meaningful.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Dict, Any, List
import numpy as np
import json

from .operational import OperationalEntry, create_operational_entry
from .reflective import ReflectiveEntry, create_reflective_entry

# Logging
from src.logging_utils import get_logger
logger = get_logger(__name__)


@dataclass
class ContinuityMetrics:
    """
    Derived by comparing operational vs reflective logs.
    This is what grounds the EISV dynamics.
    """
    timestamp: datetime
    agent_id: str
    
    # === Core divergence ===
    derived_complexity: float         # From operational
    self_complexity: Optional[float]  # From reflective (may be None)
    complexity_divergence: float      # |derived - self| or default if no self-report
    
    # === Signals ===
    overconfidence_signal: bool       # High confidence + high derived complexity
    underconfidence_signal: bool      # Low confidence + low derived complexity
    
    # === Grounded EISV inputs ===
    E_input: float  # Activity rate
    I_input: float  # Alignment (inverse of divergence)
    S_input: float  # Uncertainty
    
    # === Calibration ===
    calibration_weight: float = 0.5  # How much to trust this agent

    # === Substrate-portability canaries (observe-only) ===
    # These record, per check-in, whether an input was actually supplied or was
    # silently filled by a default. Nothing below changes the math — they exist
    # so the decision to change the defaults can be made against the live
    # distribution instead of an assumption. See
    # docs/proposals/substrate-portability-checkin-v0.md.
    self_complexity_defaulted: bool = False   # divergence used the 0.2 stand-in
    E_input_clipped: bool = False             # E_input hit a clip bound, carries no signal
    continuity_degenerate: bool = False       # text features can't move for this caller

    def to_dict(self) -> dict:
        """Serialize for storage and API responses."""
        return {
            'timestamp': self.timestamp.isoformat(),
            'agent_id': self.agent_id,
            'derived_complexity': self.derived_complexity,
            'self_complexity': self.self_complexity,
            'complexity_divergence': self.complexity_divergence,
            'overconfidence_signal': self.overconfidence_signal,
            'underconfidence_signal': self.underconfidence_signal,
            'E_input': self.E_input,
            'I_input': self.I_input,
            'S_input': self.S_input,
            'calibration_weight': self.calibration_weight,
            'self_complexity_defaulted': self.self_complexity_defaulted,
            'E_input_clipped': self.E_input_clipped,
            'continuity_degenerate': self.continuity_degenerate,
        }


def derive_complexity(op: OperationalEntry) -> float:
    """
    Derive task complexity from observable operational features.
    
    PURPOSE: Divergence detection, not canonical truth.
    
    This measures OUTPUT complexity (what was produced), not necessarily
    TASK complexity (how hard it was to produce). The formula is heuristic
    and not empirically validated. Weights are defensible but arbitrary.
    
    The value is in CONSISTENCY — divergence patterns between derived and
    self-reported complexity reveal miscalibration, regardless of whether
    the absolute values are "correct".
    
    KNOWN LIMITATIONS:
    - Weights (0.45, 0.30, 0.15, 0.10) are made up
    - Code blocks don't always mean complex (hello world isn't hard)
    - Missing signals: edit patterns, timing, error recovery, context reads
    - structure_factor maxes at 0.5, then scaled by 0.30 → max 0.15 contribution

    NOTE: compute_continuity_metrics() applies a 0.4 floor for high-reasoning
    task types (refactoring, debugging, review, design, research) after calling
    this function.
    
    VALIDATION PATH:
    - Log both derived and reported
    - Analyze divergence distribution over time
    - Correlate with actual outcomes (errors, revisions, user feedback)
    - Adjust weights based on real data
    
    Returns: float in [0, 1]
    """
    
    # === Token factor (log scale, diminishing returns) ===
    # 500 tokens = 0.5, 2000 tokens = 1.0
    if op.response_tokens > 0:
        token_factor = min(1.0, np.log1p(op.response_tokens) / np.log1p(2000))
    else:
        token_factor = 0.0
    
    # === Structural complexity ===
    structure_factor = 0.0
    
    # Code blocks indicate technical/complex work
    if op.has_code_blocks:
        structure_factor += 0.25 * min(1.0, op.code_block_count / 3)
    
    # Many list items = structured breakdown
    if op.list_item_count > 3:
        structure_factor += 0.15 * min(1.0, (op.list_item_count - 3) / 10)
    
    # Multiple paragraphs = multi-part reasoning
    if op.paragraph_count > 2:
        structure_factor += 0.1 * min(1.0, (op.paragraph_count - 2) / 5)
    
    # === Tool complexity ===
    tool_factor = 0.15 * min(1.0, len(op.mentioned_tools) / 4)
    
    # === Question factor ===
    question_factor = 0.05 * min(1.0, op.question_count / 3)
    
    # === Combine with weights ===
    derived = (
        0.45 * token_factor +      # Length matters most
        0.30 * structure_factor +  # Structure indicates depth
        0.15 * tool_factor +       # Tool use indicates coordination
        0.10 * question_factor     # Questions indicate engagement
    )
    
    return float(np.clip(derived, 0.0, 1.0))


def compute_continuity_metrics(
    op: OperationalEntry,
    refl: ReflectiveEntry,
    calibration_weight: float = 0.5
) -> ContinuityMetrics:
    """
    Core grounding function.
    
    Compares operational and reflective logs to produce metrics
    that feed into EISV dynamics.
    """
    
    # Derive complexity from operational features
    derived_complexity = derive_complexity(op)

    # Task types with high cognitive-to-output ratio: the thinking is the work,
    # not the output. A 3-line refactor after reading 2000 lines is not "simple."
    HIGH_REASONING_TASK_TYPES = {'refactoring', 'debugging', 'review', 'design', 'research'}
    if refl.task_type in HIGH_REASONING_TASK_TYPES:
        derived_complexity = max(derived_complexity, 0.4)

    # === Divergence ===
    if refl.self_complexity is not None:
        complexity_divergence = abs(derived_complexity - refl.self_complexity)
        self_complexity_defaulted = False
    else:
        # No self-report: can't compute divergence, use moderate default.
        # NOTE: ProcessAgentUpdateParams.complexity currently defaults to 0.5,
        # so a caller that omits it usually arrives here looking like a caller
        # that reported 0.5, and this branch is rarer than it should be. The
        # flag records which rows had no real self-report so the default can be
        # retired against evidence rather than by assertion.
        complexity_divergence = 0.2
        self_complexity_defaulted = True

    # === Confidence signals ===
    overconfidence = (
        refl.self_confidence is not None and
        refl.self_confidence > 0.8 and
        derived_complexity > 0.6
    )
    
    underconfidence = (
        refl.self_confidence is not None and
        refl.self_confidence < 0.3 and
        derived_complexity < 0.3
    )
    
    # === Grounded EISV inputs ===
    
    # E_input: Activity rate
    #
    # DEGENERACY WARNING: `latency_ms` is the wall-clock gap between check-ins,
    # not generation time, but the /200 normalizer was written for tokens-per-
    # second of *generation*. Clearing the 0.3 floor therefore needs >60 tok/s
    # sustained across the entire idle gap, which essentially never happens —
    # a 2000-token Claude turn 60s after the last check-in scores 33 tok/s and
    # clips; Lumen clips at any interval. In practice this term contributes a
    # constant 0.3, which the behavioral sensor then blends in at 20% as a
    # steady downward pull on E rather than as a measurement. Do not "fix" the
    # constant by picking a new divisor — measure `E_input_clipped` first and
    # decide whether the term earns its place at all.
    if op.latency_ms and op.latency_ms > 0:
        tokens_per_sec = op.response_tokens / (op.latency_ms / 1000)
        E_raw = tokens_per_sec / 200
    else:
        E_raw = 0.5 + 0.3 * (op.response_tokens / 1000)
    E_input = float(np.clip(E_raw, 0.3, 1.0))
    E_input_clipped = not (0.3 < E_raw < 1.0)

    # I_input: Alignment (low divergence = high integrity)
    I_input = float(1.0 - complexity_divergence)
    
    # S_input: Uncertainty from multiple sources
    S_input = float(np.clip(
        0.1 +  # Base uncertainty
        0.5 * complexity_divergence +
        (0.1 if not op.is_session_continuation else 0) +
        (0.1 if refl.self_complexity is None else 0),
        0.0, 1.0
    ))
    
    # Degeneracy: with no markdown structure, no tool mentions and no questions,
    # every term of derive_complexity except the token count is zero, so the
    # "operational" reading collapses to a function of response length alone.
    # For a caller whose response_text is a fixed-shape template (a sensor
    # bridge, a process harness, a tool-call-only turn) that is a constant, and
    # the divergence computed against it is an artifact of the template rather
    # than a miscalibration signal.
    continuity_degenerate = (
        not op.has_code_blocks
        and op.list_item_count == 0
        and op.paragraph_count <= 1
        and op.question_count == 0
        and not op.mentioned_tools
    )

    return ContinuityMetrics(
        timestamp=datetime.now(),
        agent_id=op.agent_id,
        derived_complexity=derived_complexity,
        self_complexity=refl.self_complexity,
        complexity_divergence=complexity_divergence,
        overconfidence_signal=overconfidence,
        underconfidence_signal=underconfidence,
        E_input=E_input,
        I_input=I_input,
        S_input=S_input,
        calibration_weight=calibration_weight,
        self_complexity_defaulted=self_complexity_defaulted,
        E_input_clipped=E_input_clipped,
        continuity_degenerate=continuity_degenerate,
    )


def tool_usage_complexity(
    unique_tools: int = 0,
    total_calls: int = 0,
    error_rate: float = 0.0,
    files_modified: int = 0,
) -> float:
    """Derive complexity estimate from tool usage patterns.

    Independent of text analysis — grounded in what the agent actually did.
    Returns float in [0, 1].
    """
    if total_calls == 0:
        return 0.2  # Minimal baseline

    # Tool diversity: many unique tools = more complex task
    # 1 tool = simple, 5+ tools = complex
    diversity = min(1.0, unique_tools / 5.0)

    # Error rate: errors signal harder problem (capped contribution)
    error_signal = min(1.0, error_rate * 2.0)

    # Files touched: multi-file = higher complexity
    # 1 file = simple, 8+ = complex
    file_signal = min(1.0, files_modified / 8.0)

    # Call volume: more calls = more work (log scale)
    volume = min(1.0, np.log1p(total_calls) / np.log1p(50))

    derived = (
        0.35 * diversity +
        0.15 * error_signal +
        0.25 * file_signal +
        0.25 * volume
    )
    return float(np.clip(derived, 0.0, 1.0))


class ContinuityLayer:
    """
    Manages the dual-log architecture.
    
    This is the main interface for the dual-log system.
    
    Usage:
        layer = ContinuityLayer(agent_id, redis_client)
        metrics = layer.process_update(
            response_text="...",
            self_complexity=0.5,
            self_confidence=0.8,
            client_session_id="abc123"
        )
        # metrics.derived_complexity - server-derived (grounded)
        # metrics.complexity_divergence - |derived - reported|
        # metrics.E_input, I_input, S_input - grounded EISV inputs
    """
    
    # Redis key prefixes
    OP_LOG_PREFIX = "dual_log:op:"
    REFL_LOG_PREFIX = "dual_log:refl:"
    CONTINUITY_PREFIX = "dual_log:cont:"
    STATE_PREFIX = "dual_log:state:"
    
    # Retention settings
    MAX_LOG_ENTRIES = 100  # Per agent
    LOG_TTL_SECONDS = 86400 * 7  # 7 days
    
    def __init__(self, agent_id: str, redis_client=None):
        """
        Args:
            agent_id: The agent this layer manages
            redis_client: Redis client for persistence. If None, uses in-memory.
        """
        self.agent_id = agent_id
        self.redis = redis_client

        # In-memory fallback
        self._memory_storage: Dict[str, Any] = {}

        # Track previous derived complexity for rate-of-change calculation
        self._prev_derived_complexity: Optional[float] = None

        # Load or initialize state
        self._prev_session_id: Optional[str] = None
        self._prev_timestamp: Optional[datetime] = None
        self._prev_topic_hash: Optional[str] = None
        self._load_state()
    
    def _load_state(self):
        """Load previous state from storage."""
        if self.redis:
            try:
                state_key = f"{self.STATE_PREFIX}{self.agent_id}"
                state_data = self.redis.get(state_key)
                if state_data:
                    state = json.loads(state_data)
                    self._prev_session_id = state.get('prev_session_id')
                    if state.get('prev_timestamp'):
                        self._prev_timestamp = datetime.fromisoformat(state['prev_timestamp'])
                    self._prev_topic_hash = state.get('prev_topic_hash')
                    self._prev_derived_complexity = state.get('prev_derived_complexity')
            except Exception as e:
                logger.warning(f"Failed to load dual-log state: {e}")
    
    def _save_state(self):
        """Save current state to storage."""
        if self.redis:
            try:
                state_key = f"{self.STATE_PREFIX}{self.agent_id}"
                state = {
                    'prev_session_id': self._prev_session_id,
                    'prev_timestamp': self._prev_timestamp.isoformat() if self._prev_timestamp else None,
                    'prev_topic_hash': self._prev_topic_hash,
                    'prev_derived_complexity': self._prev_derived_complexity,
                }
                self.redis.setex(state_key, self.LOG_TTL_SECONDS, json.dumps(state))
            except Exception as e:
                logger.warning(f"Failed to save dual-log state: {e}")
    
    def process_update(
        self,
        response_text: str,
        self_complexity: Optional[float] = None,
        self_confidence: Optional[float] = None,
        client_session_id: str = "",
        task_type: Optional[str] = None,
        calibration_weight: float = 0.5,
        tool_usage_stats: Optional[Dict[str, Any]] = None,
    ) -> ContinuityMetrics:
        """
        Process an agent update through dual-log architecture.
        
        This is the main entry point. Call this from process_agent_update.
        
        Args:
            response_text: The response text to analyze
            self_complexity: Agent's self-reported complexity (optional)
            self_confidence: Agent's self-reported confidence (optional)
            client_session_id: Session ID for continuity tracking
            task_type: Optional task type label
            calibration_weight: Trust weight for this agent (0-1)
        
        Returns:
            ContinuityMetrics with grounded EISV inputs
        """
        
        # 1. Create operational entry (server-derived)
        op_entry = create_operational_entry(
            agent_id=self.agent_id,
            response_text=response_text,
            client_session_id=client_session_id,
            prev_session_id=self._prev_session_id,
            prev_timestamp=self._prev_timestamp
        )
        
        # 2. Create reflective entry (agent-reported)
        refl_entry = create_reflective_entry(
            agent_id=self.agent_id,
            complexity=self_complexity,
            confidence=self_confidence,
            task_type=task_type
        )
        
        # 3. Compute continuity metrics (the grounding)
        metrics = compute_continuity_metrics(
            op_entry, refl_entry, calibration_weight
        )

        # 3b. Blend tool-usage-derived complexity when available.
        # This grounds complexity in actual tool call patterns, not just text features.
        if tool_usage_stats:
            tu_complexity = tool_usage_complexity(
                unique_tools=tool_usage_stats.get("unique_tools", 0),
                total_calls=tool_usage_stats.get("total_calls", 0),
                error_rate=tool_usage_stats.get("error_rate", 0.0),
                files_modified=tool_usage_stats.get("files_modified", 0),
            )
            # Blend: 70% text-derived + 30% tool-derived
            metrics.derived_complexity = 0.70 * metrics.derived_complexity + 0.30 * tu_complexity

            # Cross-validate: if self-report diverges from BOTH text and tool derivation,
            # strengthen the divergence signal
            if refl_entry.self_complexity is not None:
                tu_divergence = abs(tu_complexity - refl_entry.self_complexity)
                text_divergence = metrics.complexity_divergence
                if tu_divergence > 0.3 and text_divergence > 0.2:
                    # Both independent derivations disagree with self-report
                    metrics.complexity_divergence = max(
                        metrics.complexity_divergence,
                        (text_divergence + tu_divergence) / 2,
                    )

        # 3c. When no self-reported complexity, use rate-of-change of derived
        # complexity instead of hardcoded 0.2. This gives non-Lumen agents a
        # real signal that varies with actual task changes.
        if refl_entry.self_complexity is None and self._prev_derived_complexity is not None:
            complexity_roc = min(1.0, abs(metrics.derived_complexity - self._prev_derived_complexity))
            metrics.complexity_divergence = complexity_roc
        self._prev_derived_complexity = metrics.derived_complexity

        # 4. Store entries
        self._store_operational(op_entry)
        self._store_reflective(refl_entry)
        self._store_continuity(metrics)
        
        # 5. Update tracking state
        self._prev_session_id = client_session_id
        self._prev_timestamp = op_entry.timestamp
        self._prev_topic_hash = op_entry.topic_hash
        self._save_state()
        
        logger.debug(
            f"Dual-log processed: derived={metrics.derived_complexity:.3f}, "
            f"reported={metrics.self_complexity}, "
            f"divergence={metrics.complexity_divergence:.3f}"
        )
        
        return metrics
    
    def _store_operational(self, entry: OperationalEntry):
        """Store operational entry."""
        if self.redis:
            try:
                key = f"{self.OP_LOG_PREFIX}{self.agent_id}"
                score = entry.timestamp.timestamp()
                self.redis.zadd(key, {json.dumps(entry.to_dict()): score})
                self.redis.expire(key, self.LOG_TTL_SECONDS)
                # Trim to max entries
                self.redis.zremrangebyrank(key, 0, -self.MAX_LOG_ENTRIES - 1)
            except Exception as e:
                logger.warning(f"Failed to store operational entry: {e}")
        else:
            # In-memory fallback
            key = f"op:{self.agent_id}"
            if key not in self._memory_storage:
                self._memory_storage[key] = []
            self._memory_storage[key].append(entry.to_dict())
            self._memory_storage[key] = self._memory_storage[key][-self.MAX_LOG_ENTRIES:]
    
    def _store_reflective(self, entry: ReflectiveEntry):
        """Store reflective entry."""
        if self.redis:
            try:
                key = f"{self.REFL_LOG_PREFIX}{self.agent_id}"
                score = entry.timestamp.timestamp()
                self.redis.zadd(key, {json.dumps(entry.to_dict()): score})
                self.redis.expire(key, self.LOG_TTL_SECONDS)
                self.redis.zremrangebyrank(key, 0, -self.MAX_LOG_ENTRIES - 1)
            except Exception as e:
                logger.warning(f"Failed to store reflective entry: {e}")
        else:
            key = f"refl:{self.agent_id}"
            if key not in self._memory_storage:
                self._memory_storage[key] = []
            self._memory_storage[key].append(entry.to_dict())
            self._memory_storage[key] = self._memory_storage[key][-self.MAX_LOG_ENTRIES:]
    
    def _store_continuity(self, metrics: ContinuityMetrics):
        """Store continuity metrics."""
        if self.redis:
            try:
                key = f"{self.CONTINUITY_PREFIX}{self.agent_id}"
                score = metrics.timestamp.timestamp()
                self.redis.zadd(key, {json.dumps(metrics.to_dict()): score})
                self.redis.expire(key, self.LOG_TTL_SECONDS)
                self.redis.zremrangebyrank(key, 0, -self.MAX_LOG_ENTRIES - 1)
            except Exception as e:
                logger.warning(f"Failed to store continuity metrics: {e}")
        else:
            key = f"cont:{self.agent_id}"
            if key not in self._memory_storage:
                self._memory_storage[key] = []
            self._memory_storage[key].append(metrics.to_dict())
            self._memory_storage[key] = self._memory_storage[key][-self.MAX_LOG_ENTRIES:]
    
    def get_recent_metrics(self, count: int = 10) -> List[dict]:
        """Get recent continuity metrics for this agent."""
        if self.redis:
            try:
                key = f"{self.CONTINUITY_PREFIX}{self.agent_id}"
                entries = self.redis.zrevrange(key, 0, count - 1)
                return [json.loads(e) for e in entries]
            except Exception as e:
                logger.warning(f"Failed to get recent metrics: {e}")
                return []
        else:
            key = f"cont:{self.agent_id}"
            return list(reversed(self._memory_storage.get(key, [])[-count:]))
    
    def get_cumulative_divergence(self, window_count: int = 10) -> float:
        """Get cumulative divergence over recent updates."""
        metrics = self.get_recent_metrics(window_count)
        if not metrics:
            return 0.0
        return sum(m.get('complexity_divergence', 0) for m in metrics)
