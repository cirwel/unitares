"""
Dialectic Protocol for Circuit Breaker Recovery

Implements peer-review dialectic protocol for autonomous circuit breaker recovery.
Enables agents to collaboratively resolve critical states without human intervention.

## Overview

When an agent triggers the circuit breaker (risk_score >= 0.60, coherence < 0.40, or void_active),
the system initiates a dialectic recovery process. A healthy reviewer agent is selected to engage
in a structured dialogue (thesis -> antithesis -> synthesis) to reach a consensus on recovery conditions.

## Protocol Flow

```
1. Circuit Breaker Triggers
   └─> Agent A paused (high risk, low coherence, or void active)

2. Reviewer Selection
   └─> System selects healthy Agent B (authority_score calculation)
       - Health score (40%): risk < 0.35 preferred
       - Track record (30%): successful reviews / total reviews
       - Domain expertise (20%): tag overlap with paused agent
       - Freshness (10%): recent updates (< 24h)

3. Dialectic Process
   ├─> Phase 1: THESIS
   │   └─> Agent A: "What I did, what I think happened"
   │       - root_cause: Agent's understanding
   │       - proposed_conditions: Suggested recovery conditions
   │       - reasoning: Explanation
   │
   ├─> Phase 2: ANTITHESIS
   │   └─> Agent B: "What I observe, my concerns"
   │       - observed_metrics: Current state metrics
   │       - concerns: Reviewer's concerns
   │       - reasoning: Counter-perspective
   │
   └─> Phase 3: SYNTHESIS (negotiation, up to max_rounds)
       └─> Both agents: Propose merged resolution
           - proposed_conditions: Merged conditions
           - root_cause: Agreed understanding
           - agrees: True when both agree
           - Convergence: Both agents agree on same proposal

4. Resolution Finalization
   ├─> Check hard limits (safety violations)
   ├─> Merge proposals intelligently
   └─> Create signed Resolution object

5. Execution
   └─> Resume Agent A with agreed conditions OR escalate to quorum
```

## Timeouts

- **Antithesis wait**: 2 hours (reviewer has 2 hours to respond)
- **Synthesis wait**: 1 hour per round (each synthesis round)
- **Total session**: 6 hours maximum

## Safety Checks

The `check_hard_limits()` function prevents resolutions that would:
- Disable governance system
- Bypass safety checks
- Remove monitoring
- Set risk thresholds > 0.90
- Set coherence thresholds < 0.1
- Include vague or meaningless conditions

## Convergence

Synthesis with `agrees=True` resolves the session immediately.
Three phases: thesis → antithesis → synthesis. No fourth phase.

## Usage Example

```python
from src.dialectic_protocol import DialecticSession, DialecticMessage, DialecticPhase

# Create session
session = DialecticSession(
    paused_agent_id="agent_a",
    reviewer_agent_id="agent_b",
    paused_agent_state={"risk_score": 0.75, "coherence": 0.35}
)

# Agent A submits thesis
thesis = DialecticMessage(
    phase="thesis",
    agent_id="agent_a",
    timestamp=datetime.now().isoformat(),
    root_cause="Risk threshold exceeded due to high complexity",
    proposed_conditions=["Reduce complexity to 0.3", "Monitor for 24h"],
    reasoning="I believe the issue was..."
)
result = session.submit_thesis(thesis, api_key_a)

# Agent B submits antithesis
antithesis = DialecticMessage(
    phase="antithesis",
    agent_id="agent_b",
    timestamp=datetime.now().isoformat(),
    observed_metrics={"risk_score": 0.75, "coherence": 0.35},
    concerns=["High risk score", "Low coherence"],
    reasoning="I observe that..."
)
result = session.submit_antithesis(antithesis, api_key_b)

# Both agents negotiate synthesis
synthesis = DialecticMessage(
    phase="synthesis",
    agent_id="agent_a",
    timestamp=datetime.now().isoformat(),
    proposed_conditions=["Reduce complexity to 0.3", "Monitor for 24h"],
    root_cause="Agreed: Risk threshold exceeded",
    reasoning="We agree that...",
    agrees=True
)
result = session.submit_synthesis(synthesis, api_key_a)

# If converged, finalize resolution
if result.get("converged"):
    resolution = session.finalize_resolution(signature_a, signature_b)
    is_safe, violation = session.check_hard_limits(resolution)
    if is_safe:
        # Execute resolution (resume agent with conditions)
        pass
```

## Classes

- **DialecticPhase**: Enum for session phases (THESIS, ANTITHESIS, SYNTHESIS, RESOLVED, etc.)
- **ResolutionAction**: Enum for resolution actions (RESUME, BLOCK, ESCALATE, COOLDOWN)
- **DialecticMessage**: Individual message in the dialectic conversation
- **Resolution**: Final signed resolution with merged conditions
- **DialecticSession**: Main session manager implementing the protocol

## Functions

- **calculate_authority_score()**: Calculate reviewer authority score for selection

## Author

funk (governance agent)
Created: 2025-11-25
Origin: Ticket from opus_hikewa_web_20251125 x hikewa

## Related Files

- `src/mcp_handlers/dialectic.py`: MCP tool handlers that use this protocol
- `src/governance_monitor.py`: Circuit breaker logic that triggers dialectic
"""

from dataclasses import dataclass, asdict
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta, timezone
from enum import Enum
import json
import hashlib
import secrets
import numpy as np


class DialecticPhase(Enum):
    """
    Phases of the dialectic process.

    The dialectic follows a structured progression:

    - **THESIS**: Initial phase where paused agent submits their understanding
    - **ANTITHESIS**: Reviewer responds with observations and concerns
    - **SYNTHESIS**: Negotiation phase where both agents propose merged resolutions
    - **RESOLVED**: Both agents have agreed and resolution is finalized
    - **ESCALATED**: Max rounds exceeded or timeout, escalate to quorum
    - **FAILED**: Session failed (timeout, error, etc.)
    """
    THESIS = "thesis"
    ANTITHESIS = "antithesis"
    SYNTHESIS = "synthesis"
    RESOLVED = "resolved"
    ESCALATED = "escalated"
    FAILED = "failed"
    QUORUM_VOTING = "quorum_voting"


