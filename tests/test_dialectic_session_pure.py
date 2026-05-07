"""
Tests for pure/near-pure functions in dialectic session handling.

Tests _reconstruct_session_from_dict
and DialecticMessage/Resolution/DialecticSession from dialectic_protocol.
"""

import pytest
import sys
from pathlib import Path
from datetime import datetime, timedelta

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.dialectic_protocol import (
    DialecticSession,
    DialecticMessage,
    Resolution,
    DialecticPhase,
    ResolutionAction,
)
from src.mcp_handlers.dialectic.session import _reconstruct_session_from_dict


# ============================================================================
# DialecticPhase Enum
# ============================================================================

class TestDialecticPhase:

    def test_thesis_value(self):
        assert DialecticPhase.THESIS.value == "thesis"

    def test_antithesis_value(self):
        assert DialecticPhase.ANTITHESIS.value == "antithesis"

    def test_resolved_value(self):
        assert DialecticPhase.RESOLVED.value == "resolved"

    def test_all_phases(self):
        phases = [p.value for p in DialecticPhase]
        assert "thesis" in phases
        assert "antithesis" in phases
        assert "synthesis" in phases
        assert "resolved" in phases
        assert "failed" in phases
        # ESCALATED + QUORUM_VOTING retired; 0/47 historical uses (council 2026-05-06)
        assert "escalated" not in phases
        assert "quorum_voting" not in phases


# ============================================================================
# ResolutionAction Enum
# ============================================================================

class TestResolutionAction:

    def test_resume(self):
        assert ResolutionAction.RESUME.value == "resume"

    def test_block(self):
        assert ResolutionAction.BLOCK.value == "block"

    def test_escalate(self):
        assert ResolutionAction.ESCALATE.value == "escalate"


# ============================================================================
# DialecticMessage
# ============================================================================

class TestDialecticMessage:

    def test_create_minimal(self):
        msg = DialecticMessage(
            phase="thesis", agent_id="agent-a", timestamp="2026-01-15T12:00:00"
        )
        assert msg.phase == "thesis"
        assert msg.agent_id == "agent-a"

    def test_optional_fields_default_none(self):
        msg = DialecticMessage(
            phase="thesis", agent_id="agent-a", timestamp="2026-01-15T12:00:00"
        )
        assert msg.root_cause is None
        assert msg.observed_metrics is None
        assert msg.proposed_conditions is None
        assert msg.reasoning is None
        assert msg.agrees is None
        assert msg.concerns is None

    def test_to_dict(self):
        msg = DialecticMessage(
            phase="thesis",
            agent_id="agent-a",
            timestamp="2026-01-15T12:00:00",
            root_cause="Risk too high",
            proposed_conditions=["Reduce complexity"],
            reasoning="Because risk spiked",
        )
        d = msg.to_dict()
        assert d["phase"] == "thesis"
        assert d["agent_id"] == "agent-a"
        assert d["root_cause"] == "Risk too high"
        assert d["proposed_conditions"] == ["Reduce complexity"]

    def test_sign_produces_hex(self):
        msg = DialecticMessage(
            phase="thesis", agent_id="agent-a", timestamp="2026-01-15T12:00:00"
        )
        sig = msg.sign("test-api-key")
        assert isinstance(sig, str)
        assert len(sig) == 64  # SHA-256 hex

    def test_sign_deterministic(self):
        msg = DialecticMessage(
            phase="thesis", agent_id="agent-a", timestamp="2026-01-15T12:00:00"
        )
        sig1 = msg.sign("key-1")
        sig2 = msg.sign("key-1")
        assert sig1 == sig2

    def test_sign_different_keys(self):
        msg = DialecticMessage(
            phase="thesis", agent_id="agent-a", timestamp="2026-01-15T12:00:00"
        )
        sig1 = msg.sign("key-1")
        sig2 = msg.sign("key-2")
        assert sig1 != sig2


# ============================================================================
# Resolution
# ============================================================================

