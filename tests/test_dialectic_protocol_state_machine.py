"""
Lean 4 formal verification & Hypothesis state-machine refinement tests for
`src/dialectic_protocol.py`, `src/dialectic_reviewed_work.py`,
`src/mcp_handlers/dialectic/resolution_helpers.py`, and
`src/mcp_handlers/dialectic/rejection_stands.py`.

This module bridges three verification layers:
1. **Lean 4 Kernel Proof Verification (`formal/DialecticProtocol.lean`)**:
   Verifies that the mechanized Lean 4 formalization declares all 11 safety and
   authority theorems with zero `sorry`s and zero user axioms, and invokes the
   `lean` kernel when installed on the host/runner (skipping the subprocess
   check on Python-only CI runners without `lean`).
2. **Parsed `#guard` Trace Conformance (`formal/DialecticProtocol.lean`)**:
   Parses the compile-time `traceScenario*` definitions and `#guard` assertions
   directly out of `formal/DialecticProtocol.lean` and executes them step-by-step
   against both `SpecState` and the live Python implementation (`DialecticSession`,
   `_handle_reassign_reviewer`, `_close_standing_rejections`, and
   `assess_rejection_stands`) on every pytest run.
3. **Hypothesis Stateful Refinement (`DialecticProtocolRefinementMachine`)**:
   Executes randomized multi-step sequences (`submit_thesis`, `submit_antithesis`,
   `submit_synthesis`, `_handle_reassign_reviewer`, `consult` non-protocol
   messages, and `assess_rejection_stands` / `_close_standing_rejections` sweeps)
   simultaneously against the live Python implementation and `SpecState`,
   checking state refinement and element-by-element transcript equality via
   `@invariant` and asserting each transition's safety properties in its `@rule`.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, patch

import pytest
from hypothesis import settings, strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, rule

from src.dialectic_protocol import DialecticMessage, DialecticPhase, DialecticSession
from src.dialectic_reviewed_work import (
    declaration_for_request,
    embed_reviewed_work_authorship,
)
from src.mcp_handlers.dialectic.auto_resolve import (
    _CycleCounts,
    _close_standing_rejections,
)
from src.mcp_handlers.dialectic.rejection_stands import (
    REJECTION_STANDS,
    assess_rejection_stands,
)
from src.mcp_handlers.dialectic import handlers as dialectic_handlers
from src.mcp_handlers.dialectic.resolution_helpers import (
    ACTIVE_SESSIONS,
    _handle_reassign_reviewer as handle_reassign_reviewer,
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
    "consult_preserves_authority_predicates",
    "phase_rank_monotonic",
    "first_self_clear_attempt_triggers_facilitation",
    "rejection_stands_sweep_only_fails",
)

EXPECTED_LEAN_DEFS = (
    "isRecused",
    "assignedReviewerRecused",
    "reviewerObjectionStands",
    "lastIndepVerdictSender",
    "reviewerAnsweredReply",
    "reviewerVerdictPending",
    "pausedAlreadyAnsweredCurrentObjection",
    "submitThesis",
    "submitAntithesis",
    "submitSynthesis",
    "reassignReviewer",
    "assessRejectionStands",
    "closeStandingRejection",
    "appendConsult",
)

SYSTEM_AGENT_ID = 999

AGENT_UUIDS: Dict[int, str] = {
    0: "00000000-0000-4000-8000-000000000000",
    1: "00000000-0000-4000-8000-000000000001",
    2: "00000000-0000-4000-8000-000000000002",
    3: "00000000-0000-4000-8000-000000000003",
    SYSTEM_AGENT_ID: "system",
}

PHASE_RANK: Dict[str, int] = {
    "thesis": 0,
    "antithesis": 1,
    "synthesis": 2,
    "resolved": 3,
    "failed": 3,
}


def _find_lean_binary() -> Optional[str]:
    """Locate the Lean 4 binary, preferring the direct toolchain binary under
    `~/.elan/toolchains/.../bin/lean` so read-only sandboxes never invoke the
    `elan` proxy's settings write."""
    elan_toolchains = Path.home() / ".elan" / "toolchains"
    toolchain_file = REPO_ROOT / "formal" / "lean-toolchain"
    if toolchain_file.is_file() and elan_toolchains.is_dir():
        raw_tc = toolchain_file.read_text(encoding="utf-8").strip()
        sanitized = raw_tc.replace("/", "--").replace(":", "---")
        direct = elan_toolchains / sanitized / "bin" / "lean"
        if direct.is_file():
            return str(direct)
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
    """`formal/DialecticProtocol.lean` declares all mechanized defs and theorems without `sorry` or `axiom`."""
    assert LEAN_SPEC_PATH.is_file(), f"Missing Lean 4 specification at {LEAN_SPEC_PATH}"
    raw = LEAN_SPEC_PATH.read_text(encoding="utf-8")
    code_only = _strip_lean_comments(raw)

    assert not re.search(r"\bsorry\b", code_only), "Lean specification must not contain `sorry`"
    assert not re.search(r"\baxiom\b", code_only), "Lean specification must not declare custom `axiom`s"
    assert not re.search(r"\badmit\b", code_only), "Lean specification must not contain `admit`"

    for def_name in EXPECTED_LEAN_DEFS:
        pattern = rf"\bdef\s+{re.escape(def_name)}\b"
        assert re.search(pattern, code_only), f"Missing def `{def_name}` in {LEAN_SPEC_PATH}"

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