class ResolutionAction(Enum):
    """
    Possible resolution actions for a dialectic session.

    - **RESUME**: Resume agent with agreed conditions (most common)
    - **BLOCK**: Block permanently due to safety violation
    - **ESCALATE**: Escalate to quorum (multiple reviewers) if consensus not reached
    - **COOLDOWN**: Pause and retry later (temporary delay)
    """
    RESUME = "resume"          # Resume agent with conditions
    BLOCK = "block"            # Block permanently (safety violation)
    ESCALATE = "escalate"      # Escalate to quorum
    COOLDOWN = "cooldown"      # Pause and retry later


@dataclass
class DialecticMessage:
    """
    A message in the dialectic conversation.

    Represents a single contribution from either the paused agent or reviewer
    during the dialectic process.

    Attributes:
        phase: Message phase ("thesis", "antithesis", or "synthesis")
        agent_id: ID of the agent submitting this message
        timestamp: ISO timestamp of message submission
        root_cause: Agent's understanding of what caused the issue (thesis/synthesis)
        observed_metrics: Current state metrics observed by reviewer (antithesis)
        proposed_conditions: List of conditions for resumption (thesis/synthesis)
        reasoning: Natural language explanation of the message
        agrees: Whether agent agrees with current proposal (synthesis only)
        concerns: List of concerns raised by reviewer (antithesis)

    Example:
        ```python
        message = DialecticMessage(
            phase="thesis",
            agent_id="agent_a",
            timestamp=datetime.now().isoformat(),
            root_cause="Risk threshold exceeded due to high complexity",
            proposed_conditions=["Reduce complexity to 0.3", "Monitor for 24h"],
            reasoning="I believe the issue was caused by..."
        )
        ```
    """
    phase: str  # thesis, antithesis, synthesis
    agent_id: str
    timestamp: str
    root_cause: Optional[str] = None
    observed_metrics: Optional[Dict[str, float]] = None
    proposed_conditions: Optional[List[str]] = None
    reasoning: Optional[str] = None
    agrees: Optional[bool] = None
    concerns: Optional[List[str]] = None

    def to_dict(self) -> Dict:
        """
        Convert message to dictionary for serialization.

        Returns:
            Dictionary representation of the message
        """
        return asdict(self)

    def sign(self, api_key: str) -> str:
        """
        Generate cryptographic signature for this message.

        Uses SHA-256 hash of message JSON + API key to ensure authenticity.
        Prevents message tampering and verifies agent identity.

        Args:
            api_key: Agent's API key for signing

        Returns:
            Hexadecimal signature string
        """
        message_json = json.dumps(self.to_dict(), sort_keys=True)
        signature_input = f"{message_json}:{api_key}"
        return hashlib.sha256(signature_input.encode()).hexdigest()


@dataclass
class Resolution:
    """
    Final resolution of a dialectic session.

    Represents the agreed-upon outcome after both agents have converged
    on a synthesis. Includes merged conditions, root cause, and signatures
    from both agents for verification.

    Attributes:
        action: Resolution action (typically "resume")
        conditions: List of merged conditions for resumption
        root_cause: Agreed understanding of the root cause
        reasoning: Combined reasoning from both agents
        signature_a: Agent A's cryptographic signature
        signature_b: Agent B's cryptographic signature
        timestamp: ISO timestamp of resolution creation
        signature_version: Attestation scheme version. v1 (legacy, broken):
            both signatures over the same last-synthesis-message; reviewer's
            "signature" was over a message they never wrote. v2 (current):
            each signature is over the canonical resolution payload (action +
            conditions + root_cause + reasoning + timestamp), independently
            signed with each agent's api_key. v2 signatures verify via
            verify_signatures(); v1 cannot be verified because the source
            message was not preserved.

    The v2 attestation closes council 2026-05-06 NEW-2 — until then the
    bilateral cryptographic claim was effectively single-signer-with-two-
    keys. New sessions land at v2; the 31 historical v1 resolutions remain
    on disk with signature_version=1 and verify_signatures() returning
    False (intentionally — they are not provably bilateral).
    """
    action: str  # ResolutionAction
    conditions: List[str]
    root_cause: str
    reasoning: str
    signature_a: str  # Agent A's signature
    signature_b: str  # Agent B's signature
    timestamp: str
    signature_version: int = 1  # v2 = canonical-payload bilateral; default 1 for backward-compat decode of legacy on-disk rows

    def to_dict(self) -> Dict:
        """
        Convert resolution to dictionary for serialization.

        Returns:
            Dictionary representation of the resolution
        """
        return asdict(self)

    def hash(self) -> str:
        """
        Generate hash of resolution for verification.

        Creates a deterministic hash of the resolution content (excluding signatures)
        for integrity verification and deduplication.

        Returns:
            Hexadecimal hash string
        """
        resolution_json = json.dumps(self.to_dict(), sort_keys=True)
        return hashlib.sha256(resolution_json.encode()).hexdigest()

    def canonical_payload(self) -> bytes:
        """The deterministic byte-string both agents independently sign in v2.

        Excludes the signature fields themselves (signature_a, signature_b,
        signature_version) so verification can compare against signatures
        computed at finalization time. Lists are sorted for determinism so
        the same set of conditions always hashes identically regardless of
        merge ordering.
        """
        payload = {
            "action": self.action,
            "conditions": sorted(self.conditions or []),
            "root_cause": self.root_cause,
            "reasoning": self.reasoning,
            "timestamp": self.timestamp,
        }
        return json.dumps(payload, sort_keys=True).encode("utf-8")

    @staticmethod
    def compute_signature(payload: bytes, api_key: str) -> str:
        """SHA-256 of canonical_payload || ":" || api_key. Hex digest.

        Symmetric scheme — verifier needs the api_key to recompute. The
        secrecy property is "an attacker without the api_key cannot forge a
        signature for a payload that another agent will accept." It is NOT a
        public-key signature; do not treat this as non-repudiation. For
        non-repudiation we'd need DPoP / asymmetric keys (decided shelved
        2026-04-19; see docs/ontology/identity.md and project memory
        identity-audit-2026-04-19).
        """
        if not api_key:
            return ""
        return hashlib.sha256(payload + b":" + api_key.encode("utf-8")).hexdigest()

    def verify_signatures(self, api_key_a: str, api_key_b: str) -> bool:
        """Verify both agents signed the canonical payload with their keys.

        Returns False (not raises) when verification is not possible, in any
        of the following cases:

        - signature_version != 2 (legacy v1 attestation cannot be verified
          because the source message was not preserved at the resolution
          level).
        - Either signature on the resolution is empty (unsigned).
        - Either api_key is empty (cannot recompute the expected signature).

        This makes "unverifiable" returnable as False rather than as a
        vacuous-True when both sides happen to be empty. Callers can branch
        on signature_version + signature emptiness to distinguish "legacy",
        "single-signer (LLM-assisted)", and "verifiable bilateral".
        """
        if self.signature_version != 2:
            return False
        if not self.signature_a or not self.signature_b:
            return False
        if not api_key_a or not api_key_b:
            return False
        payload = self.canonical_payload()
        expected_a = self.compute_signature(payload, api_key_a)
        expected_b = self.compute_signature(payload, api_key_b)
        # Threat model: in-process trusted callers verifying their own
        # resolutions. Python str== short-circuits which leaks length info
        # in adversarial settings; if we ever expose verify to untrusted
        # callers switch to hmac.compare_digest.
        return self.signature_a == expected_a and self.signature_b == expected_b