class TestResolution:

    def _make_resolution(self):
        return Resolution(
            action="resume",
            conditions=["Reduce complexity to 0.3"],
            root_cause="Risk threshold exceeded",
            reasoning="Both agents agree on root cause",
            signature_a="sig_a_hash",
            signature_b="sig_b_hash",
            timestamp="2026-01-15T12:00:00",
        )

    def test_create(self):
        res = self._make_resolution()
        assert res.action == "resume"
        assert len(res.conditions) == 1

    def test_to_dict(self):
        res = self._make_resolution()
        d = res.to_dict()
        assert d["action"] == "resume"
        assert d["conditions"] == ["Reduce complexity to 0.3"]
        assert d["signature_a"] == "sig_a_hash"

    def test_hash_produces_hex(self):
        res = self._make_resolution()
        h = res.hash()
        assert isinstance(h, str)
        assert len(h) == 64

    def test_hash_deterministic(self):
        r1 = self._make_resolution()
        r2 = self._make_resolution()
        assert r1.hash() == r2.hash()


# ============================================================================
# DialecticSession
# ============================================================================

class TestDialecticSessionBasic:

    def test_create_session(self):
        session = DialecticSession(
            paused_agent_id="agent-a",
            reviewer_agent_id="agent-b",
            paused_agent_state={"coherence": 0.3},
        )
        assert session.paused_agent_id == "agent-a"
        assert session.reviewer_agent_id == "agent-b"
        assert session.phase == DialecticPhase.THESIS
        assert len(session.transcript) == 0
        assert session.resolution is None

    def test_session_id_generated(self):
        session = DialecticSession(
            paused_agent_id="a", reviewer_agent_id="b", paused_agent_state={}
        )
        assert session.session_id is not None
        assert len(session.session_id) == 16

    def test_session_id_unique(self):
        s1 = DialecticSession(paused_agent_id="a", reviewer_agent_id="b", paused_agent_state={})
        s2 = DialecticSession(paused_agent_id="a", reviewer_agent_id="b", paused_agent_state={})
        assert s1.session_id != s2.session_id

    def test_default_session_type(self):
        session = DialecticSession(
            paused_agent_id="a", reviewer_agent_id="b", paused_agent_state={}
        )
        assert session.session_type == "review"

    def test_exploration_session_type(self):
        session = DialecticSession(
            paused_agent_id="a", reviewer_agent_id="b",
            paused_agent_state={}, session_type="exploration"
        )
        assert session.session_type == "exploration"

    def test_to_dict(self):
        session = DialecticSession(
            paused_agent_id="agent-a",
            reviewer_agent_id="agent-b",
            paused_agent_state={"key": "val"},
        )
        d = session.to_dict()
        assert d["paused_agent_id"] == "agent-a"
        assert d["reviewer_agent_id"] == "agent-b"
        assert d["session_type"] == "review"
        assert d["phase"] in [p.value for p in DialecticPhase]


# ============================================================================
# _reconstruct_session_from_dict
# ============================================================================

