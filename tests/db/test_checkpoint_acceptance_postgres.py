"""Real PostgreSQL acceptance, ordering, retry, and isolated execution claims.

Every writer uses a distinct connection. Fixtures delete only their own rows;
schema bootstrap must be coordinated with other users of governance_test.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import uuid

import pytest
import pytest_asyncio

from src.checkpoints import CheckpointConflict, CheckpointProvenance
from src.db.mixins.checkpoint import CheckpointMixin
from src.db.mixins.identity import IdentityMixin, CheckpointIdentityRefused
from tests.test_db_utils import TEST_DB_URL, can_connect_to_test_db, ensure_test_database_schema

asyncpg = pytest.importorskip("asyncpg")
pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(
    not can_connect_to_test_db(), reason="governance_test database not available",
)]


class LoseCommitAck:
    def __init__(self, transaction):
        self.transaction = transaction

    async def __aenter__(self):
        return await self.transaction.__aenter__()

    async def __aexit__(self, exc_type, exc, tb):
        result = await self.transaction.__aexit__(exc_type, exc, tb)
        if exc_type is None:
            raise ConnectionError("commit acknowledgement lost")
        return result


class ConnectionProxy:
    def __init__(self, conn, backend):
        self.conn, self.backend = conn, backend

    def transaction(self):
        tx = self.conn.transaction()
        return LoseCommitAck(tx) if self.backend.lose_ack else tx

    async def execute(self, sql, *args):
        if self.backend.fail_outbox and "INSERT INTO core.checkpoint_outbox" in sql:
            raise RuntimeError("injected outbox failure")
        result = await self.conn.execute(sql, *args)
        if self.backend.stream_inserted and "INSERT INTO core.checkpoint_streams" in sql:
            self.backend.stream_inserted.set()
            await self.backend.release_stream.wait()
        return result

    async def fetchrow(self, sql, *args):
        if self.backend.timestamp and "INSERT INTO core.checkpoints " in sql:
            # Inject a database-recorded instant, not the authoritative order.
            sql = sql.replace("report_excerpt, provenance)", "report_excerpt, provenance, recorded_at)")
            sql = sql.replace("$8, $9::jsonb)", "$8, $9::jsonb, $10)")
            args = (*args, self.backend.timestamp)
        return await self.conn.fetchrow(sql, *args)

    def __getattr__(self, name):
        return getattr(self.conn, name)


class Backend(CheckpointMixin, IdentityMixin):
    def __init__(self, *, lose_ack=False, fail_outbox=False, timestamp=None,
                 stream_inserted=None, release_stream=None):
        self.lose_ack, self.fail_outbox, self.timestamp = lose_ack, fail_outbox, timestamp
        self.pids = []
        self.stream_inserted, self.release_stream = stream_inserted, release_stream

    @asynccontextmanager
    async def acquire(self):
        conn = await asyncpg.connect(TEST_DB_URL)
        self.pids.append(await conn.fetchval("SELECT pg_backend_pid()"))
        try:
            yield ConnectionProxy(conn, self)
        finally:
            await conn.close()


@pytest_asyncio.fixture
async def identities():
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    values = []
    try:
        assert await conn.fetchval("SELECT current_database()") == "governance_test"
        for _ in range(2):
            agent = f"test-checkpoint-{uuid.uuid4()}"
            await conn.execute("INSERT INTO core.agents(id, api_key) VALUES ($1, 'test-key')", agent)
            identity = await conn.fetchval(
                "INSERT INTO core.identities(agent_id, api_key_hash) VALUES ($1, 'test-hash') RETURNING identity_id", agent,
            )
            values.append((agent, identity))
        yield values
    finally:
        ids, agents = [item[1] for item in values], [item[0] for item in values]
        await conn.execute(
            "DELETE FROM assessment.checkpoint_deliveries WHERE checkpoint_id IN "
            "(SELECT checkpoint_id FROM core.checkpoints WHERE identity_id = ANY($1::bigint[]))", ids,
        )
        await conn.execute("DELETE FROM core.checkpoint_outbox WHERE identity_id = ANY($1::bigint[])", ids)
        await conn.execute(
            "UPDATE core.checkpoint_streams SET last_seq = 0, last_checkpoint_id = NULL WHERE identity_id = ANY($1::bigint[])", ids,
        )
        await conn.execute("DELETE FROM core.checkpoints WHERE identity_id = ANY($1::bigint[])", ids)
        await conn.execute("DELETE FROM core.checkpoint_streams WHERE identity_id = ANY($1::bigint[])", ids)
        await conn.execute("DELETE FROM core.identities WHERE identity_id = ANY($1::bigint[])", ids)
        await conn.execute("DELETE FROM core.agents WHERE id = ANY($1::text[])", agents)
        await conn.close()


async def accept(backend, identity, *, key="key", payload=b'{"report":"unchanged"}', attempt=None):
    return await backend.accept_checkpoint(
        agent_id=identity[0], identity_id=identity[1], idempotency_key=key,
        request_payload=payload, report_excerpt="Done. password=private-value",
        provenance=CheckpointProvenance(identity_tier="strong", proof_origin="caller_asserted"),
        attempt_id=attempt or str(uuid.uuid4()),
    )


async def counts(identity_id):
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        return dict(await conn.fetchrow(
            "SELECT (SELECT count(*) FROM core.checkpoints WHERE identity_id=$1) checkpoints, "
            "(SELECT count(*) FROM core.checkpoint_outbox WHERE identity_id=$1) events, "
            "(SELECT last_seq FROM core.checkpoint_streams WHERE identity_id=$1) last_seq", identity_id,
        ))
    finally:
        await conn.close()


async def test_zero_assessors_accepts_and_retains_only_approved_material(identities):
    receipt = await accept(Backend(), identities[0], key="private-caller-key", payload=b"private complete report")
    assert receipt.seq == 1 and receipt.prev_checkpoint_id is None and not receipt.replayed
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        row = await conn.fetchrow("SELECT * FROM core.checkpoints WHERE checkpoint_id=$1", uuid.UUID(receipt.checkpoint_id))
        assert row["report_excerpt"] == "Done. password=[REDACTED]"
        stored = json.dumps(dict(row), default=str)
        for forbidden in ("private complete report", "private-caller-key", "private-value"):
            assert forbidden not in stored
        assert await conn.fetchval(
            "SELECT count(*) FROM assessment.checkpoint_deliveries WHERE checkpoint_id=$1", uuid.UUID(receipt.checkpoint_id),
        ) == 0
        event = await conn.fetchrow("SELECT * FROM core.checkpoint_outbox WHERE event_id=$1", uuid.UUID(receipt.checkpoint_id))
        assert event["recorded_at"] == receipt.recorded_at and event["event_schema"] == "checkpoint.recorded.v1"
    finally:
        await conn.close()


async def test_concurrent_identical_requests_get_one_record_and_original_attempt(identities):
    first, second = Backend(), Backend()
    results = await asyncio.gather(accept(first, identities[0]), accept(second, identities[0]))
    assert first.pids != second.pids
    assert {r.replayed for r in results} == {False, True}
    assert len({r.checkpoint_id for r in results}) == len({r.accepted_attempt_id for r in results}) == 1
    assert len({r.recorded_at for r in results}) == 1
    assert await counts(identities[0][1]) == {"checkpoints": 1, "events": 1, "last_seq": 1}


async def test_concurrent_conflict_has_one_winner_and_consumes_no_extra_sequence(identities):
    results = await asyncio.gather(
        accept(Backend(), identities[0], payload=b"one"),
        accept(Backend(), identities[0], payload=b"two"), return_exceptions=True,
    )
    errors = [r for r in results if isinstance(r, CheckpointConflict)]
    winner = next(r for r in results if not isinstance(r, Exception))
    assert len(errors) == 1 and errors[0].canonical_checkpoint_id == winner.checkpoint_id
    assert await counts(identities[0][1]) == {"checkpoints": 1, "events": 1, "last_seq": 1}


async def test_concurrent_distinct_requests_chain_without_gaps(identities):
    results = await asyncio.gather(*(accept(Backend(), identities[0], key=f"key-{i}") for i in range(8)))
    ordered = sorted(results, key=lambda row: row.seq)
    assert [r.seq for r in ordered] == list(range(1, 9))
    assert ordered[0].prev_checkpoint_id is None
    for before, after in zip(ordered, ordered[1:]):
        assert after.prev_checkpoint_id == before.checkpoint_id


async def test_outbox_failure_rolls_back_checkpoint_stream_and_sequence(identities):
    with pytest.raises(RuntimeError, match="injected outbox"):
        await accept(Backend(fail_outbox=True), identities[0])
    assert await counts(identities[0][1]) == {"checkpoints": 0, "events": 0, "last_seq": None}
    retry = await accept(Backend(), identities[0])
    assert retry.seq == 1 and not retry.replayed


async def test_commit_ack_loss_replays_after_new_connection_and_new_backend(identities):
    original_attempt = str(uuid.uuid4())
    with pytest.raises(ConnectionError, match="acknowledgement lost"):
        await accept(Backend(lose_ack=True), identities[0], attempt=original_attempt)
    assert await counts(identities[0][1]) == {"checkpoints": 1, "events": 1, "last_seq": 1}
    replay = await accept(Backend(), identities[0])
    assert replay.replayed and replay.accepted_attempt_id == original_attempt
    lookup = await Backend().lookup_checkpoint(
        agent_id=identities[0][0], identity_id=identities[0][1], idempotency_key="key", request_payload=b'{"report":"unchanged"}',
    )
    assert lookup == replay


async def test_no_key_deliberately_creates_distinct_occurrences(identities):
    first, second = await accept(Backend(), identities[0], key=None), await accept(Backend(), identities[0], key=None)
    assert first.checkpoint_id != second.checkpoint_id
    assert not first.retry_safe and not second.retry_safe and second.seq == 2


async def test_identity_scope_and_predecessor_foreign_key(identities):
    first = await accept(Backend(), identities[0])
    second = await accept(Backend(), identities[1])
    assert first.seq == second.seq == 1 and first.checkpoint_id != second.checkpoint_id
    with pytest.raises(CheckpointIdentityRefused, match="identity_scope_mismatch"):
        await accept(Backend(), (identities[0][0], identities[1][1]))
    with pytest.raises(CheckpointIdentityRefused, match="identity_scope_mismatch"):
        await Backend().lookup_checkpoint(
            agent_id=identities[0][0], identity_id=identities[1][1], idempotency_key="key", request_payload=b'{"report":"unchanged"}',
        )
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(
                "INSERT INTO core.checkpoints(checkpoint_id,identity_id,seq,prev_checkpoint_id,idempotency_key_digest,"
                "request_digest,accepted_attempt_id,provenance) VALUES($1,$2,2,$3,$4,$4,$5,'{}')",
                uuid.uuid4(), identities[1][1], uuid.UUID(first.checkpoint_id), "a" * 64, uuid.uuid4(),
            )
    finally:
        await conn.close()


async def test_records_and_outbox_reject_mutation(identities):
    receipt = await accept(Backend(), identities[0])
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        for table, id_column, assignment in (
            ("core.checkpoints", "checkpoint_id", "report_excerpt='changed'"),
            ("core.checkpoint_outbox", "event_id", "recorded_at=now()"),
        ):
            with pytest.raises(asyncpg.RaiseError, match="immutable"):
                await conn.execute(f"UPDATE {table} SET {assignment} WHERE {id_column}=$1", uuid.UUID(receipt.checkpoint_id))
    finally:
        await conn.close()


async def test_sequence_wins_when_database_clock_ties_or_rolls_back(identities):
    now = datetime(2026, 11, 2, tzinfo=timezone.utc)
    earlier = datetime(2026, 11, 1, tzinfo=timezone.utc)
    first = await accept(Backend(timestamp=now), identities[0], key="1")
    second = await accept(Backend(timestamp=now), identities[0], key="2")
    third = await accept(Backend(timestamp=earlier), identities[0], key="3")
    assert first.recorded_at == second.recorded_at > third.recorded_at
    assert [first.seq, second.seq, third.seq] == [1, 2, 3]
    assert third.prev_checkpoint_id == second.checkpoint_id


async def test_durable_hold_refuses_new_acceptance_but_storage_replay_is_read(identities):
    receipt = await accept(Backend(), identities[0])
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        async with conn.transaction():
            await conn.execute("UPDATE core.agents SET status='paused' WHERE id=$1", identities[0][0])
            await conn.execute("UPDATE core.identities SET status='paused' WHERE identity_id=$1", identities[0][1])
        with pytest.raises(CheckpointIdentityRefused, match="identity_not_active"):
            await accept(Backend(), identities[0], key="new")
        replay = await accept(Backend(), identities[0])
        assert replay.replayed and replay.checkpoint_id == receipt.checkpoint_id
    finally:
        await conn.close()


async def test_lifecycle_lock_prevents_stale_active_acceptance(identities):
    # Establish head before the pause lock so this exercises the active-status
    # lock, rather than a new stream's FK lock.
    await accept(Backend(), identities[0])
    conn = await asyncpg.connect(TEST_DB_URL)
    tx = conn.transaction()
    await tx.start()
    try:
        await conn.execute("UPDATE core.agents SET status='paused' WHERE id=$1", identities[0][0])
        await conn.execute("UPDATE core.identities SET status='paused' WHERE identity_id=$1", identities[0][1])
        pending = asyncio.create_task(accept(Backend(), identities[0], key="after-pause"))
        await asyncio.sleep(0.1)
        assert not pending.done()
        await tx.commit()
        with pytest.raises(CheckpointIdentityRefused, match="identity_not_active"):
            await pending
        assert await counts(identities[0][1]) == {"checkpoints": 1, "events": 1, "last_seq": 1}
    finally:
        if conn.is_in_transaction():
            await tx.rollback()
        await conn.close()


async def test_first_stream_and_pause_use_the_same_lifecycle_lock_order(identities):
    inserted, release, pause_has_agent = asyncio.Event(), asyncio.Event(), asyncio.Event()
    writer = Backend(stream_inserted=inserted, release_stream=release)
    append_task = asyncio.create_task(accept(writer, identities[0]))
    pause_conn = await asyncpg.connect(TEST_DB_URL)

    async def pause():
        async with pause_conn.transaction():
            await pause_conn.fetchval("SELECT id FROM core.agents WHERE id=$1 FOR UPDATE", identities[0][0])
            pause_has_agent.set()
            await pause_conn.execute("UPDATE core.agents SET status='paused' WHERE id=$1", identities[0][0])
            await pause_conn.execute("UPDATE core.identities SET status='paused' WHERE identity_id=$1", identities[0][1])

    pause_task = None
    try:
        await asyncio.wait_for(inserted.wait(), timeout=3)
        pause_task = asyncio.create_task(pause())
        await asyncio.sleep(0.1)
        # New-stream FK validation must not precede lifecycle row locking:
        # otherwise pause can own agents while the append owns identities.
        assert not pause_has_agent.is_set()
        release.set()
        receipt, _ = await asyncio.wait_for(asyncio.gather(append_task, pause_task), timeout=3)
        assert receipt.seq == 1 and pause_has_agent.is_set()
    finally:
        release.set()
        for task in (append_task, pause_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (append_task, pause_task) if task is not None), return_exceptions=True)
        await pause_conn.close()


async def test_concurrent_assessment_claims_have_one_owner_and_terminal_retries_are_safe(identities):
    receipt = await accept(Backend(), identities[0])
    claims = await asyncio.gather(*(Backend().claim_checkpoint_assessment(
        checkpoint_id=receipt.checkpoint_id, assessor="fixture-assessor.v1",
    ) for _ in range(3)))
    assert sum(claim.claimed for claim in claims) == 1
    owner = next(claim for claim in claims if claim.claimed)
    assert owner.claim_token is not None
    assert all(claim.claim_token is None for claim in claims if not claim.claimed)
    args = dict(checkpoint_id=receipt.checkpoint_id, assessor=owner.assessor, claim_token=owner.claim_token)
    assert not await Backend().finish_checkpoint_assessment(**{**args, "claim_token": str(uuid.uuid4())}, status="completed")
    assert await Backend().finish_checkpoint_assessment(**args, status="completed")
    assert await Backend().finish_checkpoint_assessment(**args, status="completed")
    assert not await Backend().finish_checkpoint_assessment(**args, status="failed")
    replay = await Backend().claim_checkpoint_assessment(checkpoint_id=receipt.checkpoint_id, assessor=owner.assessor)
    assert not replay.claimed and replay.status == "completed"


@pytest.mark.parametrize("terminal", [None, "failed"])
async def test_ambiguous_or_failed_assessment_is_never_automatically_reclaimed(identities, terminal):
    receipt = await accept(Backend(), identities[0])
    if terminal:
        owner = await Backend().claim_checkpoint_assessment(checkpoint_id=receipt.checkpoint_id, assessor="fixture")
        assert await Backend().finish_checkpoint_assessment(
            checkpoint_id=receipt.checkpoint_id, assessor="fixture", claim_token=owner.claim_token, status=terminal,
        )
    else:
        with pytest.raises(ConnectionError):
            await Backend(lose_ack=True).claim_checkpoint_assessment(checkpoint_id=receipt.checkpoint_id, assessor="fixture")
    replay = await Backend().claim_checkpoint_assessment(checkpoint_id=receipt.checkpoint_id, assessor="fixture")
    assert replay.status == (terminal or "started") and not replay.claimed
    assert replay.claim_token is None
