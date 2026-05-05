"""R2 PR 2: lineage lifecycle FSM tests.

Covers the provisional → {confirmed, demoted, archived} state machine
in `src/identity/lineage_lifecycle.py`. The FSM orchestrates R1
(`score_trajectory_continuity`), the storage helpers from PR 1, and
the audit-event emission for the five lineage-lifecycle event types.

Most tests run against the autouse mocked DB backend — the FSM is a
pure orchestrator over backend methods, so mock-based tests exercise
every transition and side-effect cleanly. One DB-touching test
(`test_read_lineage_state_*`) verifies the new
`read_lineage_state` helper's single-query shape against a live
postgres backend.

See: docs/handoffs/2026-05-04-r2-implementation-plan.md PR 2
     docs/ontology/r2-honest-memory-integration.md
       §"Promotion / demotion / archival protocol"
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest

from src.identity import lineage_lifecycle
from src.identity.trajectory_continuity import TrajectoryContinuityScore


PARENT = "parent-uuid-aaaaaaaaaaaa"
SUCCESSOR = "successor-uuid-bbbbbbbb"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _provisional_row(
    *,
    declared_at: Optional[datetime] = None,
    last_eval_at: Optional[datetime] = None,
    parent_id: Optional[str] = PARENT,
) -> Dict[str, Any]:
    """A `read_lineage_state` row for a provisional successor."""
    return {
        "parent_agent_id": parent_id,
        "provisional_lineage": True,
        "confirmed_at": None,
        "lineage_declared_at": declared_at,
        "lineage_demoted_at": None,
        "lineage_archived_at": None,
        "lineage_last_eval_at": last_eval_at,
        "chain_obs_count": 0,
    }


def _confirmed_row(
    *,
    last_eval_at: Optional[datetime] = None,
    parent_id: Optional[str] = PARENT,
    chain_obs_count: int = 5,
) -> Dict[str, Any]:
    """A `read_lineage_state` row for a confirmed successor."""
    return {
        "parent_agent_id": parent_id,
        "provisional_lineage": False,
        "confirmed_at": datetime.now(timezone.utc) - timedelta(days=1),
        "lineage_declared_at": datetime.now(timezone.utc) - timedelta(days=1),
        "lineage_demoted_at": None,
        "lineage_archived_at": None,
        "lineage_last_eval_at": last_eval_at,
        "chain_obs_count": chain_obs_count,
    }


def _build_score(verdict: str, *, plausibility: float = 0.8) -> TrajectoryContinuityScore:
    """Construct a TrajectoryContinuityScore for FSM tests.

    The FSM only inspects `verdict`, `score_id`, and `plausibility`
    on the returned object — other fields are filled with shape-valid
    placeholders so the dataclass constructor accepts them.
    """
    return TrajectoryContinuityScore(
        score_id="00000000-0000-0000-0000-000000000abc",
        plausibility=plausibility,
        verdict=verdict,  # type: ignore[arg-type]
        observations={"parent": {"E": 30, "I": 30, "S": 30, "V": 30},
                      "successor": {"E": 30, "I": 30, "S": 30, "V": 30}},
        components={"E": 0.8, "I": 0.8, "S": 0.8, "V": 0.8},
        reasons=[],
        parent_mature=True,
        calibration_status="seeded",
        n_dims_used=4,
    )


@pytest.fixture
def captured_audit(monkeypatch) -> List[Dict[str, Any]]:
    """Spy on `_emit_audit` calls — captures the entries the FSM
    would have written to `audit.events`.

    Patching the module-local helper keeps us independent of
    `src.audit_db` import order: the FSM lazy-imports
    `append_audit_event_async` at call time, but the module-level
    `_emit_audit` is the actual call site we care about.
    """
    captured: List[Dict[str, Any]] = []

    async def fake_emit(event_type: str, agent_id: str, *, details: Dict[str, Any]) -> None:
        captured.append({
            "event_type": event_type,
            "agent_id": agent_id,
            "details": details,
        })

    monkeypatch.setattr(lineage_lifecycle, "_emit_audit", fake_emit)
    return captured


@pytest.fixture
def mocked_score(monkeypatch):
    """Inject a fake `score_trajectory_continuity` returning the
    verdict the test specifies. Tracks call count so cadence-guard
    tests can assert R1 was not invoked."""
    state: Dict[str, Any] = {"score": None, "calls": 0}

    async def fake_score(parent_id, successor_id, *, min_observations=5):
        state["calls"] += 1
        state["last_args"] = (parent_id, successor_id, min_observations)
        return state["score"]

    monkeypatch.setattr(
        lineage_lifecycle, "score_trajectory_continuity", fake_score,
    )
    return state


# ---------------------------------------------------------------------------
# 1. provisional → confirmed on plausible
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provisional_promoted_on_plausible(mocked_score, captured_audit):
    """R1 plausible verdict on a provisional row → confirm + promote audit."""
    from src.db import get_db
    backend = get_db()
    backend.read_lineage_state = AsyncMock(return_value=_provisional_row(
        declared_at=datetime.now(timezone.utc) - timedelta(hours=2),
    ))
    mocked_score["score"] = _build_score("plausible", plausibility=0.85)

    outcome = await lineage_lifecycle.evaluate_lineage_for(SUCCESSOR)

    assert outcome.terminal_state == "confirmed"
    assert outcome.transition == "provisional_to_confirmed"
    assert outcome.r1_verdict == "plausible"
    backend.confirm_lineage.assert_awaited_once_with(SUCCESSOR)
    backend.stamp_lineage_eval.assert_awaited_with(SUCCESSOR)
    assert any(c["event_type"] == "lineage_promoted" for c in captured_audit)
    promoted = [c for c in captured_audit if c["event_type"] == "lineage_promoted"][0]
    assert promoted["details"]["parent_id"] == PARENT
    assert promoted["details"]["score_id"] == "00000000-0000-0000-0000-000000000abc"
    assert promoted["details"]["plausibility"] == 0.85


# ---------------------------------------------------------------------------
# 2. provisional → demoted on unsupported (r1_unsupported reason)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provisional_demoted_on_unsupported(mocked_score, captured_audit):
    """R1 unsupported verdict on provisional → demote with
    reason=r1_unsupported."""
    from src.db import get_db
    backend = get_db()
    backend.read_lineage_state = AsyncMock(return_value=_provisional_row(
        declared_at=datetime.now(timezone.utc) - timedelta(hours=2),
    ))
    mocked_score["score"] = _build_score("unsupported", plausibility=0.20)

    outcome = await lineage_lifecycle.evaluate_lineage_for(SUCCESSOR)

    assert outcome.terminal_state == "demoted"
    assert outcome.transition == "provisional_to_demoted"
    assert outcome.r1_verdict == "unsupported"
    backend.demote_lineage.assert_awaited_once_with(
        SUCCESSOR, reason="r1_unsupported",
    )
    assert any(c["event_type"] == "lineage_demoted" for c in captured_audit)
    demoted = [c for c in captured_audit if c["event_type"] == "lineage_demoted"][0]
    assert demoted["details"]["reason"] == "r1_unsupported"
    assert demoted["details"]["parent_id"] == PARENT
    assert demoted["details"]["score_id"] == "00000000-0000-0000-0000-000000000abc"


# ---------------------------------------------------------------------------
# 3. provisional + inconclusive → no transition, no audit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provisional_inconclusive_no_transition(mocked_score, captured_audit):
    """Inconclusive verdict leaves the row alone; only the cadence
    stamp is updated so the next sweeper tick honors the cadence guard."""
    from src.db import get_db
    backend = get_db()
    backend.read_lineage_state = AsyncMock(return_value=_provisional_row(
        declared_at=datetime.now(timezone.utc) - timedelta(hours=2),
    ))
    mocked_score["score"] = _build_score("inconclusive", plausibility=0.60)

    outcome = await lineage_lifecycle.evaluate_lineage_for(SUCCESSOR)

    assert outcome.terminal_state is None
    assert outcome.transition is None
    assert outcome.r1_verdict == "inconclusive"
    backend.confirm_lineage.assert_not_awaited()
    backend.demote_lineage.assert_not_awaited()
    backend.archive_lineage.assert_not_awaited()
    assert captured_audit == []


# ---------------------------------------------------------------------------
# 4. provisional + grace expired → archived (R1 NOT called)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provisional_archived_on_grace_expiration(
    mocked_score, captured_audit,
):
    """Once a provisional row's declaration is older than the grace
    window, the FSM archives it without scoring. This keeps the
    sweeper from spending R1 cycles on edges that exhausted their
    verification budget."""
    from src.db import get_db
    backend = get_db()
    declared = datetime.now(timezone.utc) - timedelta(days=45)
    backend.read_lineage_state = AsyncMock(return_value=_provisional_row(
        declared_at=declared,
    ))

    outcome = await lineage_lifecycle.evaluate_lineage_for(
        SUCCESSOR, grace_window=timedelta(days=30),
    )

    assert outcome.terminal_state == "archived"
    assert outcome.transition == "provisional_to_archived"
    assert outcome.r1_verdict is None
    # R1 must not be invoked once grace is blown.
    assert mocked_score["calls"] == 0
    backend.archive_lineage.assert_awaited_once_with(SUCCESSOR)
    backend.stamp_lineage_eval.assert_awaited_with(SUCCESSOR)
    assert any(c["event_type"] == "lineage_grace_expired" for c in captured_audit)
    expired = [c for c in captured_audit if c["event_type"] == "lineage_grace_expired"][0]
    assert expired["details"]["parent_id"] == PARENT
    assert expired["details"]["declared_at"] == declared.isoformat()


# ---------------------------------------------------------------------------
# 5. cadence guard skips eval entirely
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cadence_guard_skips_eval(mocked_score, captured_audit):
    """If `lineage_last_eval_at` is within the cadence window, the
    FSM short-circuits without calling R1 or touching storage."""
    from src.db import get_db
    backend = get_db()
    recent = datetime.now(timezone.utc) - timedelta(minutes=10)
    backend.read_lineage_state = AsyncMock(return_value=_provisional_row(
        declared_at=datetime.now(timezone.utc) - timedelta(hours=5),
        last_eval_at=recent,
    ))

    outcome = await lineage_lifecycle.evaluate_lineage_for(
        SUCCESSOR, eval_cadence=timedelta(hours=1),
    )

    assert outcome.skipped_reason == "within_cadence"
    assert outcome.terminal_state is None
    assert outcome.transition is None
    assert outcome.r1_verdict is None
    assert mocked_score["calls"] == 0
    backend.confirm_lineage.assert_not_awaited()
    backend.demote_lineage.assert_not_awaited()
    backend.archive_lineage.assert_not_awaited()
    backend.stamp_lineage_eval.assert_not_awaited()
    assert captured_audit == []


# ---------------------------------------------------------------------------
# 6. confirmed → demoted on unsupported (post-promotion divergence)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirmed_demoted_on_unsupported_post_promotion(
    mocked_score, captured_audit,
):
    """A confirmed row that re-scores unsupported demotes with reason
    `post_promotion_divergence`. `demote_lineage` (PR 1) clamps
    chain_obs_count back to 0 inside the same UPDATE."""
    from src.db import get_db
    backend = get_db()
    backend.read_lineage_state = AsyncMock(return_value=_confirmed_row(
        chain_obs_count=12,
    ))
    mocked_score["score"] = _build_score("unsupported", plausibility=0.30)

    outcome = await lineage_lifecycle.evaluate_lineage_for(SUCCESSOR)

    assert outcome.terminal_state == "demoted"
    assert outcome.transition == "confirmed_to_demoted"
    backend.demote_lineage.assert_awaited_once_with(
        SUCCESSOR, reason="post_promotion_divergence",
    )
    assert any(c["event_type"] == "lineage_demoted" for c in captured_audit)
    demoted = [c for c in captured_audit if c["event_type"] == "lineage_demoted"][0]
    assert demoted["details"]["reason"] == "post_promotion_divergence"


# ---------------------------------------------------------------------------
# 7. confirmed + plausible → no-op (don't re-fire lineage_promoted)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirmed_plausible_no_op(mocked_score, captured_audit):
    """Re-scoring an already-confirmed row as plausible must NOT
    re-emit `lineage_promoted` — promotion is a one-shot event."""
    from src.db import get_db
    backend = get_db()
    backend.read_lineage_state = AsyncMock(return_value=_confirmed_row())
    mocked_score["score"] = _build_score("plausible", plausibility=0.90)

    outcome = await lineage_lifecycle.evaluate_lineage_for(SUCCESSOR)

    assert outcome.terminal_state is None
    assert outcome.transition is None
    assert outcome.r1_verdict == "plausible"
    backend.confirm_lineage.assert_not_awaited()
    backend.demote_lineage.assert_not_awaited()
    assert not any(c["event_type"] == "lineage_promoted" for c in captured_audit)


# ---------------------------------------------------------------------------
# 8. no parent → skipped, R1 NOT called
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_parent_skips_with_reason(mocked_score, captured_audit):
    """A row with `parent_agent_id IS NULL` is not a lineage edge —
    nothing for the FSM to evaluate."""
    from src.db import get_db
    backend = get_db()
    backend.read_lineage_state = AsyncMock(return_value=_provisional_row(
        parent_id=None,
        declared_at=None,
    ))

    outcome = await lineage_lifecycle.evaluate_lineage_for(SUCCESSOR)

    assert outcome.skipped_reason == "no_parent"
    assert outcome.parent_id is None
    assert outcome.terminal_state is None
    assert mocked_score["calls"] == 0
    assert captured_audit == []


@pytest.mark.asyncio
async def test_unknown_agent_skips_no_parent(mocked_score, captured_audit):
    """`read_lineage_state` returns None for unknown agents — same
    no_parent skip path."""
    from src.db import get_db
    backend = get_db()
    backend.read_lineage_state = AsyncMock(return_value=None)

    outcome = await lineage_lifecycle.evaluate_lineage_for("nonexistent")

    assert outcome.skipped_reason == "no_parent"
    assert mocked_score["calls"] == 0
    assert captured_audit == []


# ---------------------------------------------------------------------------
# 9. stamp_lineage_eval is called even on inconclusive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stamp_lineage_eval_fires_on_inconclusive(mocked_score, captured_audit):
    """Cadence stamp must update on every R1 invocation, regardless
    of verdict — otherwise an inconclusive row would re-fire R1 on
    the next sweeper tick and the cadence guard would be useless."""
    from src.db import get_db
    backend = get_db()
    backend.read_lineage_state = AsyncMock(return_value=_provisional_row(
        declared_at=datetime.now(timezone.utc) - timedelta(hours=2),
    ))
    mocked_score["score"] = _build_score("inconclusive", plausibility=0.60)

    await lineage_lifecycle.evaluate_lineage_for(SUCCESSOR)

    backend.stamp_lineage_eval.assert_awaited_with(SUCCESSOR)


# ---------------------------------------------------------------------------
# 10. conftest stub regression — read_lineage_state mock returns dict-or-None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conftest_read_lineage_state_default_returns_none():
    """Meta-regression: the autouse mock backend returns None from
    `read_lineage_state` by default — not an AsyncMock auto-child
    coroutine, which would leak as an unawaited coroutine warning at
    teardown (R1 v3.2-E pattern)."""
    from src.db import get_db
    backend = get_db()
    result = await backend.read_lineage_state("any-agent-id")
    assert result is None


# ---------------------------------------------------------------------------
# 11. Council fix 1b: terminal-state short-circuit at top of FSM
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_state_short_circuits_fsm(mocked_score, captured_audit):
    """If `read_lineage_state` returns a row already in a terminal
    state (archived or demoted), the FSM short-circuits with
    skipped_reason='terminal_state' WITHOUT calling R1, touching
    storage, or emitting an audit event.

    Catches the bug where a stale check-in trigger could fire R1 on an
    archived row, then call confirm_lineage and (pre-WHERE-guard) flip
    a terminal row into a corrupt dual-stamped state."""
    from src.db import get_db
    backend = get_db()
    archived_row = _provisional_row(
        declared_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    archived_row["lineage_archived_at"] = datetime.now(timezone.utc)
    archived_row["provisional_lineage"] = False  # archive_lineage clears this
    backend.read_lineage_state = AsyncMock(return_value=archived_row)
    mocked_score["score"] = _build_score("plausible", plausibility=0.85)

    outcome = await lineage_lifecycle.evaluate_lineage_for(SUCCESSOR)

    assert outcome.skipped_reason == "terminal_state"
    assert outcome.terminal_state is None
    assert outcome.transition is None
    assert outcome.r1_verdict is None
    # R1 must not be called for terminal rows.
    assert mocked_score["calls"] == 0
    # No storage transitions, no cadence stamp.
    backend.confirm_lineage.assert_not_awaited()
    backend.demote_lineage.assert_not_awaited()
    backend.archive_lineage.assert_not_awaited()
    backend.stamp_lineage_eval.assert_not_awaited()
    assert captured_audit == []


@pytest.mark.asyncio
async def test_terminal_state_short_circuits_fsm_demoted(
    mocked_score, captured_audit,
):
    """Symmetric: a demoted row also short-circuits."""
    from src.db import get_db
    backend = get_db()
    demoted_row = _provisional_row(
        declared_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    demoted_row["lineage_demoted_at"] = datetime.now(timezone.utc)
    demoted_row["provisional_lineage"] = False
    backend.read_lineage_state = AsyncMock(return_value=demoted_row)
    mocked_score["score"] = _build_score("unsupported", plausibility=0.10)

    outcome = await lineage_lifecycle.evaluate_lineage_for(SUCCESSOR)

    assert outcome.skipped_reason == "terminal_state"
    assert mocked_score["calls"] == 0
    backend.demote_lineage.assert_not_awaited()
    assert captured_audit == []


# ---------------------------------------------------------------------------
# 12. Council fix 2: storage helper guard-skip degrades outcome to skipped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_lineage_no_op_returns_skipped_not_confirmed(
    mocked_score, captured_audit,
):
    """If `confirm_lineage` returns False (its WHERE guard fired due to
    a concurrent archive/demote landing between read and write), the
    FSM must NOT report `terminal_state="confirmed"` and must NOT emit
    a `lineage_promoted` audit. Outcome carries
    skipped_reason='confirm_skipped_terminal_state'.

    Stamping cadence still happens — the row was scored, so the
    cadence guard should fire on the next tick."""
    from src.db import get_db
    backend = get_db()
    backend.read_lineage_state = AsyncMock(return_value=_provisional_row(
        declared_at=datetime.now(timezone.utc) - timedelta(hours=2),
    ))
    backend.confirm_lineage = AsyncMock(return_value=False)
    mocked_score["score"] = _build_score("plausible", plausibility=0.85)

    outcome = await lineage_lifecycle.evaluate_lineage_for(SUCCESSOR)

    assert outcome.terminal_state is None
    assert outcome.transition is None
    assert outcome.skipped_reason == "confirm_skipped_terminal_state"
    assert outcome.r1_verdict == "plausible"
    backend.confirm_lineage.assert_awaited_once_with(SUCCESSOR)
    backend.stamp_lineage_eval.assert_awaited_once_with(SUCCESSOR)
    assert not any(c["event_type"] == "lineage_promoted" for c in captured_audit)


@pytest.mark.asyncio
async def test_demote_lineage_no_op_returns_skipped_not_demoted(
    mocked_score, captured_audit,
):
    """Symmetric to the confirm-skip test: if `demote_lineage` returns
    False (WHERE guard fired), the FSM must NOT report
    `terminal_state="demoted"` and must NOT emit `lineage_demoted`."""
    from src.db import get_db
    backend = get_db()
    backend.read_lineage_state = AsyncMock(return_value=_provisional_row(
        declared_at=datetime.now(timezone.utc) - timedelta(hours=2),
    ))
    backend.demote_lineage = AsyncMock(return_value=False)
    mocked_score["score"] = _build_score("unsupported", plausibility=0.10)

    outcome = await lineage_lifecycle.evaluate_lineage_for(SUCCESSOR)

    assert outcome.terminal_state is None
    assert outcome.transition is None
    assert outcome.skipped_reason == "demote_skipped_terminal_state"
    assert outcome.r1_verdict == "unsupported"
    backend.demote_lineage.assert_awaited_once()
    backend.stamp_lineage_eval.assert_awaited_once_with(SUCCESSOR)
    assert not any(c["event_type"] == "lineage_demoted" for c in captured_audit)


# ---------------------------------------------------------------------------
# 13. Council fix 3: R1 exception → skipped + cadence stamp (no tight loop)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r1_exception_returns_skipped_and_stamps_eval(
    monkeypatch, captured_audit,
):
    """If `score_trajectory_continuity` raises (e.g. its internal audit
    write fails per `trajectory_continuity.py` ~line 355), the FSM
    must:
    - return outcome with skipped_reason='r1_error'
    - NOT transition the row
    - NOT emit an audit event
    - stamp `lineage_last_eval_at` so the cadence guard prevents
      tight-looping on persistent errors

    Catches `Exception` (not `BaseException`), so `CancelledError`
    still propagates."""
    from src.db import get_db
    backend = get_db()
    backend.read_lineage_state = AsyncMock(return_value=_provisional_row(
        declared_at=datetime.now(timezone.utc) - timedelta(hours=2),
    ))

    async def raising_score(parent_id, successor_id, *, min_observations=5):
        raise RuntimeError("simulated R1 audit failure")

    monkeypatch.setattr(
        lineage_lifecycle, "score_trajectory_continuity", raising_score,
    )

    outcome = await lineage_lifecycle.evaluate_lineage_for(SUCCESSOR)

    assert outcome.skipped_reason == "r1_error"
    assert outcome.terminal_state is None
    assert outcome.transition is None
    assert outcome.r1_verdict is None
    backend.stamp_lineage_eval.assert_awaited_with(SUCCESSOR)
    backend.confirm_lineage.assert_not_awaited()
    backend.demote_lineage.assert_not_awaited()
    assert captured_audit == []


# ---------------------------------------------------------------------------
# DB-touching: read_lineage_state shape against live postgres
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_lineage_state_returns_dict_for_seeded_pair(live_postgres_backend):
    """Smoke test for the new backend helper — provisional pair
    returns a dict with the FSM's expected keys + values."""
    from tests.db.conftest import _insert_identity, _cleanup, _uuid_suffix

    parent_id = "test-parent-" + _uuid_suffix()
    successor_id = "test-successor-" + _uuid_suffix()
    try:
        await _insert_identity(live_postgres_backend, parent_id)
        await _insert_identity(
            live_postgres_backend, successor_id,
            parent_agent_id=parent_id, provisional_lineage=True,
        )
        state = await live_postgres_backend.read_lineage_state(successor_id)
        assert state is not None
        assert state["parent_agent_id"] == parent_id
        assert state["provisional_lineage"] is True
        assert state["confirmed_at"] is None
        assert state["lineage_declared_at"] is not None
        assert state["lineage_demoted_at"] is None
        assert state["lineage_archived_at"] is None
        assert state["chain_obs_count"] == 0
    finally:
        await _cleanup(live_postgres_backend, [parent_id, successor_id])


@pytest.mark.asyncio
async def test_read_lineage_state_returns_none_for_unknown_agent(live_postgres_backend):
    """Single-query path returns None for missing rows."""
    state = await live_postgres_backend.read_lineage_state("nonexistent-xyz")
    assert state is None