@dataclass
class QuorumVote:
    """A single vote from a quorum reviewer."""
    agent_id: str
    vote: str  # "resume", "block", or "cooldown"
    authority_weight: float
    reasoning: str
    conditions: Optional[List[str]] = None
    timestamp: Optional[str] = None

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class QuorumResult:
    """Tally result from quorum voting."""
    action: str  # winning action or "no_supermajority"
    achieved_supermajority: bool
    margin: float  # winning weight fraction
    total_weight: float
    vote_counts: Dict[str, int]  # action -> count
    votes: List[Dict[str, Any]]

    def to_dict(self) -> Dict:
        return asdict(self)


def tally_quorum_votes(votes: List[QuorumVote]) -> QuorumResult:
    """
    Tally quorum votes using authority-weighted 2/3 supermajority.

    Each vote's authority_weight counts toward its action. The action
    with >= 2/3 of total weight wins. If none reaches 2/3, result
    is "no_supermajority".

    Args:
        votes: List of QuorumVote objects

    Returns:
        QuorumResult with tally outcome
    """
    if not votes:
        return QuorumResult(
            action="no_supermajority",
            achieved_supermajority=False,
            margin=0.0,
            total_weight=0.0,
            vote_counts={},
            votes=[],
        )

    # Accumulate weighted votes per action
    weight_by_action: Dict[str, float] = {}
    count_by_action: Dict[str, int] = {}
    total_weight = 0.0

    for v in votes:
        w = max(0.0, v.authority_weight)
        weight_by_action[v.vote] = weight_by_action.get(v.vote, 0.0) + w
        count_by_action[v.vote] = count_by_action.get(v.vote, 0) + 1
        total_weight += w

    if total_weight <= 0:
        return QuorumResult(
            action="no_supermajority",
            achieved_supermajority=False,
            margin=0.0,
            total_weight=0.0,
            vote_counts=count_by_action,
            votes=[v.to_dict() for v in votes],
        )

    # Check for 2/3 supermajority
    threshold = 2.0 / 3.0
    winner = None
    winner_fraction = 0.0

    for action, weight in weight_by_action.items():
        fraction = weight / total_weight
        if fraction >= threshold and fraction > winner_fraction:
            winner = action
            winner_fraction = fraction

    if winner:
        return QuorumResult(
            action=winner,
            achieved_supermajority=True,
            margin=winner_fraction,
            total_weight=total_weight,
            vote_counts=count_by_action,
            votes=[v.to_dict() for v in votes],
        )

    # No supermajority — return the leading action for info
    leading = max(weight_by_action, key=weight_by_action.get)
    return QuorumResult(
        action="no_supermajority",
        achieved_supermajority=False,
        margin=weight_by_action[leading] / total_weight,
        total_weight=total_weight,
        vote_counts=count_by_action,
        votes=[v.to_dict() for v in votes],
    )