def spec_assigned_reviewer_recused(s: SpecState) -> bool:
    return spec_is_recused(s, s.reviewer_agent)


def spec_reviewer_objection_stands(s: SpecState) -> bool:
    self_review = s.reviewer_agent == s.paused_agent
    for msg in reversed(s.transcript):
        if msg.phase != "synthesis" or msg.agrees is None:
            continue
        if not self_review and msg.sender == s.paused_agent:
            continue
        return msg.agrees is False
    return False


def spec_last_indep_verdict_sender(s: SpecState) -> Optional[int]:
    for msg in reversed(s.transcript):
        if msg.phase == "synthesis" and msg.sender != s.paused_agent and msg.agrees is not None:
            return msg.sender
    return None


def spec_reviewer_answered_reply(s: SpecState, rev: int) -> bool:
    stage = 0
    seen_paused_between = False
    for msg in reversed(s.transcript):
        if msg.phase != "synthesis":
            continue
        if msg.sender == s.paused_agent:
            if stage == 0:
                return False
            if stage == 1:
                seen_paused_between = True
            else:
                return False
        else:
            if msg.agrees is None:
                continue
            if stage == 0:
                if msg.sender == rev:
                    stage = 1
                    seen_paused_between = False
                else:
                    return False
            else:
                return (msg.sender == rev) and seen_paused_between
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
    if spec_assigned_reviewer_recused(s):
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


def spec_reassign_reviewer(s: SpecState, new_reviewer: int) -> Tuple[bool, SpecState]:
    """Mirror `reassignReviewer` in `formal/DialecticProtocol.lean`."""
    if new_reviewer == s.paused_agent or spec_is_recused(s, new_reviewer):
        return False, s
    phase_allowed = (
        s.phase in ("thesis", "antithesis")
        or s.awaiting_facilitation
        or (s.phase == "synthesis" and spec_assigned_reviewer_recused(s))
    )
    if not phase_allowed:
        return False, s

    was_terminal = s.phase in ("resolved", "failed")
    old_self_or_conflicted = s.reviewer_agent is not None and (
        s.reviewer_agent == s.paused_agent
        or spec_is_recused(s, s.reviewer_agent)
    )
    if was_terminal and s.awaiting_facilitation:
        has_thesis = any(m.phase == "thesis" for m in s.transcript)
        next_phase = "antithesis" if has_thesis else "thesis"
    elif (
        (s.awaiting_facilitation or spec_assigned_reviewer_recused(s))
        and old_self_or_conflicted
        and s.phase == "synthesis"
    ):
        next_phase = "antithesis"
    else:
        next_phase = s.phase

    audit_msg_phase = (
        next_phase
        if next_phase in ("thesis", "antithesis", "synthesis")
        else "synthesis"
    )
    audit_msg = SpecMessage(
        phase=audit_msg_phase,
        sender=SYSTEM_AGENT_ID,
        has_root_cause=False,
        has_conditions=False,
        has_reasoning=True,
        agrees=None,
    )
    return True, SpecState(
        paused_agent=s.paused_agent,
        reviewer_agent=new_reviewer,
        declared_authors=s.declared_authors,
        session_type=s.session_type,
        max_rounds=s.max_rounds,
        phase=next_phase,
        synthesis_round=s.synthesis_round,
        awaiting_facilitation=False,
        transcript=s.transcript + (audit_msg,),
    )


def spec_assess_rejection_stands(
    s: SpecState, is_orchestrated: bool, window_ended: bool
) -> bool:
    """Mirror `assessRejectionStands` in `formal/DialecticProtocol.lean`."""
    if not s.awaiting_facilitation:
        return False
    if s.reviewer_agent is None or s.reviewer_agent == s.paused_agent:
        return False
    if not spec_reviewer_objection_stands(s):
        return False
    if spec_last_indep_verdict_sender(s) != s.reviewer_agent:
        return False
    return spec_reviewer_answered_reply(s, s.reviewer_agent) or (
        is_orchestrated and window_ended
    )


