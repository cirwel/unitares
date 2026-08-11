"""
Operational Log - What Actually Happened

Server-derived features from response_text. No self-report.
This is one half of the dual-log architecture.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List
import re
import hashlib


@dataclass
class OperationalEntry:
    """
    What actually happened. Derived by server from response_text.
    No self-report. Just observable features.
    """
    timestamp: datetime
    agent_id: str
    
    # === From response_text analysis ===
    response_tokens: int          # Approximate token count
    response_chars: int           # Character count
    
    # Structural features
    has_code_blocks: bool
    code_block_count: int
    list_item_count: int
    paragraph_count: int
    question_count: int           # Questions asked by agent
    
    # === Timing ===
    latency_ms: Optional[float] = None  # Time since last update
    
    # === Session ===
    client_session_id: str = ""
    is_session_continuation: bool = False
    
    # === Semantic (lightweight) ===
    topic_hash: str = ""          # Hash of normalized text (for drift)
    
    # === Tool mentions (detected from text) ===
    mentioned_tools: List[str] = field(default_factory=list)
    
    def to_dict(self) -> dict:
        """Serialize for storage."""
        return {
            'timestamp': self.timestamp.isoformat(),
            'agent_id': self.agent_id,
            'response_tokens': self.response_tokens,
            'response_chars': self.response_chars,
            'has_code_blocks': self.has_code_blocks,
            'code_block_count': self.code_block_count,
            'list_item_count': self.list_item_count,
            'paragraph_count': self.paragraph_count,
            'question_count': self.question_count,
            'latency_ms': self.latency_ms,
            'client_session_id': self.client_session_id,
            'is_session_continuation': self.is_session_continuation,
            'topic_hash': self.topic_hash,
            'mentioned_tools': self.mentioned_tools,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> 'OperationalEntry':
        """Deserialize from storage."""
        data = data.copy()
        data['timestamp'] = datetime.fromisoformat(data['timestamp'])
        return cls(**data)


# Known UNITARES tools for detection
KNOWN_TOOLS = [
    'process_agent_update', 'get_governance_metrics', 'search_knowledge_graph',
    'store_knowledge_graph', 'leave_note', 'compare_me_to_similar',
    'list_agents', 'get_discovery_details', 'identity', 'onboard',
    'get_agent_metadata', 'observe_agent', 'detect_anomalies',
    'aggregate_metrics', 'export_to_file', 'get_system_history',
]


def analyze_response_text(text: str) -> dict:
    """
    Extract operational features from response_text.
    Pure text analysis - no ML models needed.

    SUBSTRATE ASSUMPTION: every feature here (code fences, markdown list items,
    blank-line paragraphs, question marks, tool names mentioned in prose) is a
    property of a *natural-language assistant turn formatted in markdown*. This
    is the operational half of the dual log — the half that is supposed to be
    independent of what the agent claims about itself — and it is independent of
    self-report but NOT independent of substrate.

    Measured on the live formulas (2026-08-10): sweeping a Claude turn from
    trivial to heavy moves ``derive_complexity`` by 0.270. Sweeping Lumen from
    healthy to near-dead moves it by 0.0016, because Lumen's ``response_text``
    is a fixed-shape template whose structural features do not vary with its
    state. For non-linguistic substrates this function returns a near-constant
    and the "grounding" it provides is nominal.

    That does not reach an embodied agent's verdict — ``governance_monitor``
    prefers a caller-published ``sensor_eisv`` when one is present — but it does
    reach stored drift/calibration telemetry for every agent. Read
    ``continuity_degenerate`` on the emitted metrics before treating
    ``complexity_divergence`` as a miscalibration signal.

    Returns dict with:
        tokens, chars, has_code, code_blocks, list_items,
        paragraphs, questions, topic_hash, tools
    """
    if not text:
        return {
            'tokens': 0,
            'chars': 0,
            'has_code': False,
            'code_blocks': 0,
            'list_items': 0,
            'paragraphs': 0,
            'questions': 0,
            'topic_hash': '',
            'tools': []
        }
    
    # Token estimate (rough: ~4 chars per token for English)
    tokens = len(text) // 4
    
    # Code blocks (```...```)
    code_blocks = len(re.findall(r'```[\s\S]*?```', text))
    
    # List items (- item or * item or 1. item)
    # Both alternatives must be line-anchored. The ordered-item branch used to
    # sit outside the `^\s*` group, so any "<digits>. " sequence mid-sentence
    # counted as a list item. Agents that report decimals in prose (Lumen's
    # period-joined status line: "Warmth: 0.62. Clarity: 0.71. ...") scored one
    # phantom item per reading — 6 on a string containing no list at all.
    list_items = len(re.findall(r'^\s*(?:[-*•]|\d+\.)\s+', text, re.MULTILINE))
    
    # Paragraphs (double newline separated)
    paragraphs = len([p for p in text.split('\n\n') if p.strip()])
    
    # Questions
    questions = text.count('?')
    
    # Topic hash (first 8 chars of SHA256 of lowercased, stripped text)
    normalized = ' '.join(text.lower().split())
    topic_hash = hashlib.sha256(normalized.encode()).hexdigest()[:8]
    
    # Tool mentions
    text_lower = text.lower()
    tools = [t for t in KNOWN_TOOLS if t.lower() in text_lower]
    
    return {
        'tokens': tokens,
        'chars': len(text),
        'has_code': code_blocks > 0,
        'code_blocks': code_blocks,
        'list_items': list_items,
        'paragraphs': max(1, paragraphs),
        'questions': questions,
        'topic_hash': topic_hash,
        'tools': tools
    }


def create_operational_entry(
    agent_id: str,
    response_text: str,
    client_session_id: str,
    prev_session_id: Optional[str] = None,
    prev_timestamp: Optional[datetime] = None
) -> OperationalEntry:
    """
    Factory function to create an operational entry.
    
    Args:
        agent_id: The agent identifier
        response_text: The response text to analyze
        client_session_id: Current session ID
        prev_session_id: Previous session ID (for continuity detection)
        prev_timestamp: Previous update timestamp (for latency calculation)
    """
    analysis = analyze_response_text(response_text)
    now = datetime.now()
    
    latency_ms = None
    if prev_timestamp:
        latency_ms = (now - prev_timestamp).total_seconds() * 1000
    
    return OperationalEntry(
        timestamp=now,
        agent_id=agent_id,
        response_tokens=analysis['tokens'],
        response_chars=analysis['chars'],
        has_code_blocks=analysis['has_code'],
        code_block_count=analysis['code_blocks'],
        list_item_count=analysis['list_items'],
        paragraph_count=analysis['paragraphs'],
        question_count=analysis['questions'],
        latency_ms=latency_ms,
        client_session_id=client_session_id,
        is_session_continuation=(client_session_id == prev_session_id) if prev_session_id else False,
        topic_hash=analysis['topic_hash'],
        mentioned_tools=analysis['tools']
    )
