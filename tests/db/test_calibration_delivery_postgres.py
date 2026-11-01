"""Real transactions with isolated projection tables, not a mocked commit."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from src import calibration_delivery as delivery
from src.calibration_mutations import CalibrationVersionConflict, make_mutation
from src.db.mixins.calibration import CalibrationMixin
from src.db.mixins.tool_usage import ToolUsageMixin
from src.outcome_corroboration import GRADE_WEIGHTS, TOOL_OBSERVED
from tests.test_db_utils import TEST_DB_URL, can_connect_to_test_db, ensure_test_database_schema

asyncpg = pytest.importorskip("asyncpg")
pytestmark = pytest.mark.skipif(not can_connect_to_test_db(), reason="governance_test unavailable")


class _Transaction:
    def __init__(self, transaction, lose_ack):
        self.transaction, self.lose_ack = transaction, lose_ack

    async def __aenter__(self):
        return await self.transaction.__aenter__()

    async def __aexit__(self, kind, error, traceback):
        result = await self.transaction.__aexit__(kind, error, traceback)
        if kind is None and self.lose_ack:
            raise ConnectionError("committed but acknowledgement lost")
        return result


class _Connection:
    def __init__(self, conn, backend):
        self.conn, self.backend = conn, backend

    def transaction(self):
        lose_ack, self.backend.lose_ack = self.backend.lose_ack, False
        return _Transaction(self.conn.transaction(), lose_ack)

    def __getattr__(self, method):
        async def call(sql, *args):
            if self.backend.fail_enqueue and "INSERT INTO audit.outcome_calibration_deliveries" in sql:
                raise OSError("outbox storage unavailable")
            return await getattr(self.conn, method)(
                sql.replace("core.calibration", self.backend.schema + ".calibration"), *args,
            )
        return call


class Backend(ToolUsageMixin, CalibrationMixin):
    lose_ack = False
    fail_enqueue = False

    def __init__(self, schema):
        self.schema = schema

    @asynccontextmanager
    async def acquire(self):
        conn = await asyncpg.connect(TEST_DB_URL)
        try:
            yield _Connection(conn, self)
        finally:
            await conn.close()


@pytest_asyncio.fixture
async def instrument(tmp_path, monkeypatch):
    await ensure_test_database_schema()
    schema = "delivery_test_" + uuid4().hex
    conn = await asyncpg.connect(TEST_DB_URL)
    await conn.execute(f"CREATE SCHEMA {schema}")
    await conn.execute(f"CREATE TABLE {schema}.calibration (LIKE core.calibration INCLUDING ALL)")
    await conn.execute(f"INSERT INTO {schema}.calibration (id, data, version) VALUES (TRUE, '{{}}', 1)")
    await conn.execute(
        f"CREATE TABLE {schema}.calibration_writer_receipts "
        "(LIKE core.calibration_writer_receipts INCLUDING ALL)"
    )
    target = {**delivery.delivery_target(), "host": schema,
              "calibration_file": str(tmp_path / "cal.json"),
              "sequential_file": str(tmp_path / "seq.json")}
    monkeypatch.setattr(delivery, "delivery_target", lambda: target)
    agent = "delivery-test-" + uuid4().hex
    try:
        yield Backend(schema), agent, target
    finally:
        await conn.execute("DELETE FROM audit.outcome_calibration_deliveries WHERE agent_id=$1", agent)
        await conn.execute("DELETE FROM audit.outcome_prediction_bindings WHERE agent_id=$1", agent)
        await conn.execute("DELETE FROM audit.outcome_events WHERE agent_id=$1", agent)
        await conn.execute(f"DROP SCHEMA {schema} CASCADE")
        await conn.close()


async def record(db, agent, prediction=None, *, eligible=True):
    return await db.record_bound_outcome_event(
        agent_id=agent, prediction_id=prediction or str(uuid4()), request_digest="a" * 64,
        outcome_type="test_passed", outcome_score=0.123456789, is_bad=False,
        detail={"reported_confidence": 0.83, "prediction_source": "registry",
                "evidence_weight": GRADE_WEIGHTS[TOOL_OBSERVED],
                "calibration_excluded": not eligible, "eprocess_eligible": True,
                "hard_exogenous_signal": "tests", "decision_action": "proceed"},
    )


async def projection(db, target):
    if target["backend"] == "json":
        return json.loads(Path(target["calibration_file"]).read_text())
    return await db.get_calibration()


def sequential(target):
    from src.sequential_calibration import SequentialCalibrationTracker
    return SequentialCalibrationTracker(target["sequential_file"]).compute_metrics()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["postgres", "json"])
async def test_lost_outcome_ack_restarts_and_delivers_once(instrument, backend):
    db, agent, target = instrument
    target["backend"] = backend
    prediction = str(uuid4())
    db.lose_ack = True
    assert (await record(db, agent, prediction))["status"] == "error"
    replay = await record(db, agent, prediction)
    assert replay["status"] == "existing"
    assert await delivery.drain_deliveries(db) == 1
    assert await delivery.drain_deliveries(Backend(db.schema)) == 0
    state = await projection(db, target)
    assert state["bins"]["0.8-0.9"]["count"] == 1
    assert state["bins"]["0.8-0.9"]["actual_correct"] == replay["outcome_score"]
    assert state["tactical_bins"]["0.8-0.9"]["count"] == 1
    assert state["bins_by_agent"][agent]["0.8-0.9"]["count"] == 1
    assert sequential(target)["eligible_samples"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["postgres", "json"])
async def test_file_commit_then_db_rollback_retries_without_double_count(instrument, backend, monkeypatch):
    db, agent, target = instrument
    target["backend"] = backend
    await record(db, agent)
    deliver = delivery._deliver_file_sinks

    def fail_after_file(*args):
        deliver(*args)
        raise OSError("crash before database receipt")

    monkeypatch.setattr(delivery, "_deliver_file_sinks", fail_after_file)
    assert await delivery.drain_deliveries(db, limit=1) == 0
    assert sequential(target)["eligible_samples"] == 1
    monkeypatch.setattr(delivery, "_deliver_file_sinks", deliver)
    async with db.acquire() as conn:
        await conn.execute("UPDATE audit.outcome_calibration_deliveries SET next_attempt_at=now() WHERE agent_id=$1", agent)
    assert await delivery.drain_deliveries(Backend(db.schema)) == 1
    assert (await projection(db, target))["bins"]["0.8-0.9"]["count"] == 1
    assert sequential(target)["eligible_samples"] == 1


@pytest.mark.asyncio
async def test_concurrent_workers_and_lost_delivery_ack(instrument):
    db, agent, target = instrument
    for _ in range(4):
        await record(db, agent)
    db.lose_ack = True
    await asyncio.gather(delivery.drain_deliveries(db), delivery.drain_deliveries(Backend(db.schema)))
    assert (await projection(db, target))["bins"]["0.8-0.9"]["count"] == 4
    assert sequential(target)["eligible_samples"] == 4
    assert await delivery.drain_deliveries(db) == 0


@pytest.mark.asyncio
async def test_wrong_destination_and_ineligible_outcomes_are_not_delivered(instrument):
    db, agent, target = instrument
    await record(db, agent, eligible=False)
    assert await delivery.drain_deliveries(db) == 0
    await record(db, agent)
    assert await delivery.drain_deliveries(db, target={**target, "epoch": -1}) == 0
    assert await delivery.drain_deliveries(db) == 1


@pytest.mark.asyncio
async def test_enqueue_failure_rolls_back_binding_and_outcome(instrument):
    db, agent, target = instrument
    db.fail_enqueue = True
    assert (await record(db, agent))["status"] == "error"
    async with db.acquire() as conn:
        for table in ("outcome_events", "outcome_prediction_bindings", "outcome_calibration_deliveries"):
            assert await conn.fetchval(f"SELECT count(*) FROM audit.{table} WHERE agent_id=$1", agent) == 0
    assert (await projection(db, target)).get("bins", {}) == {}
    assert sequential(target)["eligible_samples"] == 0


@pytest.mark.asyncio
async def test_pending_delivery_survives_canonical_retention(instrument):
    db, agent, target = instrument
    result = await record(db, agent)
    async with db.acquire() as conn:
        await conn.execute("DELETE FROM audit.outcome_events WHERE agent_id=$1", agent)
        await conn.execute("UPDATE audit.outcome_prediction_bindings SET canonical_outcome_ts=now()-interval '400 days' WHERE agent_id=$1", agent)
        await conn.execute("UPDATE audit.outcome_calibration_deliveries SET outcome_ts=now()-interval '400 days' WHERE agent_id=$1", agent)
        await conn.fetchval("SELECT audit.cleanup_outcome_prediction_bindings()")
        assert await conn.fetchval("SELECT count(*) FROM audit.outcome_prediction_bindings WHERE agent_id=$1", agent) == 1
    assert await delivery.drain_deliveries(db) == 1
    assert (await projection(db, target))["bins"]["0.8-0.9"]["count"] == 1
    async with db.acquire() as conn:
        await conn.fetchval("SELECT audit.cleanup_outcome_prediction_bindings()")
        assert await conn.fetchval("SELECT count(*) FROM audit.outcome_calibration_deliveries WHERE outcome_id=$1", UUID(result["outcome_id"])) == 0
    assert sequential(target)["eligible_samples"] == 1


@pytest.mark.asyncio
async def test_ordinary_writer_retry_and_concurrency_preserve_outcome_delivery(instrument):
    db, agent, target = instrument
    writer, mutation = str(uuid4()), make_mutation({}, {"bins": {"0.8-0.9": {
        "count": 1, "predicted_correct": 1, "actual_correct": 1, "confidence_sum": 0.83,
    }}})
    db.lose_ack = True
    with pytest.raises(ConnectionError):
        await db.apply_calibration_mutation(writer_id=writer, sequence=1, mutation=mutation)
    await record(db, agent)
    assert await delivery.drain_deliveries(db) == 1
    assert not (await db.apply_calibration_mutation(writer_id=writer, sequence=1, mutation=mutation))["applied"]
    await asyncio.gather(*[
        db.apply_calibration_mutation(writer_id=str(uuid4()), sequence=1, mutation=mutation)
        for _ in range(2)
    ])
    state = await projection(db, target)
    assert state["bins"]["0.8-0.9"]["count"] == 4
    assert not await db.update_calibration({"_version": 1, "bins": {}})
    with pytest.raises(CalibrationVersionConflict):
        await db.update_calibration({"bins": {}})


@pytest.mark.asyncio
async def test_recovery_refreshes_live_cache_even_when_delivery_ack_was_lost(instrument, monkeypatch, tmp_path):
    from src.calibration import CalibrationChecker
    import src.calibration
    import src.db

    db, agent, _ = instrument
    checker = CalibrationChecker(state_file=tmp_path / "cold-cache.json")
    checker._backend = "postgres"
    monkeypatch.setattr(src.calibration, "get_calibration_checker", lambda: checker)
    monkeypatch.setattr(src.db, "get_db", lambda: db)
    await record(db, agent)
    db.lose_ack = True

    async def stop_after_one_pass(_):
        raise asyncio.CancelledError

    monkeypatch.setattr(delivery.asyncio, "sleep", stop_after_one_pass)
    with pytest.raises(asyncio.CancelledError):
        await delivery.calibration_delivery_task()
    assert checker.bin_stats["0.8-0.9"]["count"] == 1