def spec_close_standing_rejection(
    s: SpecState, is_orchestrated: bool, window_ended: bool
) -> Tuple[bool, SpecState]:
    """Mirror `closeStandingRejection` in `formal/DialecticProtocol.lean`."""
    if s.phase != "synthesis":
        return False, s
    if spec_assigned_reviewer_recused(s):
        return False, s
    if not spec_assess_rejection_stands(s, is_orchestrated, window_ended):
        return False, s
    return True, SpecState(
        paused_agent=s.paused_agent,
        reviewer_agent=s.reviewer_agent,
        declared_authors=s.declared_authors,
        session_type=s.session_type,
        max_rounds=s.max_rounds,
        phase="failed",
        synthesis_round=s.synthesis_round,
        awaiting_facilitation=False,
        transcript=s.transcript,
    )


def spec_append_consult(s: SpecState, sender: int) -> SpecState:
    """Mirror `appendConsult` in `formal/DialecticProtocol.lean`."""
    consult_msg = SpecMessage(
        phase="consult",
        sender=sender,
        has_root_cause=False,
        has_conditions=False,
        has_reasoning=True,
        agrees=None,
    )
    return SpecState(
        paused_agent=s.paused_agent,
        reviewer_agent=s.reviewer_agent,
        declared_authors=s.declared_authors,
        session_type=s.session_type,
        max_rounds=s.max_rounds,
        phase=s.phase,
        synthesis_round=s.synthesis_round,
        awaiting_facilitation=s.awaiting_facilitation,
        transcript=s.transcript + (consult_msg,),
    )


def _apply_live_reassignment(
    session: DialecticSession, new_reviewer_uuid: str
) -> bool:
    """Execute the production `handle_reassign_reviewer` handler against `session`."""
    ACTIVE_SESSIONS[session.session_id] = session
    fake_meta = {
        uuid: {"status": "active"}
        for idx, uuid in AGENT_UUIDS.items()
        if idx != SYSTEM_AGENT_ID
    }
    fake_mcp = SimpleNamespace(
        load_metadata_async=AsyncMock(return_value=None),
        agent_metadata=fake_meta,
    )
    try:
        with patch.object(
            dialectic_handlers,
            "_reviewer_reassignment_actor",
            return_value="operator",
        ), patch.object(
            dialectic_handlers,
            "mcp_server",
            fake_mcp,
        ), patch(
            "src.mcp_handlers.dialectic.resolution_helpers._append_protocol_message",
            new=AsyncMock(return_value=None),
        ), patch(
            "src.mcp_handlers.dialectic.resolution_helpers._emit_request_reviewer_reassigned",
            new=AsyncMock(return_value=None),
        ):
            res = asyncio.run(
                handle_reassign_reviewer(
                    {
                        "session_id": session.session_id,
                        "new_reviewer_id": new_reviewer_uuid,
                        "reason": "state_machine_test",
                    }
                )
            )
        payload = json.loads(res[0].text)
        return bool(payload.get("success") is True)
    finally:
        ACTIVE_SESSIONS.pop(session.session_id, None)