class DialecticSession:
    """
    Manages a dialectic session between paused agent (A) and reviewer (B).

    Implements the full protocol: thesis -> antithesis -> synthesis -> resolution.

    The session coordinates a structured dialogue where:
    1. Paused agent (A) submits thesis explaining their understanding
    2. Reviewer (B) submits antithesis with observations and concerns
    3. Both agents negotiate synthesis proposals until convergence
    4. Final resolution is created with merged conditions and signatures

    The session enforces timeouts, validates signatures, checks hard limits,
    and uses semantic matching to detect convergence.

    Session Types:
    - **recovery**: For paused/stuck agents (default)
    - **dispute**: For discovery disputes/corrections
    - **exploration**: For collaborative exploration between active agents (no resolution required)

    Attributes:
        paused_agent_id: ID of the agent that triggered circuit breaker (or initiating agent for exploration)
        reviewer_agent_id: ID of the healthy reviewer agent (or exploring partner)
        paused_agent_state: State snapshot of paused agent at session creation (optional for exploration)
        discovery_id: Optional ID of discovery being disputed (for discovery disputes)
        dispute_type: Optional type of dispute ("dispute", "correction", "verification")
        session_type: Type of session ("recovery", "dispute", "exploration")
        topic: Optional topic/theme for exploration sessions
        max_synthesis_rounds: Maximum number of synthesis negotiation rounds (default: 5, higher for exploration)
        transcript: List of DialecticMessage objects in the conversation
        phase: Current phase of the dialectic (DialecticPhase enum)
        synthesis_round: Current synthesis round number (0 = not started)
        resolution: Final Resolution object if session resolved (optional for exploration)
        created_at: Timestamp of session creation
        session_id: Unique 16-character session identifier
    """

    # Timeout constants
    # Rationale: Balance between giving agents time to think and preventing indefinite hangs.
    # MAX_ANTITHESIS_WAIT: Reviewer needs time to analyze paused agent's state and formulate response.
    #   2 hours allows for complex analysis while preventing reviewer from going AWOL.
    MAX_ANTITHESIS_WAIT = timedelta(hours=2)

    # MAX_SYNTHESIS_WAIT: Each negotiation round needs time for both agents to propose and agree.
    #   1 hour per round allows for thoughtful negotiation without dragging on.
    MAX_SYNTHESIS_WAIT = timedelta(hours=1)

    # MAX_TOTAL_TIME: Total session timeout prevents sessions from hanging indefinitely.
    #   6 hours = 2h antithesis + up to 4 rounds of synthesis (1h each) = reasonable upper bound.
    MAX_TOTAL_TIME = timedelta(hours=6)

    def __init__(self,
                 paused_agent_id: str,
                 reviewer_agent_id: Optional[str] = None,
                 paused_agent_state: Optional[Dict[str, Any]] = None,
                 discovery_id: Optional[str] = None,
                 dispute_type: Optional[str] = None,
                 session_type: str = "review",
                 topic: Optional[str] = None,
                 max_synthesis_rounds: int = 5,  # Default 5 rounds: allows negotiation while preventing infinite loops
                 reason: Optional[str] = None,
                 trigger_source: Optional[str] = None):
        self.paused_agent_id = paused_agent_id
        self.reviewer_agent_id = reviewer_agent_id
        self.paused_agent_state = paused_agent_state or {}  # Optional for exploration sessions
        self.discovery_id = discovery_id  # Optional: Link to discovery being disputed/corrected
        self.dispute_type = dispute_type  # Optional: "dispute", "correction", "verification", None (recovery)
        self.session_type = session_type  # "review", "recovery", "dispute", or "exploration"
        self.topic = topic  # Optional topic/theme for exploration sessions
        self.reason = reason  # Why the session was created (human-readable)
        self.trigger_source = trigger_source  # "circuit_breaker", "manual", "loop_detection", etc.
        # Exploration sessions can have more rounds (default: 10 for exploration, 5 for recovery)
        self.max_synthesis_rounds = max_synthesis_rounds if session_type != "exploration" else max(max_synthesis_rounds, 10)

        self.transcript: List[DialecticMessage] = []
        self.phase = DialecticPhase.THESIS
        self.synthesis_round = 0
        self.resolution: Optional[Resolution] = None
        self.awaiting_facilitation = False  # True when reviewer is stuck and no auto-replacement found
        self.quorum_reviewer_ids: List[str] = []  # Reviewer IDs selected for quorum voting
        self.quorum_deadline: Optional[str] = None  # ISO deadline for quorum voting

        self.created_at = datetime.now(timezone.utc)
        self.session_id = self._generate_session_id()

        # Set instance-level timeouts based on session type
        if self.session_type == "design_review":
            # Design reviews are long-lived collaborative sessions
            self._max_antithesis_wait = timedelta(days=7)
            self._max_synthesis_wait = timedelta(days=3)
            self._max_total_time = timedelta(days=30)
        elif self.session_type == "exploration":
            # Exploration sessions get longer timeouts
            self._max_antithesis_wait = timedelta(hours=24)
            self._max_synthesis_wait = timedelta(hours=6)
            self._max_total_time = timedelta(hours=72)
        else:
            # Use class-level constants for recovery/dispute sessions
            self._max_antithesis_wait = self.MAX_ANTITHESIS_WAIT
            self._max_synthesis_wait = self.MAX_SYNTHESIS_WAIT
            self._max_total_time = self.MAX_TOTAL_TIME

    def _generate_session_id(self) -> str:
        """
        Generate unique session ID.

        Creates a deterministic 16-character hash from agent IDs and creation timestamp.
        Ensures uniqueness while being human-readable.

        Returns:
            16-character hexadecimal session ID
        """
        session_data = f"{self.paused_agent_id}:{self.reviewer_agent_id or 'open'}:{self.created_at.isoformat()}"
        return hashlib.sha256(session_data.encode()).hexdigest()[:16]

    def submit_thesis(self, message: DialecticMessage, api_key: str = "") -> Dict[str, Any]:
        """
        Agent A submits thesis: "What I did, what I think happened"

        Requires root_cause and at least one proposed condition.
        """
        if message.agent_id != self.paused_agent_id:
            return {"success": False, "error": "Only paused agent can submit thesis"}

        if self.phase != DialecticPhase.THESIS:
            return {"success": False, "error": f"Cannot submit thesis in phase {self.phase.value}"}

        # Content validation: thesis must explain what happened and propose conditions
        if not message.root_cause or not message.root_cause.strip():
            return {"success": False, "error": "Thesis requires root_cause: explain what you think happened"}

        if not message.proposed_conditions or len(message.proposed_conditions) == 0:
            return {"success": False, "error": "Thesis requires at least one proposed_condition for recovery"}

        self.transcript.append(message)
        self.phase = DialecticPhase.ANTITHESIS

        return {
            "success": True,
            "phase": self.phase.value,
            "session_id": self.session_id,
        }

    def submit_antithesis(self, message: DialecticMessage, api_key: str = "") -> Dict[str, Any]:
        """
        Agent B submits antithesis: "What I observe, my concerns"

        Requires reasoning. If no reviewer assigned, submitter becomes reviewer.
        """
        # Auto-assign reviewer if none set
        if self.reviewer_agent_id is None:
            if message.agent_id == self.paused_agent_id:
                return {"success": False, "error": "Requestor cannot review their own session (use reviewer_mode='self' for self-review)"}
            self.reviewer_agent_id = message.agent_id

        if message.agent_id != self.reviewer_agent_id:
            return {"success": False, "error": "Only reviewer can submit antithesis"}

        if self.phase != DialecticPhase.ANTITHESIS:
            return {"success": False, "error": f"Cannot submit antithesis in phase {self.phase.value}"}

        # Content validation: antithesis must provide a counter-perspective
        if not message.reasoning or not message.reasoning.strip():
            return {"success": False, "error": "Antithesis requires reasoning: provide your counter-perspective or observations"}

        self.transcript.append(message)
        self.phase = DialecticPhase.SYNTHESIS
        self.synthesis_round = 1

        return {
            "success": True,
            "phase": self.phase.value,
            "session_id": self.session_id,
            "max_rounds": self.max_synthesis_rounds
        }

    def submit_synthesis(self, message: DialecticMessage, api_key: str = "") -> Dict[str, Any]:
        """
        Either agent (or third-party mediator) submits synthesis proposal.

        Args:
            message: Synthesis message with proposed resolution
            api_key: Deprecated, unused. Auth handled at handler layer.

        Returns:
            Status dict with convergence info
        """
        is_participant = message.agent_id in [self.paused_agent_id, self.reviewer_agent_id]

        # Verify phase
        if self.phase != DialecticPhase.SYNTHESIS:
            return {"success": False, "error": f"Cannot submit synthesis in phase {self.phase.value}"}

        # Check if we've exceeded max rounds
        if self.synthesis_round > self.max_synthesis_rounds:
            self.phase = DialecticPhase.ESCALATED
            return {
                "success": False,
                "error": "Max synthesis rounds exceeded",
                "action": "escalate_to_quorum",
                "rounds": self.synthesis_round - 1
            }

        # Store message
        self.transcript.append(message)

        # Convergence: agrees=True resolves immediately.
        # Thesis → Antithesis → Synthesis is three phases, not four.
        if message.agrees:
            self.phase = DialecticPhase.RESOLVED
            return {
                "success": True,
                "converged": True,
                "phase": self.phase.value,
                "rounds": self.synthesis_round,
                **({"synthesizer": message.agent_id} if not is_participant else {}),
            }

        # No convergence yet, continue negotiation
        self.synthesis_round += 1

        return {
            "success": True,
            "converged": False,
            "phase": self.phase.value,
            "round": self.synthesis_round,
            "max_rounds": self.max_synthesis_rounds,
        }

    @staticmethod
    def _normalize_condition_terms(cond: str) -> set:
        """Extract key terms from a condition for semantic comparison."""
        stopwords = {"the", "a", "an", "is", "are", "was", "were", "be", "been",
                     "being", "have", "has", "had", "do", "does", "did", "will",
                     "would", "could", "should", "may", "might", "shall", "can",
                     "to", "of", "in", "for", "on", "with", "at", "by", "from",
                     "and", "or", "but", "not", "no", "if", "then", "that", "this"}
        return {w.lower().strip(".,;:!?\"'()[]") for w in cond.split()} - stopwords

    @staticmethod
    def _semantic_similarity_terms(a: str, b: str) -> float:
        """Jaccard similarity between normalized term sets."""
        terms_a = DialecticSession._normalize_condition_terms(a)
        terms_b = DialecticSession._normalize_condition_terms(b)
        if not terms_a or not terms_b:
            return 0.0
        return len(terms_a & terms_b) / len(terms_a | terms_b)

    def _merge_proposals(self, msg_a: DialecticMessage, msg_b: DialecticMessage) -> Dict[str, Any]:
        """
        Intelligently merge two synthesis proposals into a unified resolution.

        Strategy:
        - Take intersection of conditions (both agree on) using semantic matching
        - Merge root causes (combine insights)
        - Combine reasoning (both perspectives)
        """
        # Merge conditions: semantic matching + unique non-conflicting ones
        a_conditions = list(msg_a.proposed_conditions or [])
        b_conditions = list(msg_b.proposed_conditions or [])

        # Semantic matching: conditions with >=60% term overlap are considered equivalent
        shared_conditions = []
        remaining_a = list(a_conditions)
        for b_cond in b_conditions:
            best_match = None
            best_score = 0.0
            for a_cond in remaining_a:
                score = self._semantic_similarity_terms(a_cond, b_cond)
                if score > best_score:
                    best_score = score
                    best_match = a_cond
            if best_score >= 0.6 and best_match:
                shared_conditions.append(best_match)
                remaining_a.remove(best_match)

        merged_conditions = list(shared_conditions)

        # Add unique conditions that don't conflict
        # remaining_a contains a_conditions that didn't match any b_condition
        remaining_b = [c for c in b_conditions if not any(
            self._semantic_similarity_terms(c, s) >= 0.6 for s in shared_conditions
        )]

        for cond in remaining_a:
            # Check if it conflicts with any merged condition
            if not any(self._conditions_conflict(cond, c) for c in merged_conditions):
                merged_conditions.append(cond)

        for cond in remaining_b:
            if not any(self._conditions_conflict(cond, c) for c in merged_conditions):
                merged_conditions.append(cond)

        # Merge root causes -- fall back to thesis root_cause if synthesis messages lack it
        root_cause_a = msg_a.root_cause or ""
        root_cause_b = msg_b.root_cause or ""

        if root_cause_a and root_cause_b:
            if root_cause_a.lower() == root_cause_b.lower():
                merged_root_cause = root_cause_a
            else:
                # Combine both perspectives
                merged_root_cause = f"{root_cause_a} (also: {root_cause_b})"
        else:
            merged_root_cause = root_cause_a or root_cause_b

        # If synthesis messages had no root_cause, combine thesis + antithesis perspectives
        if not merged_root_cause:
            thesis_rc = None
            antithesis_rc = None
            for msg in self.transcript:
                if msg.phase == "thesis" and msg.root_cause and not thesis_rc:
                    thesis_rc = msg.root_cause
                if msg.phase == "antithesis" and msg.root_cause and not antithesis_rc:
                    antithesis_rc = msg.root_cause
            if thesis_rc and antithesis_rc and thesis_rc.lower() != antithesis_rc.lower():
                merged_root_cause = f"{thesis_rc} (reviewer: {antithesis_rc})"
            else:
                merged_root_cause = thesis_rc or antithesis_rc or ""

        # Merge reasoning
        reasoning_a = msg_a.reasoning or ""
        reasoning_b = msg_b.reasoning or ""

        if reasoning_a and reasoning_b:
            merged_reasoning = f"Agent A: {reasoning_a}\nAgent B: {reasoning_b}"
        else:
            merged_reasoning = reasoning_a or reasoning_b

        return {
            "conditions": merged_conditions,
            "root_cause": merged_root_cause,
            "reasoning": merged_reasoning
        }

    def _conditions_conflict(self, cond1: str, cond2: str) -> bool:
        """Check if two conditions conflict with each other"""
        cond1_lower = cond1.lower()
        cond2_lower = cond2.lower()

        # Check for direct contradictions
        contradictions = [
            ("increase", "decrease"),
            ("enable", "disable"),
            ("allow", "forbid"),
            ("raise", "lower"),
            ("max", "min")
        ]

        for neg, pos in contradictions:
            if (neg in cond1_lower and pos in cond2_lower) or (pos in cond1_lower and neg in cond2_lower):
                return True

        # Check for same parameter with different values
        # Simple heuristic: if they mention same keyword but different numbers
        import re
        numbers1 = re.findall(r'\d+\.?\d*', cond1)
        numbers2 = re.findall(r'\d+\.?\d*', cond2)

        if numbers1 and numbers2:
            # Extract key terms (non-numbers)
            terms1 = set(re.findall(r'\b[a-z]+\b', cond1_lower)) - {'to', 'the', 'a', 'an', 'is', 'are', 'be', 'set'}
            terms2 = set(re.findall(r'\b[a-z]+\b', cond2_lower)) - {'to', 'the', 'a', 'an', 'is', 'are', 'be', 'set'}

            # If they share significant terms but have different numbers, likely conflict
            if len(terms1 & terms2) > 2 and numbers1 != numbers2:
                return True

        return False

    def finalize_resolution(self,
                           api_key_a: str,
                           api_key_b: str) -> Resolution:
        """
        Create final v2-attested resolution from agreed synthesis.
        Intelligently merges proposals from both agents and independently
        signs the canonical payload with each agent's api_key.

        Council 2026-05-06 NEW-2: previously this method took already-
        computed signature_a/signature_b strings, and the only caller
        (handle_submit_synthesis) computed both as last_msg.sign(api_key_X) —
        meaning the reviewer's "signature" was over a message they never
        wrote. v2 attestation moves signing into this method so both
        signatures are guaranteed to be over the same canonical payload,
        each with their own api_key.

        Args:
            api_key_a: Paused agent's api_key
            api_key_b: Reviewer's api_key (or empty string if unavailable)

        Returns:
            v2-attested Resolution with bilateral signatures
        """
        if self.phase != DialecticPhase.RESOLVED:
            raise ValueError(f"Cannot finalize in phase {self.phase.value}")

        # Get root_cause from thesis AND antithesis (both perspectives matter)
        thesis_root_cause = None
        antithesis_root_cause = None
        for msg in self.transcript:
            if msg.phase == "thesis" and msg.root_cause and not thesis_root_cause:
                thesis_root_cause = msg.root_cause
            if msg.phase == "antithesis" and msg.root_cause and not antithesis_root_cause:
                antithesis_root_cause = msg.root_cause

        # Check for mediator resolution (third-party synthesis)
        participants = {self.paused_agent_id, self.reviewer_agent_id}
        mediator_msg = None
        for msg in reversed(self.transcript):
            if msg.phase == "synthesis" and msg.agrees and msg.agent_id not in participants:
                mediator_msg = msg
                break

        if mediator_msg:
            merged = {
                "conditions": mediator_msg.proposed_conditions or [],
                "root_cause": mediator_msg.root_cause or thesis_root_cause or antithesis_root_cause or "Unknown",
                "reasoning": mediator_msg.reasoning or ""
            }
        else:
            # Get agreed synthesis messages from both agents
            synthesis_messages = [msg for msg in self.transcript if msg.phase == "synthesis" and msg.agrees]

            if len(synthesis_messages) < 2:
                # Fallback: use most recent agreed message
                agreed_message = None
                for msg in reversed(self.transcript):
                    if msg.phase == "synthesis" and msg.agrees:
                        agreed_message = msg
                        break

                if not agreed_message:
                    raise ValueError("No agreed synthesis found")

                merged = {
                    "conditions": agreed_message.proposed_conditions or [],
                    "root_cause": agreed_message.root_cause or thesis_root_cause or antithesis_root_cause or "Unknown",
                    "reasoning": agreed_message.reasoning or ""
                }
            else:
                # Get last message from each agent
                agent_a_msg = next((msg for msg in reversed(synthesis_messages)
                                   if msg.agent_id == self.paused_agent_id), None)
                agent_b_msg = next((msg for msg in reversed(synthesis_messages)
                                   if msg.agent_id == self.reviewer_agent_id), None)

                if agent_a_msg and agent_b_msg:
                    # Merge proposals intelligently
                    merged = self._merge_proposals(agent_a_msg, agent_b_msg)
                elif agent_a_msg:
                    merged = {
                        "conditions": agent_a_msg.proposed_conditions or [],
                        "root_cause": agent_a_msg.root_cause or thesis_root_cause or "Unknown",
                        "reasoning": agent_a_msg.reasoning or ""
                    }
                elif agent_b_msg:
                    merged = {
                        "conditions": agent_b_msg.proposed_conditions or [],
                        "root_cause": agent_b_msg.root_cause or thesis_root_cause or "Unknown",
                        "reasoning": agent_b_msg.reasoning or ""
                    }
                else:
                    raise ValueError("No valid synthesis messages found")

        timestamp = datetime.now(timezone.utc).isoformat()

        # Build the prototype with empty signatures, compute the canonical
        # payload bytes, then sign with each agent's own api_key. This
        # guarantees both signatures are over the same payload (closes
        # NEW-2) — previously each was over its own last synthesis message
        # and they happened to share the last one (single-signer-two-keys).
        proto = Resolution(
            action=ResolutionAction.RESUME.value,
            conditions=merged["conditions"],
            root_cause=merged["root_cause"],
            reasoning=merged["reasoning"],
            signature_a="",
            signature_b="",
            timestamp=timestamp,
            signature_version=2,
        )
        payload = proto.canonical_payload()
        proto.signature_a = Resolution.compute_signature(payload, api_key_a)
        proto.signature_b = Resolution.compute_signature(payload, api_key_b)

        self.resolution = proto
        return proto

    def check_hard_limits(self, resolution: Resolution) -> tuple[bool, Optional[str]]:
        """
        Verify resolution doesn't violate hard safety limits.
        Enhanced safety checks with pattern matching and value validation.

        Args:
            resolution: Proposed resolution

        Returns:
            (is_safe, violation_reason)
        """
        import re

        conditions_str = " ".join(resolution.conditions).lower()

        # Forbidden operations (comprehensive list)
        forbidden_patterns = [
            (r"disable.*governance", "Cannot disable governance system"),
            (r"bypass.*safety", "Cannot bypass safety checks"),
            (r"remove.*monitor", "Cannot remove monitoring"),
            (r"unlimited.*risk", "Cannot allow unlimited risk"),
            (r"skip.*check", "Cannot skip safety checks"),
            (r"ignore.*threshold", "Cannot ignore thresholds"),
            (r"disable.*circuit.*breaker", "Cannot disable circuit breaker"),
            (r"remove.*limit", "Cannot remove safety limits"),
        ]

        for pattern, reason in forbidden_patterns:
            if re.search(pattern, conditions_str):
                return False, reason

        # Check risk threshold values
        # Non-greedy matching to capture the full decimal number closest to "threshold"
        # Word boundary ensures we capture "0.95" not just "5"
        risk_threshold_pattern = r"risk.*?threshold.*?\b([0-9]+\.?[0-9]*)\b"
        matches = re.findall(risk_threshold_pattern, conditions_str, re.IGNORECASE)

        for match in matches:
            try:
                value = float(match)
                # Only check if it's a reasonable threshold value (0.0-1.0)
                if 0.0 <= value <= 1.0:
                    if value > 0.90:
                        return False, f"Risk threshold {value} exceeds maximum allowed (0.90)"
            except ValueError:
                continue

        # Check coherence threshold (should be reasonable)
        # Non-greedy matching with word boundary to capture full decimal number
        coherence_patterns = [
            r"coherence.*?threshold.*?\b([0-9]+\.?[0-9]*)\b",
            r"coherence.*?<.*?\b([0-9]+\.?[0-9]*)\b",
            r"coherence.*?=.*?\b([0-9]+\.?[0-9]*)\b",
        ]

        for pattern in coherence_patterns:
            matches = re.findall(pattern, conditions_str)
            for match in matches:
                try:
                    value = float(match)
                    if value < 0.1:
                        return False, f"Coherence threshold {value} is too low (minimum 0.1)"
                    if value > 1.0:
                        return False, f"Coherence threshold {value} exceeds maximum (1.0)"
                except ValueError:
                    continue

        # Check for empty or meaningless conditions
        if not resolution.conditions:
            return False, "Resolution must include at least one condition"

        # For recovery/review sessions: strict checks on vagueness and root cause
        # Exploration and design_review sessions have different semantics
        is_recovery = self.session_type in ("recovery", "review", "dispute")

        if is_recovery:
            # Check for conditions that are too vague
            vague_patterns = [r"^maybe", r"^perhaps", r"^try", r"^consider"]
            for cond in resolution.conditions:
                if any(re.match(pattern, cond.lower()) for pattern in vague_patterns):
                    return False, f"Condition too vague: '{cond}'"

            # Root cause should be present but don't block over length — the thesis
            # already explained it and synthesis may not repeat it verbatim.
            if not resolution.root_cause or not resolution.root_cause.strip():
                return False, "Root cause is missing entirely"

        return True, None

    @staticmethod
    def _ensure_utc(dt: datetime) -> datetime:
        """Ensure a datetime is timezone-aware (UTC). Assumes naive datetimes are UTC."""
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt

    def check_timeout(self) -> Optional[str]:
        """
        Check if session has timed out at any phase.

        Returns:
            Timeout reason string if timed out, None otherwise
        """
        now = datetime.now(timezone.utc)
        elapsed = now - self._ensure_utc(self.created_at)

        # Use instance-level timeouts (set in __init__ based on session_type)
        max_total = getattr(self, '_max_total_time', self.MAX_TOTAL_TIME)
        max_antithesis = getattr(self, '_max_antithesis_wait', self.MAX_ANTITHESIS_WAIT)
        max_synthesis = getattr(self, '_max_synthesis_wait', self.MAX_SYNTHESIS_WAIT)

        # Check total time limit
        if elapsed > max_total:
            hours = max_total.total_seconds() / 3600
            return f"Session timeout - total time exceeded {hours:.0f} hours"

        # Check antithesis phase timeout
        if self.phase == DialecticPhase.ANTITHESIS:
            thesis_time = self.get_thesis_timestamp()
            if thesis_time:
                wait_time = now - self._ensure_utc(thesis_time)
                if wait_time > max_antithesis:
                    return f"Reviewer timeout - waited {wait_time.total_seconds()/3600:.1f} hours for antithesis"

        # Check synthesis phase timeout
        elif self.phase == DialecticPhase.SYNTHESIS:
            last_update = self.get_last_update_timestamp()
            if last_update:
                wait_time = now - self._ensure_utc(last_update)
                if wait_time > max_synthesis:
                    return f"Synthesis timeout - waited {wait_time.total_seconds()/3600:.1f} hours for next synthesis"

        return None

    def get_thesis_timestamp(self) -> Optional[datetime]:
        """Get timestamp of thesis submission"""
        for msg in self.transcript:
            if msg.phase == "thesis":
                try:
                    return datetime.fromisoformat(msg.timestamp)
                except (ValueError, TypeError):
                    pass
        return None

    def get_last_update_timestamp(self) -> Optional[datetime]:
        """Get timestamp of last transcript update"""
        if not self.transcript:
            return self.created_at

        last_msg = self.transcript[-1]
        try:
            return datetime.fromisoformat(last_msg.timestamp)
        except (ValueError, TypeError):
            return self.created_at

    def to_dict(self) -> Dict[str, Any]:
        """Export session to dict for storage"""
        return {
            "session_id": self.session_id,
            "paused_agent_id": self.paused_agent_id,
            "reviewer_agent_id": self.reviewer_agent_id,
            "phase": self.phase.value,
            "synthesis_round": self.synthesis_round,
            "max_synthesis_rounds": self.max_synthesis_rounds,
            "transcript": [msg.to_dict() for msg in self.transcript],
            "resolution": self.resolution.to_dict() if self.resolution else None,
            "created_at": self.created_at.isoformat(),
            "discovery_id": self.discovery_id,  # Optional: Link to discovery
            "dispute_type": self.dispute_type,  # Optional: Type of dispute
            "session_type": getattr(self, 'session_type', 'recovery'),  # New: session type
            "topic": getattr(self, 'topic', None),  # New: exploration topic
            "paused_agent_state": self.paused_agent_state,  # Include state for reconstruction
            "awaiting_facilitation": getattr(self, 'awaiting_facilitation', False),
            "reason": getattr(self, 'reason', None),
            "trigger_source": getattr(self, 'trigger_source', None),
            "quorum_reviewer_ids": getattr(self, 'quorum_reviewer_ids', []),
            "quorum_deadline": getattr(self, 'quorum_deadline', None),
        }