class TestReconstructSessionFromDict:

    def test_minimal_session(self):
        data = {
            "paused_agent_id": "agent-a",
            "reviewer_agent_id": "agent-b",
            "paused_agent_state": {},
            "phase": "thesis",
        }
        session = _reconstruct_session_from_dict("sess-001", data)
        assert session is not None
        assert session.session_id == "sess-001"
        assert session.paused_agent_id == "agent-a"
        assert session.phase == DialecticPhase.THESIS

    def test_with_transcript(self):
        data = {
            "paused_agent_id": "agent-a",
            "reviewer_agent_id": "agent-b",
            "paused_agent_state": {},
            "phase": "antithesis",
            "transcript": [
                {
                    "phase": "thesis",
                    "agent_id": "agent-a",
                    "timestamp": "2026-01-15T12:00:00",
                    "root_cause": "Risk too high",
                    "reasoning": "Testing",
                }
            ],
        }
        session = _reconstruct_session_from_dict("sess-002", data)
        assert session is not None
        assert len(session.transcript) == 1
        assert session.transcript[0].phase == "thesis"

    def test_sqlite_message_type_field(self):
        """SQLite uses 'message_type' instead of 'phase' for messages."""
        data = {
            "paused_agent_id": "agent-a",
            "reviewer_agent_id": "agent-b",
            "paused_agent_state": {},
            "transcript": [
                {
                    "message_type": "antithesis",
                    "agent_id": "agent-b",
                    "timestamp": "2026-01-15T12:00:00",
                }
            ],
        }
        session = _reconstruct_session_from_dict("sess-003", data)
        assert session is not None
        assert session.transcript[0].phase == "antithesis"

    def test_with_resolution(self):
        data = {
            "paused_agent_id": "agent-a",
            "reviewer_agent_id": "agent-b",
            "paused_agent_state": {},
            "phase": "resolved",
            "resolution": {
                "action": "resume",
                "conditions": ["condition1"],
                "root_cause": "identified root cause",
                "reasoning": "both agreed",
                "signature_a": "sig_a",
                "signature_b": "sig_b",
                "timestamp": "2026-01-15T12:30:00",
            },
        }
        session = _reconstruct_session_from_dict("sess-004", data)
        assert session is not None
        assert session.resolution is not None
        assert session.resolution.action == "resume"
        assert session.resolution.conditions == ["condition1"]

    def test_no_resolution(self):
        data = {
            "paused_agent_id": "agent-a",
            "reviewer_agent_id": "agent-b",
            "paused_agent_state": {},
        }
        session = _reconstruct_session_from_dict("sess-005", data)
        assert session is not None
        assert session.resolution is None

    def test_invalid_phase_defaults_to_thesis(self):
        data = {
            "paused_agent_id": "agent-a",
            "reviewer_agent_id": "agent-b",
            "paused_agent_state": {},
            "phase": "nonexistent_phase",
        }
        session = _reconstruct_session_from_dict("sess-006", data)
        assert session is not None
        assert session.phase == DialecticPhase.THESIS

    def test_exploration_session_timeouts(self):
        data = {
            "paused_agent_id": "agent-a",
            "reviewer_agent_id": "agent-b",
            "paused_agent_state": {},
            "session_type": "exploration",
        }
        session = _reconstruct_session_from_dict("sess-007", data)
        assert session is not None
        assert session.session_type == "exploration"
        assert session._max_antithesis_wait == timedelta(hours=24)
        assert session._max_total_time == timedelta(hours=72)

    def test_recovery_session_timeouts(self):
        data = {
            "paused_agent_id": "agent-a",
            "reviewer_agent_id": "agent-b",
            "paused_agent_state": {},
            "session_type": "recovery",
        }
        session = _reconstruct_session_from_dict("sess-008", data)
        assert session is not None
        assert session._max_antithesis_wait == DialecticSession.MAX_ANTITHESIS_WAIT

    def test_created_at_string(self):
        data = {
            "paused_agent_id": "agent-a",
            "reviewer_agent_id": "agent-b",
            "paused_agent_state": {},
            "created_at": "2026-01-15T12:00:00",
        }
        session = _reconstruct_session_from_dict("sess-009", data)
        assert session is not None
        assert session.created_at == datetime(2026, 1, 15, 12, 0, 0)

    def test_created_at_datetime(self):
        dt = datetime(2026, 1, 15, 12, 0, 0)
        data = {
            "paused_agent_id": "agent-a",
            "reviewer_agent_id": "agent-b",
            "paused_agent_state": {},
            "created_at": dt,
        }
        session = _reconstruct_session_from_dict("sess-010", data)
        assert session is not None
        assert session.created_at == dt

    def test_synthesis_round_restored(self):
        data = {
            "paused_agent_id": "agent-a",
            "reviewer_agent_id": "agent-b",
            "paused_agent_state": {},
            "synthesis_round": 3,
        }
        session = _reconstruct_session_from_dict("sess-011", data)
        assert session is not None
        assert session.synthesis_round == 3

    def test_empty_transcript(self):
        data = {
            "paused_agent_id": "agent-a",
            "reviewer_agent_id": "agent-b",
            "paused_agent_state": {},
            "transcript": [],
        }
        session = _reconstruct_session_from_dict("sess-012", data)
        assert session is not None
        assert session.transcript == []

    def test_messages_key_instead_of_transcript(self):
        """PostgreSQL backend uses 'messages' key."""
        data = {
            "paused_agent_id": "agent-a",
            "reviewer_agent_id": "agent-b",
            "paused_agent_state": {},
            "messages": [
                {
                    "phase": "thesis",
                    "agent_id": "agent-a",
                    "timestamp": "2026-01-15T12:00:00",
                }
            ],
        }
        session = _reconstruct_session_from_dict("sess-013", data)
        assert session is not None
        assert len(session.transcript) == 1

    def test_max_synthesis_rounds_restored(self):
        data = {
            "paused_agent_id": "agent-a",
            "reviewer_agent_id": "agent-b",
            "paused_agent_state": {},
            "max_synthesis_rounds": 10,
        }
        session = _reconstruct_session_from_dict("sess-014", data)
        assert session is not None
        assert session.max_synthesis_rounds == 10

    def test_topic_restored(self):
        data = {
            "paused_agent_id": "agent-a",
            "reviewer_agent_id": "agent-b",
            "paused_agent_state": {},
            "topic": "Agent oscillation pattern",
        }
        session = _reconstruct_session_from_dict("sess-015", data)
        assert session is not None
        assert session.topic == "Agent oscillation pattern"