def _apply_live_close_standing_rejection(
    session: DialecticSession, eval_now: datetime, is_orchestrated: bool
) -> bool:
    """Execute the production `_close_standing_rejections` sweeper against `session`."""
    ACTIVE_SESSIONS[session.session_id] = session
    row = {
        "session_id": session.session_id,
        "awaiting_facilitation": session.awaiting_facilitation,
        "phase": session.phase.value,
        "reviewer_agent_id": session.reviewer_agent_id,
        "paused_agent_id": session.paused_agent_id,
        "paused_agent_state": session.paused_agent_state,
        "updated_at": eval_now.isoformat(),
        "messages": [m.to_dict() for m in session.transcript],
    }

    def _pinned_assess(*args, **kwargs):
        kwargs["now"] = eval_now
        return assess_rejection_stands(*args, **kwargs)

    initial_row_snapshot = dict(row)
    persisted_row = dict(row)
    resolve_calls: List[Dict[str, Any]] = []

    async def _mock_resolve_session_async(
        session_id: str,
        resolution: Dict[str, Any],
        status: str = "resolved",
        **kwargs: Any,
    ) -> bool:
        resolve_calls.append(
            {
                "session_id": session_id,
                "resolution": dict(resolution) if isinstance(resolution, dict) else resolution,
                "status": status,
                "kwargs": dict(kwargs),
            }
        )
        persisted_row["phase"] = status
        if kwargs.get("clear_facilitation"):
            persisted_row["awaiting_facilitation"] = False
        return True

    try:
        with patch(
            "src.mcp_handlers.dialectic.auto_resolve.has_inflight_saga_async",
            new=AsyncMock(return_value=False),
        ), patch(
            "src.mcp_handlers.dialectic.auto_resolve.get_session_async",
            new=AsyncMock(return_value=row),
        ), patch(
            "src.mcp_handlers.dialectic.auto_resolve.resolve_session_async",
            side_effect=_mock_resolve_session_async,
        ), patch(
            "src.mcp_handlers.dialectic.auto_resolve.probe_saga_since_async",
            new=AsyncMock(return_value={}),
        ), patch(
            "src.mcp_handlers.dialectic.auto_resolve.emit_guarded_write",
            new=AsyncMock(return_value=None),
        ), patch(
            "src.mcp_handlers.dialectic.handlers._has_orchestrated_reviewer_budget",
            side_effect=lambda _row, _rid: is_orchestrated,
        ), patch(
            "src.mcp_handlers.dialectic.rejection_stands.continuation_window_s",
            return_value=(300.0, 900.0),
        ), patch(
            "src.mcp_handlers.dialectic.rejection_stands.assess_rejection_stands",
            side_effect=_pinned_assess,
        ):
            closed_ids = asyncio.run(
                _close_standing_rejections(
                    _CycleCounts(),
                    [],
                    [row],
                    decision_read_ts=eval_now,
                )
            )
        closed = session.session_id in closed_ids
        if closed or resolve_calls:
            assert closed is True
            assert len(resolve_calls) == 1
            call = resolve_calls[0]
            assert call["session_id"] == session.session_id
            assert call["status"] == "failed", (
                f"Expected standing rejection write status='failed', got {call['status']!r}"
            )
            kw = call["kwargs"]
            assert kw.get("clear_facilitation") is True, (
                "Standing rejection terminal write must pass clear_facilitation=True"
            )
            assert kw.get("exclude_inflight_saga") is True
            assert kw.get("expected_phase") == initial_row_snapshot.get("phase")
            assert kw.get("expected_updated_at") == initial_row_snapshot.get("updated_at")
            assert kw.get("expected_reviewer") == initial_row_snapshot.get("reviewer_agent_id")
            assert kw.get("expected_awaiting_facilitation") == bool(
                initial_row_snapshot.get("awaiting_facilitation")
            )
            res_payload = call["resolution"]
            assert isinstance(res_payload, dict)
            assert res_payload.get("action") == "rejection_stands"
            assert res_payload.get("reviewer_approved") is False
            # Both the DB callback (`persisted_row`) and `_sync_cached_session`
            # (`session`) must independently agree on the terminal state.
            assert persisted_row["phase"] == session.phase.value == "failed"
            assert persisted_row["awaiting_facilitation"] is False
            assert session.awaiting_facilitation is False
        else:
            assert len(resolve_calls) == 0
        return closed
    finally:
        ACTIVE_SESSIONS.pop(session.session_id, None)


# ---------------------------------------------------------------------------
# Parsed `#guard` Trace Conformance Between `formal/DialecticProtocol.lean`,
# `SpecState`, and Live `DialecticSession`
# ---------------------------------------------------------------------------