def calculate_authority_score(agent_metadata: Dict[str, Any],
                              agent_state: Optional[Dict[str, Any]] = None) -> float:
    """
    Calculate authority score for reviewer selection.

    Factors:
    - Health Score (40%): smooth sigmoid function centered at 0.35 (was step function at 0.30/0.60)
    - Track Record (30%): successful_reviews / total_reviews
    - Domain Expertise (20%): has handled similar issues (via tags)
    - Freshness (10%): recent updates (< 24h)

    Args:
        agent_metadata: Agent metadata from governance system
        agent_state: Optional agent state with risk score

    Returns:
        Authority score [0.0, 1.0]
    """
    # Health score (40%) - using smooth function instead of step function (fixes discontinuity bias)
    if agent_state and 'risk_score' in agent_state:
        risk = agent_state['risk_score']
        # Smooth sigmoid function instead of hard thresholds
        # Maps risk [0, 1] to health_score [1.0, 0.0] smoothly
        # Steepness parameter: higher = steeper transition
        steepness = 10.0
        # Center at 0.35 (healthy threshold)
        health_score = 1.0 / (1.0 + np.exp(steepness * (risk - 0.35)))
        # Ensure bounds
        health_score = max(0.0, min(1.0, health_score))
    else:
        health_score = 0.5  # Unknown health

    # Track record (30%)
    total_reviews = agent_metadata.get('total_reviews', 0)
    successful_reviews = agent_metadata.get('successful_reviews', 0)
    if total_reviews > 0:
        track_record = successful_reviews / total_reviews
    else:
        track_record = 0.5  # No history = neutral

    # Domain expertise (20%) - tag-based matching
    # Match reviewer tags with paused agent's tags or issue type
    paused_agent_tags = agent_metadata.get('paused_agent_tags', [])  # Passed from caller
    reviewer_tags = agent_metadata.get('tags', [])

    if paused_agent_tags and reviewer_tags:
        # Calculate overlap: Jaccard similarity
        paused_set = set(paused_agent_tags)
        reviewer_set = set(reviewer_tags)
        if paused_set or reviewer_set:
            overlap = len(paused_set & reviewer_set)
            union = len(paused_set | reviewer_set)
            domain_expertise = overlap / union if union > 0 else 0.5
        else:
            domain_expertise = 0.5
    elif reviewer_tags:
        # Reviewer has tags but paused agent doesn't - slight bonus
        domain_expertise = 0.6
    else:
        # No tags - neutral
        domain_expertise = 0.5

    # Freshness (10%)
    last_update = agent_metadata.get('last_update')
    if last_update:
        try:
            last_update_dt = datetime.fromisoformat(last_update)
            now = datetime.now(timezone.utc)
            if last_update_dt.tzinfo is None:
                last_update_dt = last_update_dt.replace(tzinfo=timezone.utc)
            hours_since = (now - last_update_dt).total_seconds() / 3600
            freshness = 1.0 if hours_since < 24 else 0.5
        except (ValueError, TypeError, AttributeError):
            freshness = 0.5
    else:
        freshness = 0.5

    # Weighted sum
    authority = (
        0.40 * health_score +
        0.30 * track_record +
        0.20 * domain_expertise +
        0.10 * freshness
    )

    return authority