# ============================================================================
# DialecticSession Protocol (submit_thesis, submit_antithesis, submit_synthesis)
# ============================================================================

class TestDialecticProtocolFlow:
    """Tests the full dialectic protocol flow - all pure in-memory logic."""

    def _make_session(self, **kwargs):
        defaults = dict(
            paused_agent_id="agent-a",
            reviewer_agent_id="agent-b",
            paused_agent_state={"coherence": 0.3},
        )
        defaults.update(kwargs)
        return DialecticSession(**defaults)

    def _thesis_msg(self, agent_id="agent-a"):
        return DialecticMessage(
            phase="thesis",
            agent_id=agent_id,
            timestamp=datetime.now().isoformat(),
            root_cause="Risk threshold exceeded",
            proposed_conditions=["Reduce complexity to 0.3"],
            reasoning="High risk caused circuit breaker",
        )

    def _antithesis_msg(self, agent_id="agent-b"):
        return DialecticMessage(
            phase="antithesis",
            agent_id=agent_id,
            timestamp=datetime.now().isoformat(),
            observed_metrics={"coherence": 0.3, "risk_score": 0.7},
            concerns=["Complexity still high"],
            reasoning="Agent needs to reduce complexity",
        )

    def _synthesis_msg(self, agent_id, agrees=False):
        return DialecticMessage(
            phase="synthesis",
            agent_id=agent_id,
            timestamp=datetime.now().isoformat(),
            root_cause="Risk threshold exceeded due to high complexity",
            proposed_conditions=["Reduce complexity to 0.3", "Monitor for 1h"],
            reasoning="Agreed on approach",
            agrees=agrees,
        )

    # --- submit_thesis ---

    def test_thesis_success(self):
        session = self._make_session()
        result = session.submit_thesis(self._thesis_msg(), "api-key-a")
        assert result["success"] is True
        assert result["phase"] == "antithesis"
        assert len(session.transcript) == 1

    def test_thesis_wrong_agent(self):
        session = self._make_session()
        result = session.submit_thesis(self._thesis_msg(agent_id="agent-b"), "key")
        assert result["success"] is False
        assert "Only paused agent" in result["error"]

    def test_thesis_wrong_phase(self):
        session = self._make_session()
        session.phase = DialecticPhase.ANTITHESIS
        result = session.submit_thesis(self._thesis_msg(), "key")
        assert result["success"] is False
        assert "Cannot submit thesis" in result["error"]

    def test_thesis_returns_session_id(self):
        session = self._make_session()
        result = session.submit_thesis(self._thesis_msg(), "api-key-a")
        assert "session_id" in result
        assert result["session_id"] == session.session_id

    # --- submit_antithesis ---

    def test_antithesis_success(self):
        session = self._make_session()
        session.submit_thesis(self._thesis_msg(), "key-a")
        result = session.submit_antithesis(self._antithesis_msg(), "key-b")
        assert result["success"] is True
        assert result["phase"] == "synthesis"
        assert len(session.transcript) == 2

    def test_antithesis_wrong_agent(self):
        session = self._make_session()
        session.submit_thesis(self._thesis_msg(), "key-a")
        result = session.submit_antithesis(self._antithesis_msg(agent_id="agent-a"), "key-a")
        assert result["success"] is False
        assert "Only reviewer" in result["error"]

    def test_antithesis_wrong_phase(self):
        session = self._make_session()
        result = session.submit_antithesis(self._antithesis_msg(), "key-b")
        assert result["success"] is False
        assert "Cannot submit antithesis" in result["error"]

    def test_antithesis_starts_synthesis_round_1(self):
        session = self._make_session()
        session.submit_thesis(self._thesis_msg(), "key-a")
        session.submit_antithesis(self._antithesis_msg(), "key-b")
        assert session.synthesis_round == 1

    # --- submit_synthesis ---

    def test_synthesis_no_agree(self):
        session = self._make_session()
        session.submit_thesis(self._thesis_msg(), "key-a")
        session.submit_antithesis(self._antithesis_msg(), "key-b")
        result = session.submit_synthesis(self._synthesis_msg("agent-a"), "key-a")
        assert result["success"] is True
        assert result["converged"] is False
        assert session.synthesis_round == 2

    def test_synthesis_third_party_agrees_resolves(self):
        """Third-party synthesizer with agrees=True resolves immediately."""
        session = self._make_session()
        session.submit_thesis(self._thesis_msg(), "key-a")
        session.submit_antithesis(self._antithesis_msg(), "key-b")
        msg_c = self._synthesis_msg("agent-c")
        msg_c.agrees = True
        result = session.submit_synthesis(msg_c, "key-c")
        assert result["success"] is True
        assert result["converged"] is True
        assert result.get("synthesizer") == "agent-c"
        assert session.phase == DialecticPhase.RESOLVED

    def test_synthesis_third_party_no_agree_continues(self):
        """Third-party synthesizer without agrees continues negotiation."""
        session = self._make_session()
        session.submit_thesis(self._thesis_msg(), "key-a")
        session.submit_antithesis(self._antithesis_msg(), "key-b")
        msg = self._synthesis_msg("agent-c")
        msg.agrees = False
        result = session.submit_synthesis(msg, "key-c")
        assert result["success"] is True
        assert result["converged"] is False
        assert session.synthesis_round == 2

    def test_synthesis_wrong_phase(self):
        session = self._make_session()
        msg = self._synthesis_msg("agent-a")
        result = session.submit_synthesis(msg, "key-a")
        assert result["success"] is False
        assert "Cannot submit synthesis" in result["error"]

    def test_synthesis_max_rounds_exceeded(self):
        session = self._make_session(max_synthesis_rounds=2)
        session.submit_thesis(self._thesis_msg(), "key-a")
        session.submit_antithesis(self._antithesis_msg(), "key-b")
        # Round 1
        session.submit_synthesis(self._synthesis_msg("agent-a"), "key-a")
        # Round 2
        session.submit_synthesis(self._synthesis_msg("agent-b"), "key-b")
        # Round 3 → exceeds max_rounds=2
        result = session.submit_synthesis(self._synthesis_msg("agent-a"), "key-a")
        assert result["success"] is False
        assert "Max synthesis rounds exceeded" in result["error"]
        # ESCALATED retired; max-rounds now routes to FAILED with action="failed_max_rounds"
        assert session.phase == DialecticPhase.FAILED
        assert result.get("action") == "failed_max_rounds"

    def test_convergence_single_agrees(self):
        """Single synthesis with agrees=True resolves. No fourth phase."""
        session = self._make_session()
        session.submit_thesis(self._thesis_msg(), "key-a")
        session.submit_antithesis(self._antithesis_msg(), "key-b")
        result = session.submit_synthesis(self._synthesis_msg("agent-a", agrees=True), "key-a")
        assert result["success"] is True
        assert result["converged"] is True
        assert session.phase == DialecticPhase.RESOLVED

    # --- to_dict ---

    def test_to_dict_includes_transcript(self):
        session = self._make_session()
        session.submit_thesis(self._thesis_msg(), "key-a")
        d = session.to_dict()
        assert "transcript" in d
        assert len(d["transcript"]) == 1

    def test_to_dict_includes_session_metadata(self):
        session = self._make_session()
        d = session.to_dict()
        assert d["paused_agent_id"] == "agent-a"
        assert d["reviewer_agent_id"] == "agent-b"
        assert d["session_type"] == "review"
        assert d["session_id"] is not None