def test_lean4_guard_scenarios_match_python_mirror_and_live_session():
    """
    Parse `traceScenario*` and `#guard` assertions directly from
    `formal/DialecticProtocol.lean` and execute them against both `SpecState`
    and the live Python `DialecticSession` / `handle_reassign_reviewer` /
    `_close_standing_rejections` implementation.
    """
    code_only = _strip_lean_comments(LEAN_SPEC_PATH.read_text(encoding="utf-8"))

    scenario_blocks = re.findall(
        r"def\s+(traceScenario\d+)\s*:\s*State\s*:=\s*(.*?)(?=\n#guard|\ndef\s+traceScenario|\nend\s+Unitares)",
        code_only,
        flags=re.DOTALL,
    )
    assert len(scenario_blocks) >= 3, "Expected at least 3 `traceScenario*` blocks in Lean file"

    scenarios_spec: Dict[str, SpecState] = {}
    scenarios_live: Dict[str, DialecticSession] = {}

    base_clock = datetime(2026, 11, 5, 12, 0, 0, tzinfo=timezone.utc)
    tick = 0

    def next_ts() -> str:
        nonlocal tick
        tick += 1
        return (base_clock + timedelta(seconds=tick)).isoformat()

    for name, body in scenario_blocks:
        init_match = re.search(
            r"mkInitState\s+(\d+)\s+(\(some\s+\d+\)|none)\s+\[([^\]]*)\]\s+\.(\w+)\s+(\d+)",
            body,
        )
        assert init_match, f"Could not parse `mkInitState` in {name}"
        paused_idx = int(init_match.group(1))
        rev_token = init_match.group(2).strip()
        rev_idx = (
            None
            if rev_token == "none"
            else int(re.search(r"\d+", rev_token).group(0))
        )
        authors_raw = init_match.group(3).strip()
        authors = (
            tuple(int(x.strip()) for x in authors_raw.split(",") if x.strip())
            if authors_raw
            else ()
        )
        session_type = init_match.group(4)
        max_rounds = int(init_match.group(5))

        spec = SpecState(
            paused_agent=paused_idx,
            reviewer_agent=rev_idx,
            declared_authors=authors,
            session_type=session_type,
            max_rounds=max_rounds,
        )
        decl = declaration_for_request(
            [AGENT_UUIDS[i] for i in authors],
            declared_by_agent_id=AGENT_UUIDS[paused_idx],
        )
        session = DialecticSession(
            paused_agent_id=AGENT_UUIDS[paused_idx],
            reviewer_agent_id=(AGENT_UUIDS[rev_idx] if rev_idx is not None else None),
            paused_agent_state=embed_reviewed_work_authorship({"risk_score": 0.65}, decl),
            session_type=session_type,
            max_synthesis_rounds=max_rounds,
        )

        for line in body.splitlines():
            line = line.strip()
            if not line.startswith("let s"):
                continue
            if "mkInitState" in line:
                continue

            sender_match = re.search(r"sender\s*:=\s*(\d+)", line)
            rc_match = re.search(r"hasRootCause\s*:=\s*(true|false)", line)
            cond_match = re.search(r"hasConditions\s*:=\s*(true|false)", line)
            agrees_match = re.search(
                r"agrees\s*:=\s*(none|some\s+true|some\s+false)", line
            )
            if "submitThesis" in line and sender_match:
                sender = int(sender_match.group(1))
                has_rc = rc_match.group(1) == "true" if rc_match else True
                has_cond = cond_match.group(1) == "true" if cond_match else True
                _, spec = spec_submit_thesis(
                    spec,
                    SpecMessage(
                        phase="thesis",
                        sender=sender,
                        has_root_cause=has_rc,
                        has_conditions=has_cond,
                    ),
                )
                session.submit_thesis(
                    DialecticMessage(
                        phase="thesis",
                        agent_id=AGENT_UUIDS[sender],
                        timestamp=next_ts(),
                        root_cause="rc" if has_rc else "   ",
                        proposed_conditions=["c"] if has_cond else [],
                    )
                )
            elif "submitAntithesis" in line and sender_match:
                sender = int(sender_match.group(1))
                _, spec = spec_submit_antithesis(
                    spec,
                    SpecMessage(phase="antithesis", sender=sender),
                )
                session.submit_antithesis(
                    DialecticMessage(
                        phase="antithesis",
                        agent_id=AGENT_UUIDS[sender],
                        timestamp=next_ts(),
                        reasoning="r",
                    )
                )
            elif "submitSynthesis" in line and sender_match:
                sender = int(sender_match.group(1))
                agrees_raw = agrees_match.group(1) if agrees_match else "none"
                agrees = (
                    None
                    if agrees_raw == "none"
                    else (True if "true" in agrees_raw else False)
                )
                _, _, spec = spec_submit_synthesis(
                    spec,
                    SpecMessage(
                        phase="synthesis",
                        sender=sender,
                        agrees=agrees,
                    ),
                )
                session.submit_synthesis(
                    DialecticMessage(
                        phase="synthesis",
                        agent_id=AGENT_UUIDS[sender],
                        timestamp=next_ts(),
                        proposed_conditions=["c"],
                        root_cause="rc",
                        reasoning="r",
                        agrees=agrees,
                    )
                )
            elif "appendConsult" in line:
                ac_match = re.search(r"appendConsult\s+\w+\s+(\d+)", line)
                assert ac_match
                sender = int(ac_match.group(1))
                spec = spec_append_consult(spec, sender)
                session.transcript.append(
                    DialecticMessage(
                        phase="consult",
                        agent_id=AGENT_UUIDS[sender],
                        timestamp=next_ts(),
                        reasoning="consult note",
                    )
                )
            elif "closeStandingRejection" in line:
                cs_match = re.search(
                    r"closeStandingRejection\s+\w+\s+(true|false)\s+(true|false)",
                    line,
                )
                assert cs_match
                is_orch = cs_match.group(1) == "true"
                win_end = cs_match.group(2) == "true"
                closed, spec = spec_close_standing_rejection(spec, is_orch, win_end)
                eval_now = base_clock + timedelta(
                    seconds=(tick + 7200) if win_end else tick
                )
                live_closed = _apply_live_close_standing_rejection(
                    session, eval_now, is_orch
                )
                assert live_closed == closed
            elif "reassignReviewer" in line:
                rr_match = re.search(r"reassignReviewer\s+\w+\s+(\d+)", line)
                assert rr_match
                new_rev = int(rr_match.group(1))
                ok_spec, spec = spec_reassign_reviewer(spec, new_rev)
                ok_live = _apply_live_reassignment(session, AGENT_UUIDS[new_rev])
                assert ok_live == ok_spec

        scenarios_spec[name] = spec
        scenarios_live[name] = session

    guards = re.findall(r"#guard\s+([^\n]+)", code_only)
    assert len(guards) >= 12, "Expected at least 12 `#guard` assertions in Lean file"

    for guard_expr in guards:
        guard_expr = guard_expr.strip()
        m_phase = re.match(r"(traceScenario\d+)\.phase\s*==\s*\.(\w+)", guard_expr)
        m_fac = re.match(
            r"(traceScenario\d+)\.awaitingFacilitation\s*==\s*(true|false)",
            guard_expr,
        )
        m_round = re.match(
            r"(traceScenario\d+)\.synthesisRound\s*==\s*(\d+)", guard_expr
        )
        m_len = re.match(
            r"(traceScenario\d+)\.transcript\.length\s*==\s*(\d+)", guard_expr
        )
        m_rev = re.match(
            r"(traceScenario\d+)\.reviewerAgent\s*==\s*some\s+(\d+)", guard_expr
        )
        m_obj = re.match(
            r"reviewerObjectionStands\s+(traceScenario\d+)\s*==\s*(true|false)",
            guard_expr,
        )
        m_assess = re.match(
            r"assessRejectionStands\s+(traceScenario\d+)\s+(true|false)\s+(true|false)\s*==\s*(true|false)",
            guard_expr,
        )
        if m_phase:
            sc_name, expected_phase = m_phase.group(1), m_phase.group(2)
            assert scenarios_spec[sc_name].phase == expected_phase
            assert scenarios_live[sc_name].phase.value == expected_phase
        elif m_fac:
            sc_name = m_fac.group(1)
            expected_fac = m_fac.group(2) == "true"
            assert scenarios_spec[sc_name].awaiting_facilitation is expected_fac
            assert scenarios_live[sc_name].awaiting_facilitation is expected_fac
        elif m_round:
            sc_name = m_round.group(1)
            expected_round = int(m_round.group(2))
            assert scenarios_spec[sc_name].synthesis_round == expected_round
            assert scenarios_live[sc_name].synthesis_round == expected_round
        elif m_len:
            sc_name = m_len.group(1)
            expected_len = int(m_len.group(2))
            assert len(scenarios_spec[sc_name].transcript) == expected_len
            assert len(scenarios_live[sc_name].transcript) == expected_len
        elif m_rev:
            sc_name = m_rev.group(1)
            expected_rev = int(m_rev.group(2))
            assert scenarios_spec[sc_name].reviewer_agent == expected_rev
            assert (
                scenarios_live[sc_name].reviewer_agent_id == AGENT_UUIDS[expected_rev]
            )
        elif m_obj:
            sc_name = m_obj.group(1)
            expected_obj = m_obj.group(2) == "true"
            assert (
                spec_reviewer_objection_stands(scenarios_spec[sc_name]) is expected_obj
            )
            assert (
                scenarios_live[sc_name]._reviewer_objection_stands() is expected_obj
            )
        elif m_assess:
            sc_name = m_assess.group(1)
            is_orch = m_assess.group(2) == "true"
            win_end = m_assess.group(3) == "true"
            expected_assess = m_assess.group(4) == "true"
            assert (
                spec_assess_rejection_stands(
                    scenarios_spec[sc_name], is_orch, win_end
                )
                is expected_assess
            )
            eval_now = base_clock + timedelta(
                seconds=(tick + 7200) if win_end else tick
            )
            live_payload = assess_rejection_stands(
                scenarios_live[sc_name].transcript,
                paused_agent_id=scenarios_live[sc_name].paused_agent_id,
                reviewer_agent_id=scenarios_live[sc_name].reviewer_agent_id,
                awaiting_facilitation=scenarios_live[sc_name].awaiting_facilitation,
                now=eval_now,
                is_orchestrated=lambda _rev, _o=is_orch: _o,
                wait_s=300.0,
                total_s=900.0,
            )
            assert (live_payload is not None) is expected_assess
        else:
            pytest.fail(f"Unrecognized `#guard` expression in Lean file: {guard_expr}")


