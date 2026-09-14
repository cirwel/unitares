"""Real-PostgreSQL transaction tests for prediction-bound outcomes.

Each case uses unique keys and deletes only its own rows. Every competing write
gets a separate connection so PostgreSQL, not an in-process lock, arbitrates the
binding claim.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio

from src.db.mixins.tool_usage import ToolUsageMixin
from src.monitor_prediction import register_tactical_prediction
from tests.test_db_utils import (
    TEST_DB_URL,
    can_connect_to_test_db,
    ensure_test_database_schema,
)

asyncpg = pytest.importorskip("asyncpg")
pytestmark = pytest.mark.skipif(
    not can_connect_to_test_db(),
    reason="governance_test database not available",
)


class _CommitThenLoseAcknowledgement:
    def __init__(self, transaction) -> None:
        self._transaction = transaction

    async def __aenter__(self):
        return await self._transaction.__aenter__()

    async def __aexit__(self, exc_type, exc, tb):
        result = await self._transaction.__aexit__(exc_type, exc, tb)
        if exc_type is None:
            raise ConnectionError("commit acknowledgement lost")
        return result


class _ConnectionProxy:
    def __init__(self, connection, *, lose_commit_ack: bool) -> None:
        self._connection = connection
        self._lose_commit_ack = lose_commit_ack

    def transaction(self):
        transaction = self._connection.transaction()
        if self._lose_commit_ack:
            return _CommitThenLoseAcknowledgement(transaction)
        return transaction

    def __getattr__(self, name):
        return getattr(self._connection, name)


class SeparateConnectionBackend(ToolUsageMixin):
    def __init__(self, *, lose_commit_ack: bool = False) -> None:
        self.lose_commit_ack = lose_commit_ack
        self.backend_pids: list[int] = []

    @asynccontextmanager
    async def acquire(self):
        connection = await asyncpg.connect(TEST_DB_URL)
        self.backend_pids.append(await connection.fetchval("SELECT pg_backend_pid()"))
        try:
            yield _ConnectionProxy(
                connection,
                lose_commit_ack=self.lose_commit_ack,
            )
        finally:
            await connection.close()

    async def get_latest_eisv_by_agent_id(self, agent_id):
        return {
            "E": 0.71,
            "I": 0.78,
            "S": 0.16,
            "V": -0.02,
            "phi": 0.12,
            "verdict": "safe",
            "coherence": 0.51,
            "regime": "CONVERGENCE",
        }

    async def get_latest_confidence_before(self, agent_id):
        return None


@pytest_asyncio.fixture
async def isolated_binding_agent():
    await ensure_test_database_schema()
    agent_id = f"pg-binding-{uuid.uuid4()}"
    yield agent_id
    connection = await asyncpg.connect(TEST_DB_URL)
    try:
        await connection.execute(
            "DELETE FROM audit.outcome_prediction_bindings WHERE agent_id = $1",
            agent_id,
        )
        await connection.execute(
            "DELETE FROM audit.outcome_events WHERE agent_id = $1",
            agent_id,
        )
    finally:
        await connection.close()


def _record(backend, agent_id, prediction_id, *, digest="a" * 64, outcome_type="test_passed"):
    is_bad = outcome_type == "test_failed"
    return backend.record_bound_outcome_event(
        agent_id=agent_id,
        prediction_id=prediction_id,
        request_digest=digest,
        outcome_type=outcome_type,
        is_bad=is_bad,
        outcome_score=0.0 if is_bad else 1.0,
        detail={
            "prediction_id": prediction_id,
            "prediction_binding": "registry",
            "prediction_source": "registry",
            "reported_confidence": 0.83,
        },
        eisv_snapshot={"primary_eisv": {"E": 0.71}},
        verification_source="external_signal",
    )


async def _counts(agent_id, prediction_id):
    connection = await asyncpg.connect(TEST_DB_URL)
    try:
        return await connection.fetchrow(
            """
            SELECT
                (SELECT count(*) FROM audit.outcome_prediction_bindings
                 WHERE agent_id = $1 AND prediction_id = $2) AS bindings,
                (SELECT count(*) FROM audit.outcome_events
                 WHERE agent_id = $1 AND detail->>'prediction_id' = $2) AS outcomes
            """,
            agent_id,
            prediction_id,
        )
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_concurrent_identical_submissions_replay_one_canonical_outcome(
    isolated_binding_agent,
):
    prediction_id = str(uuid.uuid4())
    first = SeparateConnectionBackend()
    second = SeparateConnectionBackend()

    results = await asyncio.gather(
        _record(first, isolated_binding_agent, prediction_id),
        _record(second, isolated_binding_agent, prediction_id),
    )

    assert {result["status"] for result in results} == {"created", "existing"}
    assert len({result["outcome_id"] for result in results}) == 1
    assert first.backend_pids[0] != second.backend_pids[0]
    counts = await _counts(isolated_binding_agent, prediction_id)
    assert dict(counts) == {"bindings": 1, "outcomes": 1}


@pytest.mark.asyncio
async def test_concurrent_conflicting_submissions_choose_one_canonical_outcome(
    isolated_binding_agent,
):
    prediction_id = str(uuid.uuid4())
    first = SeparateConnectionBackend()
    second = SeparateConnectionBackend()

    results = await asyncio.gather(
        _record(first, isolated_binding_agent, prediction_id),
        _record(
            second,
            isolated_binding_agent,
            prediction_id,
            digest="b" * 64,
            outcome_type="test_failed",
        ),
    )

    assert {result["status"] for result in results} == {"created", "conflict"}
    assert len({result["outcome_id"] for result in results}) == 1
    assert first.backend_pids[0] != second.backend_pids[0]
    counts = await _counts(isolated_binding_agent, prediction_id)
    assert dict(counts) == {"bindings": 1, "outcomes": 1}


@pytest.mark.asyncio
async def test_outcome_failure_rolls_back_claim_on_real_postgres(isolated_binding_agent):
    prediction_id = str(uuid.uuid4())
    backend = SeparateConnectionBackend()

    failed = await backend.record_bound_outcome_event(
        agent_id=isolated_binding_agent,
        prediction_id=prediction_id,
        request_digest="c" * 64,
        outcome_type="test_passed",
        is_bad=False,
        outcome_score=1.0,
        detail={"prediction_id": prediction_id},
        eisv_snapshot=None,
        verification_source="not_a_valid_verification_source",
    )

    assert failed["status"] == "error"
    assert dict(await _counts(isolated_binding_agent, prediction_id)) == {
        "bindings": 0,
        "outcomes": 0,
    }
    retry = await _record(backend, isolated_binding_agent, prediction_id, digest="c" * 64)
    assert retry["status"] == "created"


@pytest.mark.asyncio
async def test_lost_commit_ack_replays_canonical_through_new_connection(
    isolated_binding_agent,
):
    prediction_id = str(uuid.uuid4())
    ambiguous = SeparateConnectionBackend(lose_commit_ack=True)
    first = await _record(ambiguous, isolated_binding_agent, prediction_id)

    assert first["status"] == "error"
    connection = await asyncpg.connect(TEST_DB_URL)
    try:
        canonical_id = await connection.fetchval(
            """
            SELECT canonical_outcome_id
            FROM audit.outcome_prediction_bindings
            WHERE agent_id = $1 AND prediction_id = $2
            """,
            isolated_binding_agent,
            prediction_id,
        )
    finally:
        await connection.close()

    replay = await _record(
        SeparateConnectionBackend(),
        isolated_binding_agent,
        prediction_id,
    )
    assert replay["status"] == "existing"
    assert replay["outcome_id"] == str(canonical_id)
    assert replay["detail"]["reported_confidence"] == 0.83
    assert dict(await _counts(isolated_binding_agent, prediction_id)) == {
        "bindings": 1,
        "outcomes": 1,
    }


@pytest.mark.asyncio
async def test_ambiguous_commit_currently_delivers_calibration_zero_times(
    isolated_binding_agent,
):
    from src.mcp_handlers.observability.outcome_events import _record_outcome_event_inline

    prediction_id = str(uuid.uuid4())
    monitor = MagicMock()
    monitor._open_predictions = {}
    monitor._prediction_ttl_seconds = 3600.0
    monitor._prev_confidence = None
    monitor._behavioral_state = None
    monitor.get_primary_eisv.return_value = (0.71, 0.78, 0.16, -0.02)
    register_tactical_prediction(
        monitor._open_predictions,
        confidence=0.83,
        decision_action="proceed",
    )
    monitor._open_predictions[prediction_id] = monitor._open_predictions.pop(
        next(iter(monitor._open_predictions))
    )
    server = MagicMock()
    server.monitors = {isolated_binding_agent: monitor}
    checker = MagicMock()
    sequential = MagicMock()
    snapshot = {"primary_eisv": {"E": 0.71}, "eisv_labels": {}}
    arguments = {
        "agent_id": isolated_binding_agent,
        "outcome_type": "test_passed",
        "prediction_id": prediction_id,
        "verification_source": "external_signal",
        "detail": {"kind": "test", "tool": "pytest", "exit_code": 0},
    }

    with (
        patch("src.db.get_db", return_value=SeparateConnectionBackend(lose_commit_ack=True)),
        patch("src.mcp_handlers.observability.outcome_events.mcp_server", server),
        patch("src.calibration.calibration_checker", checker),
        patch("src.sequential_calibration.sequential_calibration_tracker", sequential),
        patch(
            "src.services.runtime_queries._build_eisv_semantics",
            return_value=snapshot,
        ),
        patch(
            "src.mcp_handlers.context.get_context_client_session_id",
            return_value=None,
        ),
    ):
        first = await _record_outcome_event_inline(arguments)

    with (
        patch("src.db.get_db", return_value=SeparateConnectionBackend()),
        patch("src.mcp_handlers.observability.outcome_events.mcp_server", server),
        patch("src.calibration.calibration_checker", checker),
        patch("src.sequential_calibration.sequential_calibration_tracker", sequential),
        patch(
            "src.services.runtime_queries._build_eisv_semantics",
            return_value=snapshot,
        ),
        patch(
            "src.mcp_handlers.context.get_context_client_session_id",
            return_value=None,
        ),
    ):
        replay = await _record_outcome_event_inline(arguments)

    assert first["error_code"] == "DB_ERROR"
    assert replay["idempotent_replay"] is True
    checker.record_prediction.assert_not_called()
    checker.record_tactical_decision.assert_not_called()
    sequential.record_exogenous_tactical_outcome.assert_not_called()


@pytest.mark.asyncio
async def test_expired_binding_cleanup_allows_new_canonical_submission(
    isolated_binding_agent,
):
    prediction_id = str(uuid.uuid4())
    backend = SeparateConnectionBackend()
    first = await _record(backend, isolated_binding_agent, prediction_id)
    assert first["status"] == "created"

    connection = await asyncpg.connect(TEST_DB_URL)
    try:
        await connection.execute(
            """
            UPDATE audit.outcome_prediction_bindings
            SET canonical_outcome_ts = TIMESTAMPTZ '1000-01-01 00:00:00+00'
            WHERE agent_id = $1 AND prediction_id = $2
            """,
            isolated_binding_agent,
            prediction_id,
        )
        retained = await connection.fetchval(
            "SELECT audit.cleanup_outcome_prediction_bindings(100000)"
        )
        assert retained == 0
        await connection.execute(
            "DELETE FROM audit.outcome_events WHERE outcome_id = $1::uuid",
            uuid.UUID(first["outcome_id"]),
        )
        removed = await connection.fetchval(
            "SELECT audit.cleanup_outcome_prediction_bindings(100000)"
        )
    finally:
        await connection.close()

    assert removed == 1
    retry = await _record(backend, isolated_binding_agent, prediction_id)
    assert retry["status"] == "created"
    assert retry["outcome_id"] != first["outcome_id"]
    assert dict(await _counts(isolated_binding_agent, prediction_id)) == {
        "bindings": 1,
        "outcomes": 1,
    }
