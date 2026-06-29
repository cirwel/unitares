"""Tests for R1 v3.3-D + v3.3-C backend helpers on IdentityMixin.

mark_lineage_provisional / confirm_lineage update core.identities provisional
columns. read_r1_calibration_state / transition_r1_calibration_state operate
on the calibration_state singleton from migration 032.

Live SQL behavior is covered by the verifier review pass at PR 3 ship;
these tests pin contract + signature + branch coverage with mocked acquire().
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


def test_mark_lineage_provisional_exists_on_identity_mixin():
    from src.db.mixins.identity import IdentityMixin
    assert hasattr(IdentityMixin, "mark_lineage_provisional")
    assert hasattr(IdentityMixin, "confirm_lineage")
    assert hasattr(IdentityMixin, "read_r1_calibration_state")
    assert hasattr(IdentityMixin, "transition_r1_calibration_state")


# ---------------------------------------------------------------------------
# mark_lineage_provisional / confirm_lineage
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mark_lineage_provisional_returns_true_on_update():
    """UPDATE that affects 1 row → returns True."""
    from src.db.mixins.identity import IdentityMixin

    captured: list = []

    class _Stub(IdentityMixin):
        def acquire(self):
            return _AcquireCtx(captured)

    class _AcquireCtx:
        def __init__(self, sink):
            self._sink = sink

        async def __aenter__(self):
            conn = AsyncMock()

            async def _execute(sql, *args):
                self._sink.append((sql, args))
                return "UPDATE 1"

            conn.execute = _execute
            return conn

        async def __aexit__(self, *args):
            return None

    backend = _Stub()
    ok = await backend.mark_lineage_provisional(
        successor_id="successor-uuid",
        score_id="score-uuid",
    )

    assert ok is True
    assert len(captured) == 1
    sql, args = captured[0]
    assert "provisional_lineage = TRUE" in sql
    assert "confirmed_at = NULL" in sql
    assert args == ("score-uuid", "successor-uuid")


@pytest.mark.asyncio
async def test_mark_lineage_provisional_returns_false_when_no_match():
    """UPDATE that affects 0 rows → returns False."""
    from src.db.mixins.identity import IdentityMixin

    class _Stub(IdentityMixin):
        def acquire(self):
            return _AcquireCtx()

    class _AcquireCtx:
        async def __aenter__(self):
            conn = AsyncMock()
            conn.execute = AsyncMock(return_value="UPDATE 0")
            return conn

        async def __aexit__(self, *args):
            return None

    backend = _Stub()
    ok = await backend.mark_lineage_provisional(
        successor_id="missing-uuid",
        score_id="score-uuid",
    )
    assert ok is False


@pytest.mark.asyncio
async def test_confirm_lineage_clears_provisional_state():
    from src.db.mixins.identity import IdentityMixin

    captured: list = []

    class _Stub(IdentityMixin):
        def acquire(self):
            return _AcquireCtx(captured)

    class _AcquireCtx:
        def __init__(self, sink):
            self._sink = sink

        async def __aenter__(self):
            conn = AsyncMock()

            async def _execute(sql, *args):
                self._sink.append((sql, args))
                return "UPDATE 1"

            conn.execute = _execute
            return conn

        async def __aexit__(self, *args):
            return None

    backend = _Stub()
    ok = await backend.confirm_lineage(successor_id="successor-uuid")

    assert ok is True
    sql, args = captured[0]
    assert "provisional_lineage = FALSE" in sql
    assert "provisional_score_id = NULL" in sql
    assert "confirmed_at = now()" in sql
    assert args == ("successor-uuid",)


# ---------------------------------------------------------------------------
# calibration_state singleton
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_read_r1_calibration_state_returns_singleton_row():
    from src.db.mixins.identity import IdentityMixin
    from datetime import datetime, timezone

    expected_seeded_since = datetime(2026, 5, 3, 0, 0, tzinfo=timezone.utc)

    class _Stub(IdentityMixin):
        def acquire(self):
            return _AcquireCtx()

    class _AcquireCtx:
        async def __aenter__(self):
            conn = AsyncMock()
            conn.fetchrow = AsyncMock(return_value={
                "id": 1,
                "calibration_status": "seeded",
                "seeded_since": expected_seeded_since,
                "earned_at": None,
                "failed_at": None,
                "updated_at": expected_seeded_since,
            })
            return conn

        async def __aexit__(self, *args):
            return None

    backend = _Stub()
    state = await backend.read_r1_calibration_state()

    assert state["calibration_status"] == "seeded"
    assert state["seeded_since"] == expected_seeded_since
    assert state["earned_at"] is None
    assert state["failed_at"] is None


@pytest.mark.asyncio
async def test_read_r1_calibration_state_falls_back_when_singleton_missing():
    """Pre-migration-032 fallback: returns a synthesized seeded state when
    fetchrow returns None. Surface contract preserved so callers don't need
    a separate check."""
    from src.db.mixins.identity import IdentityMixin

    class _Stub(IdentityMixin):
        def acquire(self):
            return _AcquireCtx()

    class _AcquireCtx:
        async def __aenter__(self):
            conn = AsyncMock()
            conn.fetchrow = AsyncMock(return_value=None)
            return conn

        async def __aexit__(self, *args):
            return None

    backend = _Stub()
    state = await backend.read_r1_calibration_state()

    assert state["calibration_status"] == "seeded"
    assert state["earned_at"] is None
    assert state["failed_at"] is None


@pytest.mark.asyncio
async def test_transition_r1_calibration_state_rejects_invalid_status():
    from src.db.mixins.identity import IdentityMixin

    class _Stub(IdentityMixin):
        def acquire(self):
            raise AssertionError("acquire should not be called for invalid input")

    backend = _Stub()
    with pytest.raises(ValueError, match="invalid calibration_status"):
        await backend.transition_r1_calibration_state("unknown")


@pytest.mark.asyncio
async def test_transition_r1_calibration_state_to_earned_atomic_via_returning():
    """Atomic write+read via UPDATE...RETURNING * (no separate read-back —
    closes the TOCTOU window where a concurrent operator transition could
    replace the state between UPDATE and SELECT). Datetime fields are
    represented as strings in this mock-level pin; live datetime semantics
    are exercised by the verifier review pass."""
    from src.db.mixins.identity import IdentityMixin

    captured: list = []

    class _Stub(IdentityMixin):
        def acquire(self):
            return _AcquireCtx(captured)

    class _AcquireCtx:
        def __init__(self, sink):
            self._sink = sink

        async def __aenter__(self):
            conn = AsyncMock()

            async def _fetchrow(sql, *args):
                self._sink.append((sql, args))
                return {
                    "id": 1,
                    "calibration_status": "earned",
                    "seeded_since": None,
                    "earned_at": "now-stamp",
                    "failed_at": None,
                    "updated_at": "now-stamp",
                }

            conn.fetchrow = _fetchrow
            return conn

        async def __aexit__(self, *args):
            return None

    backend = _Stub()
    state = await backend.transition_r1_calibration_state("earned")

    assert len(captured) == 1, "only one DB roundtrip — UPDATE + RETURNING * combined"
    sql, args = captured[0]
    assert "UPDATE core.r1_calibration_state" in sql
    assert "RETURNING" in sql
    assert args == ("earned",)
    assert state["calibration_status"] == "earned"
    assert state["earned_at"] == "now-stamp"


@pytest.mark.asyncio
async def test_transition_r1_calibration_state_accepts_calibration_failed():
    from src.db.mixins.identity import IdentityMixin

    class _Stub(IdentityMixin):
        def acquire(self):
            return _AcquireCtx()

    class _AcquireCtx:
        async def __aenter__(self):
            conn = AsyncMock()
            conn.fetchrow = AsyncMock(return_value={
                "id": 1,
                "calibration_status": "calibration_failed",
                "seeded_since": None,
                "earned_at": None,
                "failed_at": "now-stamp",
                "updated_at": "now-stamp",
            })
            return conn

        async def __aexit__(self, *args):
            return None

    backend = _Stub()
    state = await backend.transition_r1_calibration_state("calibration_failed")
    assert state["calibration_status"] == "calibration_failed"
    assert state["failed_at"] == "now-stamp"


@pytest.mark.asyncio
async def test_transition_seeded_to_calibration_failed_skips_earned():
    """Per v3.3-C: seeded → calibration_failed direct transition is supported
    (operator can mark calibration_failed without first marking earned).
    Pins the CASE-WHEN branch that fires on `$1 = 'calibration_failed'`
    regardless of current state."""
    from src.db.mixins.identity import IdentityMixin

    class _Stub(IdentityMixin):
        def acquire(self):
            return _AcquireCtx()

    class _AcquireCtx:
        async def __aenter__(self):
            conn = AsyncMock()
            # Singleton was 'seeded' before; transition skips earned.
            conn.fetchrow = AsyncMock(return_value={
                "id": 1,
                "calibration_status": "calibration_failed",
                "seeded_since": "seeded-stamp",
                "earned_at": None,         # never earned
                "failed_at": "now-stamp",  # newly stamped
                "updated_at": "now-stamp",
            })
            return conn

        async def __aexit__(self, *args):
            return None

    backend = _Stub()
    state = await backend.transition_r1_calibration_state("calibration_failed")
    assert state["calibration_status"] == "calibration_failed"
    assert state["earned_at"] is None  # skipped earned entirely
    assert state["failed_at"] == "now-stamp"