# ---------------------------------------------------------------------------
# Stateful Refinement & Invariant Model-Checking
# ---------------------------------------------------------------------------


class DialecticProtocolRefinementMachine(RuleBasedStateMachine):
    """
    Runs arbitrary interleavings of dialectic protocol actions against both
    `DialecticSession` and `SpecState`, checking state/transcript bisimulation
    via `@invariant` and transition-specific safety properties in each `@rule`.
    """

    def __init__(self):
        super().__init__()
        self.session: Optional[DialecticSession] = None
        self.spec: Optional[SpecState] = None
        self.clock: datetime = datetime(2026, 11, 5, 12, 0, 0, tzinfo=timezone.utc)

    def _next_timestamp(self, seconds: int = 1) -> str:
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
        reviewer_uuid = (
            AGENT_UUIDS[reviewer_choice] if reviewer_choice is not None else None
        )
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
        prev_transcript_len = len(self.session.transcript)
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
        if prev_phase in ("resolved", "failed"):
            assert self.session.phase.value == prev_phase
            assert len(self.session.transcript) == prev_transcript_len
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
        prev_round = self.session.synthesis_round
        prev_len = len(self.session.transcript)

        objection_before = self.session._reviewer_objection_stands()
        pending_before = self.session._reviewer_verdict_pending()
        answered_before = (
            self.session._paused_agent_already_answered_current_objection()
        )
        reviewer_recused_before = spec_assigned_reviewer_recused(self.spec)
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
        if prev_phase in ("resolved", "failed"):
            # Theorems 1a/1b: terminal states are absorbing under submit_synthesis
            assert res["success"] is False
            assert self.session.phase.value == prev_phase
            assert len(self.session.transcript) == prev_len

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

        # Theorem 9: First self-clear attempt routes to facilitation without resolving
        if (
            prev_phase == "synthesis"
            and not reviewer_recused_before
            and prev_round <= self.session.max_synthesis_rounds
            and not pending_before
            and objection_before
            and not answered_before
            and is_paused
            and agrees is True
        ):
            assert res["success"] is True
            assert not res.get("converged", False)
            assert self.session.phase == DialecticPhase.SYNTHESIS
            assert self.session.awaiting_facilitation is True

    @rule(new_reviewer_idx=st.integers(min_value=0, max_value=3))
    def step_reassign_reviewer(self, new_reviewer_idx: int):
        """
        Execute live `handle_reassign_reviewer` and `spec_reassign_reviewer`
        across all phases (including reopening `FAILED` sessions with
        `awaiting_facilitation=True` and rewinding conflicted `SYNTHESIS` sessions).
        """
        assert self.session is not None and self.spec is not None
        old_reviewer_idx = self.spec.reviewer_agent
        old_phase = self.session.phase.value
        old_rank = PHASE_RANK[old_phase]
        was_awaiting = self.session.awaiting_facilitation
        was_recused = spec_assigned_reviewer_recused(self.spec)
        candidate_recused = spec_is_recused(self.spec, new_reviewer_idx)
        candidate_is_paused = new_reviewer_idx == self.spec.paused_agent
        objection_before = self.session._reviewer_objection_stands()

        ok_live = _apply_live_reassignment(
            self.session, AGENT_UUIDS[new_reviewer_idx]
        )
        ok_spec, next_spec = spec_reassign_reviewer(self.spec, new_reviewer_idx)
        self.spec = next_spec

        assert ok_live == ok_spec

        # Theorem 6c & 6d: Recused candidate or paused agent (`SELF_REVIEW`) cannot be assigned
        if candidate_recused or candidate_is_paused:
            assert ok_live is False

        # Theorem 8: When not awaiting facilitation and not replacing a recused
        # reviewer, reassignment preserves phase rank
        if not was_awaiting and not was_recused:
            assert PHASE_RANK[self.session.phase.value] == old_rank

        # Theorem 5: Reassignment from an independent reviewer preserves standing
        # objection and never transitions a non-resolved session to resolved
        if old_reviewer_idx is not None and old_reviewer_idx != self.spec.paused_agent:
            assert self.session._reviewer_objection_stands() == objection_before
        if old_phase != "resolved":
            assert self.session.phase != DialecticPhase.RESOLVED

    @rule(sender_idx=st.integers(min_value=0, max_value=3))
    def step_append_consult_entry(self, sender_idx: int):
        """
        Theorem 7 (`consult_preserves_authority_predicates`): non-protocol `consult`
        transcript entries preserve phase, reviewer, round, facilitation flag,
        last-update timestamp, and all resolution-authority predicates.
        """
        assert self.session is not None and self.spec is not None
        phase_before = self.session.phase
        rev_before = self.session.reviewer_agent_id
        round_before = self.session.synthesis_round
        fac_before = self.session.awaiting_facilitation
        last_ts_before = self.session.get_last_update_timestamp()
        objection_before = self.session._reviewer_objection_stands()
        pending_before = self.session._reviewer_verdict_pending()
        answered_before = (
            self.session._paused_agent_already_answered_current_objection()
        )
        assess_before = (
            assess_rejection_stands(
                self.session.transcript,
                paused_agent_id=self.session.paused_agent_id,
                reviewer_agent_id=self.session.reviewer_agent_id,
                awaiting_facilitation=self.session.awaiting_facilitation,
                now=self.clock + timedelta(seconds=7200),
                is_orchestrated=lambda _rev: True,
                wait_s=300.0,
                total_s=900.0,
            )
            is not None
        )

        consult_msg = DialecticMessage(
            phase="consult",
            agent_id=AGENT_UUIDS[sender_idx],
            timestamp=self._next_timestamp(seconds=1),
            reasoning="External advisory consult note",
        )
        self.session.transcript.append(consult_msg)
        self.spec = spec_append_consult(self.spec, sender_idx)

        assert self.session.phase == phase_before
        assert self.session.reviewer_agent_id == rev_before
        assert self.session.synthesis_round == round_before
        assert self.session.awaiting_facilitation == fac_before
        assert self.session.get_last_update_timestamp() == last_ts_before
        assert self.session._reviewer_objection_stands() == objection_before
        assert self.session._reviewer_verdict_pending() == pending_before
        assert (
            self.session._paused_agent_already_answered_current_objection()
            == answered_before
        )
        assess_after = (
            assess_rejection_stands(
                self.session.transcript,
                paused_agent_id=self.session.paused_agent_id,
                reviewer_agent_id=self.session.reviewer_agent_id,
                awaiting_facilitation=self.session.awaiting_facilitation,
                now=self.clock + timedelta(seconds=7200),
                is_orchestrated=lambda _rev: True,
                wait_s=300.0,
                total_s=900.0,
            )
            is not None
        )
        assert assess_after == assess_before

    @rule(
        window_ended=st.booleans(),
        is_orchestrated=st.booleans(),
    )
    def step_assess_and_close_rejection_stands(
        self, window_ended: bool, is_orchestrated: bool
    ):
        """
        Verify `assess_rejection_stands` equivalence with `spec_assess_rejection_stands`
        and execute the real `_close_standing_rejections` sweeper (`spec_close_standing_rejection`).
        """
        assert self.session is not None and self.spec is not None
        eval_now = self.clock + timedelta(seconds=7200 if window_ended else 0)
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
        spec_holds = spec_assess_rejection_stands(
            self.spec, is_orchestrated=is_orchestrated, window_ended=window_ended
        )
        assert (payload is not None) == spec_holds

        if payload is not None:
            # Theorem 10: `assess_rejection_stands` only fires when awaiting facilitation
            # with a standing independent reviewer rejection from the current reviewer.
            assert self.session.awaiting_facilitation is True
            assert self.session._reviewer_objection_stands() is True
            assert self.session.reviewer_agent_id is not None
            assert self.session.reviewer_agent_id != self.session.paused_agent_id
            assert payload["action"] == REJECTION_STANDS
            assert payload["reason"] == REJECTION_STANDS

        # Execute real `_close_standing_rejections` (`auto_resolve.py:512-655`)
        closed_spec, next_spec = spec_close_standing_rejection(
            self.spec, is_orchestrated=is_orchestrated, window_ended=window_ended
        )
        live_closed = _apply_live_close_standing_rejection(
            self.session, eval_now=eval_now, is_orchestrated=is_orchestrated
        )
        assert live_closed == closed_spec
        self.spec = next_spec
        if live_closed:
            assert self.session.phase == DialecticPhase.FAILED
            assert self.session.awaiting_facilitation is False

    @invariant()
    def check_refinement_bisimulation(self):
        """Live `DialecticSession` and Lean mirror `SpecState` agree on all state projections and transcript contents."""
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
        for live_m, spec_m in zip(self.session.transcript, self.spec.transcript):
            assert live_m.phase == spec_m.phase
            assert live_m.agent_id == AGENT_UUIDS[spec_m.sender]
            assert live_m.agrees == spec_m.agrees

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
