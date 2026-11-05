"""
Lean 4 formal verification & Hypothesis state-machine refinement tests for
`src/dialectic_protocol.py`, `src/dialectic_reviewed_work.py`, and
`src/mcp_handlers/dialectic/rejection_stands.py`.

This module bridges two verification layers:
1. **Lean 4 Kernel Proof Verification (`formal/DialecticProtocol.lean`)**:
   Verifies that the mechanized Lean 4 formalization declares all 11 safety and
   authority theorems with zero `sorry`s and zero user axioms, and invokes the
   `lean` kernel when installed on the host.
2. **Hypothesis Stateful Refinement (`DialecticProtocolRefinementMachine`)**:
   Executes randomized multi-step sequences (`submit_thesis`, `submit_antithesis`,
   `submit_synthesis`, reviewer reassignment, `consult` non-protocol messages,
   and `assess_rejection_stands` sweeps) simultaneously against the live Python
   `DialecticSession` and a pure mirror of `Unitares.DialecticProtocol`,
   checking exact state refinement and all 11 safety theorems at every step.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest
from hypothesis import settings, strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, rule

from src.dialectic_protocol import DialecticMessage, DialecticPhase, DialecticSession
from src.dialectic_reviewed_work import (
    declaration_for_request,
    embed_reviewed_work_authorship,
)
from src.mcp_handlers.dialectic.rejection_stands import (
    REJECTION_STANDS,
    assess_rejection_stands,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
LEAN_SPEC_PATH = REPO_ROOT / "formal" / "DialecticProtocol.lean"

EXPECTED_LEAN_THEOREMS = (
    "terminal_resolved_absorbing",
    "terminal_failed_absorbing",
    "no_unilateral_self_clear_over_objection",
    "no_premature_self_resolve_while_verdict_pending",
    "repeat_self_clear_does_not_grow_transcript",
    "reassignment_preserves_standing_objection",
    "recused_author_cannot_advance_or_resolve",
    "saga_resolve_requires_synthesis_phase",
    "phase_rank_monotonic",
    "first_self_clear_attempt_triggers_facilitation",
    "rejection_stands_sweep_only_fails",
)

AGENT_UUIDS: Dict[int, str] = {
    0: "00000000-0000-4000-8000-000000000000",
    1: "00000000-0000-4000-8000-000000000001",
    2: "00000000-0000-4000-8000-000000000002",
    3: "00000000-0000-4000-8000-000000000003",
}

PHASE_RANK: Dict[str, int] = {
    "thesis": 0,
    "antithesis": 1,
    "synthesis": 2,
    "resolved": 3,
    "failed": 3,
}


def _find_lean_binary() -> Optional[str]:
    """Locate the Lean 4 binary on PATH or standard elan/Homebrew locations."""
    from_path = shutil.which("lean")
    if from_path:
        return from_path
    for candidate in (
        Path("/opt/homebrew/bin/lean"),
        Path.home() / ".elan" / "bin" / "lean",
        Path("/usr/local/bin/lean"),
    ):
        if candidate.is_file():
            return str(candidate)
    return None


def _strip_lean_comments(source: str) -> str:
    """Remove Lean line (`-- ...`) and block (`/- ... -/`) comments."""
    without_block = re.sub(r"/-.*?-/", "", source, flags=re.DOTALL)
    return re.sub(r"--[^\n]*", "", without_block)


def test_lean4_spec_structure_and_zero_sorry():
    """`formal/DialecticProtocol.lean` declares all mechanized theorems without `sorry` or `axiom`."""
    assert LEAN_SPEC_PATH.is_file(), f"Missing Lean 4 specification at {LEAN_SPEC_PATH}"
    raw = LEAN_SPEC_PATH.read_text(encoding="utf-8")
    code_only = _strip_lean_comments(raw)

    assert not re.search(r"\bsorry\b", code_only), "Lean specification must not contain `sorry`"
    assert not re.search(r"\baxiom\b", code_only), "Lean specification must not declare custom `axiom`s"

    for theorem_name in EXPECTED_LEAN_THEOREMS:
        pattern = rf"\btheorem\s+{re.escape(theorem_name)}\b"
        assert re.search(pattern, code_only), f"Missing theorem `{theorem_name}` in {LEAN_SPEC_PATH}"


def test_lean4_kernel_typechecks_dialectic_protocol():
    """Run the Lean 4 kernel on `formal/DialecticProtocol.lean` when `lean` is installed."""
    lean_bin = _find_lean_binary()
    if lean_bin is None:
        pytest.skip("Lean 4 toolchain (`lean`) not installed on this runner")

    proc = subprocess.run(
        [lean_bin, str(LEAN_SPEC_PATH)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, (
        f"`lean {LEAN_SPEC_PATH}` failed with exit code {proc.returncode}:\n"
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )
    assert proc.stdout.strip() == ""
    assert proc.stderr.strip() == ""


# ---------------------------------------------------------------------------
# Pure Python mirror of `Unitares.DialecticProtocol` (`formal/DialecticProtocol.lean`)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpecMessage:
    phase: str  # "thesis" | "antithesis" | "synthesis" | "consult"
    sender: int
    has_root_cause: bool = True
    has_conditions: bool = True
    has_reasoning: bool = True
    agrees: Optional[bool] = None


@dataclass(frozen=True)
class SpecState:
    paused_agent: int
    reviewer_agent: Optional[int]
    declared_authors: Tuple[int, ...]
    session_type: str
    max_rounds: int
    phase: str = "thesis"
    synthesis_round: int = 0
    awaiting_facilitation: bool = False
    transcript: Tuple[SpecMessage, ...] = field(default_factory=tuple)


def spec_is_recused(s: SpecState, candidate: Optional[int]) -> bool:
    if candidate is None or candidate == s.paused_agent:
        return False
    return candidate in s.declared_authors


def spec_reviewer_objection_stands(s: SpecState) -> bool:
    self_review = s.reviewer_agent == s.paused_agent
    for msg in reversed(s.transcript):
        if msg.phase != "synthesis" or msg.agrees is None:
            continue
        if not self_review and msg.sender == s.paused_agent:
            continue
        return msg.agrees is False
    return False


def spec_reviewer_verdict_pending(s: SpecState) -> bool:
    if s.reviewer_agent is None or s.reviewer_agent == s.paused_agent:
        return False
    has_indep_antithesis = any(
        m.phase == "antithesis" and m.sender != s.paused_agent for m in s.transcript
    )
    if not has_indep_antithesis:
        return False
    has_indep_verdict = any(
        m.phase == "synthesis" and m.sender != s.paused_agent and m.agrees is not None
        for m in s.transcript
    )
    return not has_indep_verdict


def spec_paused_already_answered(s: SpecState) -> bool:
    self_review = s.reviewer_agent == s.paused_agent
    latest_rej_idx: Optional[int] = None
    for idx in range(len(s.transcript) - 1, -1, -1):
        msg = s.transcript[idx]
        if msg.phase != "synthesis" or msg.agrees is not False:
            continue
        if not self_review and msg.sender == s.paused_agent:
            continue
        latest_rej_idx = idx
        break
    if latest_rej_idx is None:
        return False
    return any(
        m.phase == "synthesis" and m.sender == s.paused_agent and m.agrees is True
        for m in s.transcript[latest_rej_idx + 1 :]
    )


def spec_submit_thesis(s: SpecState, m: SpecMessage) -> Tuple[bool, SpecState]:
    if m.sender != s.paused_agent:
        return False, s
    if s.phase != "thesis":
        return False, s
    if not m.has_root_cause:
        return False, s
    if s.session_type == "recovery" and not m.has_conditions:
        return False, s
    return True, SpecState(
        paused_agent=s.paused_agent,
        reviewer_agent=s.reviewer_agent,
        declared_authors=s.declared_authors,
        session_type=s.session_type,
        max_rounds=s.max_rounds,
        phase="antithesis",
        synthesis_round=s.synthesis_round,
        awaiting_facilitation=s.awaiting_facilitation,
        transcript=s.transcript + (m,),
    )


def spec_submit_antithesis(s: SpecState, m: SpecMessage) -> Tuple[bool, SpecState]:
    if spec_is_recused(s, m.sender):
        return False, s
    reviewer = s.reviewer_agent
    if reviewer is None:
        if m.sender == s.paused_agent:
            return False, s
        reviewer = m.sender
        s_with_rev = SpecState(
            paused_agent=s.paused_agent,
            reviewer_agent=reviewer,
            declared_authors=s.declared_authors,
            session_type=s.session_type,
            max_rounds=s.max_rounds,
            phase=s.phase,
            synthesis_round=s.synthesis_round,
            awaiting_facilitation=s.awaiting_facilitation,
            transcript=s.transcript,
        )
    else:
        s_with_rev = s

    if m.sender != reviewer:
        return False, s_with_rev
    if s_with_rev.phase != "antithesis":
        return False, s_with_rev
    if not m.has_reasoning:
        return False, s_with_rev

    return True, SpecState(
        paused_agent=s_with_rev.paused_agent,
        reviewer_agent=reviewer,
        declared_authors=s_with_rev.declared_authors,
        session_type=s_with_rev.session_type,
        max_rounds=s_with_rev.max_rounds,
        phase="synthesis",
        synthesis_round=1,
        awaiting_facilitation=s_with_rev.awaiting_facilitation,
        transcript=s_with_rev.transcript + (m,),
    )


def spec_submit_synthesis(
    s: SpecState, m: SpecMessage
) -> Tuple[bool, bool, SpecState]:
    """Returns `(success, converged, next_state)`."""
    if spec_is_recused(s, s.reviewer_agent):
        return False, False, s
    if s.phase != "synthesis":
        return False, False, s
    if s.synthesis_round > s.max_rounds:
        return (
            False,
            False,
            SpecState(
                paused_agent=s.paused_agent,
                reviewer_agent=s.reviewer_agent,
                declared_authors=s.declared_authors,
                session_type=s.session_type,
                max_rounds=s.max_rounds,
                phase="failed",
                synthesis_round=s.synthesis_round,
                awaiting_facilitation=s.awaiting_facilitation,
                transcript=s.transcript,
            ),
        )

    objection_stands = spec_reviewer_objection_stands(s)
    verdict_pending = spec_reviewer_verdict_pending(s)
    already_answered = spec_paused_already_answered(s)
    agrees_true = m.agrees is True
    is_paused = m.sender == s.paused_agent

    if agrees_true and is_paused and verdict_pending:
        return False, False, s
    if agrees_true and is_paused and objection_stands and already_answered:
        return False, False, s
    if agrees_true and is_paused and objection_stands:
        return (
            True,
            False,
            SpecState(
                paused_agent=s.paused_agent,
                reviewer_agent=s.reviewer_agent,
                declared_authors=s.declared_authors,
                session_type=s.session_type,
                max_rounds=s.max_rounds,
                phase="synthesis",
                synthesis_round=s.synthesis_round + 1,
                awaiting_facilitation=True,
                transcript=s.transcript + (m,),
            ),
        )
    if agrees_true:
        return (
            True,
            True,
            SpecState(
                paused_agent=s.paused_agent,
                reviewer_agent=s.reviewer_agent,
                declared_authors=s.declared_authors,
                session_type=s.session_type,
                max_rounds=s.max_rounds,
                phase="resolved",
                synthesis_round=s.synthesis_round,
                awaiting_facilitation=s.awaiting_facilitation,
                transcript=s.transcript + (m,),
            ),
        )
    return (
        True,
        False,
        SpecState(
            paused_agent=s.paused_agent,
            reviewer_agent=s.reviewer_agent,
            declared_authors=s.declared_authors,
            session_type=s.session_type,
            max_rounds=s.max_rounds,
            phase="synthesis",
            synthesis_round=s.synthesis_round + 1,
            awaiting_facilitation=s.awaiting_facilitation,
            transcript=s.transcript + (m,),
        ),
    )


# ---------------------------------------------------------------------------
# Stateful Refinement & Invariant Model-Checking
# ---------------------------------------------------------------------------


class DialecticProtocolRefinementMachine(RuleBasedStateMachine):
    """
    Runs arbitrary interleavings of dialectic protocol actions against both
    `DialecticSession` and `SpecState`, checking bisimulation and the 11
    Lean-proved safety theorems at every step.
    """

    def __init__(self):
        super().__init__()
        self.session: Optional[DialecticSession] = None
        self.spec: Optional[SpecState] = None
        self.clock: datetime = datetime(2026, 11, 5, 12, 0, 0, tzinfo=timezone.utc)

    def _next_timestamp(self, seconds: int = 10) -> str:
        self.clock = self.clock + timedelta(seconds=seconds)
        return self.clock.isoformat()

    @initialize(
        paused_idx=st.integers(min_value=0, max_value=1),
        reviewer_choice=st.sampled_from([None, 0, 1, 2]),
        declared_authors=st.lists(
            st.integers(min_value=0, max_value=3), max_size=2, unique=True
        ),
        session_type=st.sampled_from(["recovery", "review", "dispute", "exploration"]),
        max_rounds=st.integers(min_value=1, max_value=4),
    )
    def init_session(
        self,
        paused_idx: int,
        reviewer_choice: Optional[int],
        declared_authors: List[int],
        session_type: str,
        max_rounds: int,
    ):
        paused_uuid = AGENT_UUIDS[paused_idx]
        reviewer_uuid = AGENT_UUIDS[reviewer_choice] if reviewer_choice is not None else None
        author_uuids = [AGENT_UUIDS[i] for i in declared_authors]

        decl = declaration_for_request(author_uuids, declared_by_agent_id=paused_uuid)
        paused_state = embed_reviewed_work_authorship({"risk_score": 0.65}, decl)

        self.session = DialecticSession(
            paused_agent_id=paused_uuid,
            reviewer_agent_id=reviewer_uuid,
            paused_agent_state=paused_state,
            session_type=session_type,
            max_synthesis_rounds=max_rounds,
        )
        effective_max_rounds = self.session.max_synthesis_rounds
        self.spec = SpecState(
            paused_agent=paused_idx,
            reviewer_agent=reviewer_choice,
            declared_authors=tuple(declared_authors),
            session_type=session_type,
            max_rounds=effective_max_rounds,
            phase="thesis",
            synthesis_round=0,
            awaiting_facilitation=False,
            transcript=(),
        )

    @rule(
        sender_idx=st.integers(min_value=0, max_value=3),
        has_root_cause=st.booleans(),
        has_conditions=st.booleans(),
    )
    def step_submit_thesis(
        self, sender_idx: int, has_root_cause: bool, has_conditions: bool
    ):
        assert self.session is not None and self.spec is not None
        prev_phase = self.session.phase.value
        prev_rank = PHASE_RANK[prev_phase]
        prev_transcript_len = len(self.session.transcript)

        msg = DialecticMessage(
            phase="thesis",
            agent_id=AGENT_UUIDS[sender_idx],
            timestamp=self._next_timestamp(),
            root_cause="Root cause analysis" if has_root_cause else "   ",
            proposed_conditions=["Keep complexity <= 0.4"] if has_conditions else [],
            reasoning="Initial thesis reasoning",
        )
        spec_msg = SpecMessage(
            phase="thesis",
            sender=sender_idx,
            has_root_cause=has_root_cause,
            has_conditions=has_conditions,
        )

        res = self.session.submit_thesis(msg)
        ok_spec, next_spec = spec_submit_thesis(self.spec, spec_msg)
        self.spec = next_spec

        assert res["success"] == ok_spec
        assert PHASE_RANK[self.session.phase.value] >= prev_rank
        if prev_phase in ("resolved", "failed"):
            assert self.session.phase.value == prev_phase
            assert len(self.session.transcript) == prev_transcript_len

    @rule(
        sender_idx=st.integers(min_value=0, max_value=3),
        has_reasoning=st.booleans(),
    )
    def step_submit_antithesis(self, sender_idx: int, has_reasoning: bool):
        assert self.session is not None and self.spec is not None
        prev_phase = self.session.phase.value
        prev_rank = PHASE_RANK[prev_phase]
        was_recused = spec_is_recused(self.spec, sender_idx)

        msg = DialecticMessage(
            phase="antithesis",
            agent_id=AGENT_UUIDS[sender_idx],
            timestamp=self._next_timestamp(),
            observed_metrics={"risk_score": 0.65},
            concerns=["Check drift"],
            reasoning="Reviewer counter-perspective" if has_reasoning else "",
        )
        spec_msg = SpecMessage(
            phase="antithesis",
            sender=sender_idx,
            has_reasoning=has_reasoning,
        )

        res = self.session.submit_antithesis(msg)
        ok_spec, next_spec = spec_submit_antithesis(self.spec, spec_msg)
        self.spec = next_spec

        assert res["success"] == ok_spec
        assert PHASE_RANK[self.session.phase.value] >= prev_rank
        if was_recused:
            # Theorem 6a: recused author cannot advance antithesis
            assert res["success"] is False
            assert res.get("error_code") == "REVIEWER_AUTHOR_CONFLICT"

    @rule(
        sender_idx=st.integers(min_value=0, max_value=3),
        agrees=st.sampled_from([True, False, None]),
    )
    def step_submit_synthesis(self, sender_idx: int, agrees: Optional[bool]):
        assert self.session is not None and self.spec is not None
        prev_phase = self.session.phase.value
        prev_rank = PHASE_RANK[prev_phase]
        prev_len = len(self.session.transcript)

        objection_before = self.session._reviewer_objection_stands()
        pending_before = self.session._reviewer_verdict_pending()
        answered_before = self.session._paused_agent_already_answered_current_objection()
        reviewer_recused_before = spec_is_recused(self.spec, self.spec.reviewer_agent)
        is_paused = sender_idx == self.spec.paused_agent

        msg = DialecticMessage(
            phase="synthesis",
            agent_id=AGENT_UUIDS[sender_idx],
            timestamp=self._next_timestamp(),
            proposed_conditions=["Keep complexity <= 0.4"],
            root_cause="Agreed root cause",
            reasoning="Synthesis rationale",
            agrees=agrees,
        )
        spec_msg = SpecMessage(
            phase="synthesis",
            sender=sender_idx,
            agrees=agrees,
        )

        res = self.session.submit_synthesis(msg)
        ok_spec, conv_spec, next_spec = spec_submit_synthesis(self.spec, spec_msg)
        self.spec = next_spec

        assert res["success"] == ok_spec
        assert bool(res.get("converged", False)) == conv_spec
        assert PHASE_RANK[self.session.phase.value] >= prev_rank

        # Theorem 2: No unilateral self-clear over standing reviewer objection (#1543)
        if prev_phase != "resolved" and objection_before and is_paused:
            assert not res.get("converged", False)
            assert self.session.phase != DialecticPhase.RESOLVED

        # Theorem 3: No premature self-resolve while independent verdict is pending
        if prev_phase != "resolved" and pending_before and is_paused and agrees is True:
            assert res["success"] is False
            assert not res.get("converged", False)
            assert self.session.phase != DialecticPhase.RESOLVED
            assert len(self.session.transcript) == prev_len

        # Theorem 4: Repeat self-clear does not grow transcript
        if objection_before and answered_before and is_paused and agrees is True:
            assert not res.get("converged", False)
            assert len(self.session.transcript) == prev_len

        # Theorem 6b: Recused assigned reviewer blocks synthesis
        if reviewer_recused_before:
            assert res["success"] is False
            assert len(self.session.transcript) == prev_len

    @rule(new_reviewer_idx=st.integers(min_value=0, max_value=3))
    def step_reassign_reviewer(self, new_reviewer_idx: int):
        """Model `_apply_reviewer_reassignment` (`reviewer_agent_id` update + clear `awaiting_facilitation`)."""
        assert self.session is not None and self.spec is not None
        if self.session.phase in (DialecticPhase.RESOLVED, DialecticPhase.FAILED):
            return

        old_reviewer_idx = self.spec.reviewer_agent
        objection_before = self.session._reviewer_objection_stands()

        self.session.reviewer_agent_id = AGENT_UUIDS[new_reviewer_idx]
        self.session.awaiting_facilitation = False
        self.spec = SpecState(
            paused_agent=self.spec.paused_agent,
            reviewer_agent=new_reviewer_idx,
            declared_authors=self.spec.declared_authors,
            session_type=self.spec.session_type,
            max_rounds=self.spec.max_rounds,
            phase=self.spec.phase,
            synthesis_round=self.spec.synthesis_round,
            awaiting_facilitation=False,
            transcript=self.spec.transcript,
        )

        # Theorem 5: Reassignment between independent reviewers preserves standing objection
        if (
            old_reviewer_idx is not None
            and old_reviewer_idx != self.spec.paused_agent
            and new_reviewer_idx != self.spec.paused_agent
        ):
            assert self.session._reviewer_objection_stands() == objection_before

    @rule(sender_idx=st.integers(min_value=0, max_value=3))
    def step_append_consult_entry(self, sender_idx: int):
        """Non-protocol `consult` transcript entries never alter phase, objection state, or last-update clock."""
        assert self.session is not None and self.spec is not None
        last_ts_before = self.session.get_last_update_timestamp()
        objection_before = self.session._reviewer_objection_stands()
        pending_before = self.session._reviewer_verdict_pending()
        answered_before = self.session._paused_agent_already_answered_current_objection()

        consult_msg = DialecticMessage(
            phase="consult",
            agent_id=AGENT_UUIDS[sender_idx],
            timestamp=self._next_timestamp(seconds=300),
            reasoning="External advisory consult note",
        )
        self.session.transcript.append(consult_msg)
        self.spec = SpecState(
            paused_agent=self.spec.paused_agent,
            reviewer_agent=self.spec.reviewer_agent,
            declared_authors=self.spec.declared_authors,
            session_type=self.spec.session_type,
            max_rounds=self.spec.max_rounds,
            phase=self.spec.phase,
            synthesis_round=self.spec.synthesis_round,
            awaiting_facilitation=self.spec.awaiting_facilitation,
            transcript=self.spec.transcript
            + (SpecMessage(phase="consult", sender=sender_idx),),
        )

        assert self.session.get_last_update_timestamp() == last_ts_before
        assert self.session._reviewer_objection_stands() == objection_before
        assert self.session._reviewer_verdict_pending() == pending_before
        assert (
            self.session._paused_agent_already_answered_current_objection()
            == answered_before
        )

    @rule(
        elapsed_seconds=st.sampled_from([10, 600, 7200]),
        is_orchestrated=st.booleans(),
    )
    def step_assess_rejection_stands(
        self, elapsed_seconds: int, is_orchestrated: bool
    ):
        """Verify Theorem 10 against live `assess_rejection_stands`."""
        assert self.session is not None and self.spec is not None
        eval_now = self.clock + timedelta(seconds=elapsed_seconds)
        payload = assess_rejection_stands(
            self.session.transcript,
            paused_agent_id=self.session.paused_agent_id,
            reviewer_agent_id=self.session.reviewer_agent_id,
            awaiting_facilitation=self.session.awaiting_facilitation,
            now=eval_now,
            is_orchestrated=lambda _rev: is_orchestrated,
            wait_s=300.0,
            total_s=900.0,
        )
        if payload is not None:
            # Theorem 10: `assess_rejection_stands` only fires when awaiting facilitation
            # with a standing independent reviewer rejection, and its action is never resume.
            assert self.session.awaiting_facilitation is True
            assert self.session._reviewer_objection_stands() is True
            assert self.session.reviewer_agent_id is not None
            assert self.session.reviewer_agent_id != self.session.paused_agent_id
            assert payload["action"] == REJECTION_STANDS
            assert payload["reason"] == REJECTION_STANDS

    @invariant()
    def check_refinement_bisimulation(self):
        """Live `DialecticSession` and Lean mirror `SpecState` agree on all state projections."""
        if self.session is None or self.spec is None:
            return

        assert self.session.phase.value == self.spec.phase
        expected_rev = (
            AGENT_UUIDS[self.spec.reviewer_agent]
            if self.spec.reviewer_agent is not None
            else None
        )
        assert self.session.reviewer_agent_id == expected_rev
        assert self.session.synthesis_round == self.spec.synthesis_round
        assert self.session.awaiting_facilitation == self.spec.awaiting_facilitation
        assert len(self.session.transcript) == len(self.spec.transcript)

        assert (
            self.session._reviewer_objection_stands()
            == spec_reviewer_objection_stands(self.spec)
        )
        assert (
            self.session._reviewer_verdict_pending()
            == spec_reviewer_verdict_pending(self.spec)
        )
        assert (
            self.session._paused_agent_already_answered_current_objection()
            == spec_paused_already_answered(self.spec)
        )


TestDialecticProtocolStateMachine = DialecticProtocolRefinementMachine.TestCase
TestDialecticProtocolStateMachine.settings = settings(
    max_examples=120,
    stateful_step_count=35,
    deadline=None,
)
