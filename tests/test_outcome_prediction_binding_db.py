"""Transaction-level tests for the prediction binding ledger."""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager

import pytest

from src.db.mixins.tool_usage import ToolUsageMixin


class FakeTransaction:
    def __init__(self, events):
        self.events = events

    async def __aenter__(self):
        self.events.append("begin")
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.events.append("rollback" if exc_type else "commit")
        return False


class FakeConnection:
    def __init__(self, mode="created"):
        self.mode = mode
        self.events = []
        self.queries = []
        self.partition_failures = 1 if mode == "partition_retry" else 0
        self.canonical_id = uuid.uuid4()
        self.canonical_ts = "2026-09-14T12:00:00+00:00"
        self.snapshot = json.dumps({"primary_eisv": {"E": 0.7}})

    def transaction(self):
        return FakeTransaction(self.events)

    async def fetchrow(self, sql, *args):
        self.queries.append(sql)
        if "INSERT INTO audit.outcome_prediction_bindings" in sql:
            if self.mode == "conflict":
                return None
            attempt_token = args[3]
            return {
                "canonical_outcome_id": self.canonical_id,
                "canonical_outcome_ts": self.canonical_ts,
                "request_digest": args[2],
                "claim_token": (
                    uuid.uuid4() if self.mode == "existing" else attempt_token
                ),
                "canonical_eisv_snapshot": self.snapshot,
                "canonical_outcome_type": args[5],
                "canonical_outcome_score": args[6],
                "canonical_is_bad": args[7],
                "canonical_detail": args[8],
            }
        if "SELECT canonical_outcome_id, request_digest" in sql:
            return {
                "canonical_outcome_id": self.canonical_id,
                "request_digest": "different",
            }
        if "INSERT INTO audit.outcome_events" in sql:
            if self.mode == "failure":
                raise RuntimeError("disk unavailable")
            if self.partition_failures:
                self.partition_failures -= 1
                raise RuntimeError(
                    'no partition of relation "outcome_events" found for row'
                )
            return {
                "outcome_type": args[4],
                "outcome_score": args[5],
                "is_bad": args[6],
                "detail": args[15],
            }
        raise AssertionError(sql)

    async def fetchval(self, sql, *args):
        self.queries.append(sql)
        assert sql == "SELECT audit.partition_maintenance()"
        return None


class Backend(ToolUsageMixin):
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


async def record(backend):
    return await backend.record_bound_outcome_event(
        agent_id="agent-1",
        prediction_id="prediction-1",
        request_digest="a" * 64,
        outcome_type="test_passed",
        is_bad=False,
        outcome_score=1.0,
        detail={
            "prediction_binding": "registry",
            "prediction_source": "registry",
        },
        eisv_snapshot={"primary_eisv": {"E": 0.7}},
        verification_source="external_signal",
    )


@pytest.mark.asyncio
async def test_claim_and_outcome_insert_commit_in_one_transaction():
    conn = FakeConnection()
    result = await record(Backend(conn))

    assert result["status"] == "created"
    assert result["outcome_id"] == str(conn.canonical_id)
    assert conn.events == ["begin", "commit"]
    assert "outcome_prediction_bindings" in conn.queries[0]
    assert "INSERT INTO audit.outcome_events" in conn.queries[1]


@pytest.mark.asyncio
async def test_outcome_insert_failure_rolls_back_claim_transaction():
    conn = FakeConnection(mode="failure")
    result = await record(Backend(conn))

    assert result["status"] == "error"
    assert conn.events == ["begin", "rollback"]


@pytest.mark.asyncio
async def test_identical_existing_claim_reads_canonical_row_without_insert():
    conn = FakeConnection(mode="existing")
    result = await record(Backend(conn))

    assert result["status"] == "existing"
    assert result["eisv_snapshot"] == {"primary_eisv": {"E": 0.7}}
    assert conn.events == ["begin", "commit"]
    assert sum("INSERT INTO audit.outcome_events" in q for q in conn.queries) == 0
    assert sum("FROM audit.outcome_events" in q for q in conn.queries) == 0


@pytest.mark.asyncio
async def test_digest_conflict_returns_canonical_id_without_outcome_write():
    conn = FakeConnection(mode="conflict")
    result = await record(Backend(conn))

    assert result == {
        "status": "conflict",
        "outcome_id": str(conn.canonical_id),
    }
    assert conn.events == ["begin", "commit"]
    assert sum("INSERT INTO audit.outcome_events" in q for q in conn.queries) == 0


@pytest.mark.asyncio
async def test_missing_partition_rolls_back_then_retries_whole_atomic_unit():
    conn = FakeConnection(mode="partition_retry")
    result = await record(Backend(conn))

    assert result["status"] == "created"
    assert conn.events == ["begin", "rollback", "begin", "commit"]
    assert "SELECT audit.partition_maintenance()" in conn.queries
    assert (
        sum("INSERT INTO audit.outcome_prediction_bindings" in q for q in conn.queries)
        == 2
    )
    assert sum("INSERT INTO audit.outcome_events" in q for q in conn.queries) == 2
