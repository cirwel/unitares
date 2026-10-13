"""Delivery receipts must not consume unseen changes or leak another inbox."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from mcp.types import TextContent

from src import knowledge_notices as notices


def uid():
    return str(uuid4())


async def create(db, owner, tags=None):
    row = uid()
    async with db.acquire() as conn:
        await conn.execute("INSERT INTO knowledge.discoveries(id,agent_id,type,summary,tags,status) VALUES($1,$2,'note','first',$3,'open')", row, owner, tags or [])
    return row


async def update(db, row, summary="changed"):
    async with db.acquire() as conn:
        await conn.execute("UPDATE knowledge.discoveries SET summary=$2,updated_at=now() WHERE id=$1", row, summary)


@pytest.mark.asyncio
async def test_replay_ack_stale_revision_and_other_recipient(live_postgres_backend):
    db = live_postgres_backend
    owner, stranger = uid(), uid()
    row = await create(db, owner)
    assert not (await notices.read_notices(owner, db=db))["notices"]
    await update(db, row)
    first = await notices.read_notices(owner, db=db)
    assert first == await notices.read_notices(owner, db=db)  # reads do not claim
    revision = first["notices"][0]["revision"]
    assert not (await notices.read_notices(stranger, acknowledge=[revision], db=db))["notices"]
    await update(db, row, "newer")
    newer = await notices.read_notices(owner, acknowledge=[revision], db=db)
    assert newer["notices"][0]["revision"] != revision
    assert not (await notices.read_notices(owner, acknowledge=[newer["notices"][0]["revision"]], db=db))["notices"]


@pytest.mark.asyncio
async def test_two_readers_receive_same_unclaimed_revision(live_postgres_backend):
    db = live_postgres_backend
    owner = uid()
    await update(db, await create(db, owner))
    first, second = await asyncio.gather(notices.read_notices(owner, db=db), notices.read_notices(owner, db=db))
    assert first == second


@pytest.mark.asyncio
async def test_trigger_rollback_and_no_timestamp_only_noise(live_postgres_backend):
    db = live_postgres_backend
    owner = uid()
    row = await create(db, owner)
    async with db.acquire() as conn:
        with pytest.raises(RuntimeError):
            async with conn.transaction():
                await conn.execute("UPDATE knowledge.discoveries SET status='archived' WHERE id=$1", row)
                raise RuntimeError("crash before commit")
        await conn.execute("UPDATE knowledge.discoveries SET updated_at=now() WHERE id=$1", row)
    assert not (await notices.read_notices(owner, db=db))["notices"]


@pytest.mark.asyncio
async def test_mentions_are_exact_and_removal_revokes_pointer(live_postgres_backend):
    db = live_postgres_backend
    owner, recipient = uid(), uid()
    row = await create(db, owner, ["notify-agent:" + recipient, "notify-agent:display-name"])
    result = await notices.read_notices(recipient, db=db)
    assert result["notices"][0]["discovery_id"] == row
    assert not (await notices.read_notices("display-name", db=db))["notices"]
    async with db.acquire() as conn:
        await conn.execute("UPDATE knowledge.discoveries SET tags=ARRAY[]::text[] WHERE id=$1", row)
    assert not (await notices.read_notices(recipient, db=db))["notices"]
    assert (await notices.read_notices(owner, db=db))["notices"]
    assert not (await notices.read_notices(uid(), db=db))["notices"]  # no successor inheritance


@pytest.mark.asyncio
async def test_bound_limit_coalescing_and_mute(live_postgres_backend):
    db = live_postgres_backend
    owner = uid()
    for _ in range(5):
        row = await create(db, owner)
        for revision in range(3):
            await update(db, row, f"changed-{revision}")
    result = await notices.read_notices(owner, db=db)
    assert len(result["notices"]) == 3 and result["has_more"]
    assert len(json.dumps(result).encode()) <= notices.NOTICE_BYTES
    assert (await notices.read_notices(owner, muted=True, db=db))["notices"] == []
    assert len((await notices.read_notices(owner, muted=False, db=db))["notices"]) == 3


@pytest.mark.asyncio
async def test_expiry_and_overflow_report_stable_acknowledgeable_gap(live_postgres_backend):
    db = live_postgres_backend
    owner = uid()
    for _ in range(101):
        await update(db, await create(db, owner))
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM knowledge.change_notices WHERE recipient=$1", owner) == 100
        await conn.execute("UPDATE knowledge.change_notices SET changed_at=now()-interval '8 days' WHERE recipient=$1", owner)
    result = await notices.read_notices(owner, db=db)
    assert result["gap"] and not result["notices"]
    assert result == await notices.read_notices(owner, acknowledge_gap=uid(), db=db)
    assert "gap" not in await notices.read_notices(owner, acknowledge_gap=result["gap_token"], db=db)


@pytest.mark.parametrize("origin,agent,expected", [(None,uid(),None),("server_inferred",uid(),None),("caller_asserted",None,None),("caller_asserted","real","real")])
def test_identity_is_context_not_argument(monkeypatch, origin, agent, expected):
    monkeypatch.setattr("src.mcp_handlers.context.get_session_proof_origin", lambda: origin)
    monkeypatch.setattr("src.mcp_handlers.context.get_context_agent_id", lambda: agent)
    assert notices.proven_recipient() == expected


@pytest.mark.asyncio
async def test_spoofed_recipient_refused_without_proof(monkeypatch):
    monkeypatch.setattr(notices, "proven_recipient", lambda: None)
    read = AsyncMock()
    monkeypatch.setattr(notices, "read_notices", read)
    result = await notices.handle_knowledge_notices({"agent_id": uid(), "_agent_uuid": uid()})
    assert json.loads(result[0].text)["success"] is False
    read.assert_not_called()


@pytest.mark.asyncio
async def test_delivery_failure_keeps_success_and_retries(monkeypatch):
    monkeypatch.setattr(notices, "proven_recipient", lambda: "real")
    read = AsyncMock(side_effect=[RuntimeError("unavailable"), {"notices":[{"revision":uid()}],"muted":False,"has_more":False}])
    monkeypatch.setattr(notices, "read_notices", read)
    result = [TextContent(type="text", text='{"success":true}')]
    assert await notices.attach_notices("knowledge", {}, SimpleNamespace(), result) is result
    replay = await notices.attach_notices("knowledge", {}, SimpleNamespace(), result)
    assert json.loads(replay[0].text)["knowledge_changes"]["notices"]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{"success":False},{"success":True,"error":"failure"},{"success":True,"status":"identity_required","refused":True}])
async def test_no_notices_on_refusals(monkeypatch, payload):
    monkeypatch.setattr(notices,"proven_recipient",lambda:"real")
    read = AsyncMock()
    monkeypatch.setattr(notices,"read_notices",read)
    result=[TextContent(type="text",text=json.dumps(payload))]
    assert await notices.attach_notices("knowledge",{},SimpleNamespace(),result) is result
    read.assert_not_called()


@pytest.mark.parametrize("receipts", ["not-a-list",["bad"],[uid() for _ in range(4)]])
def test_bad_receipts_rejected(receipts):
    with pytest.raises(ValueError):
        notices._receipts(receipts)


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"success": True, "knowledge_changes": {"notices": []}},
    {"success": True, "notices": [], "ack_action": "knowledge(action='notices')"},
])
async def test_gateway_outer_dispatch_does_not_duplicate_delivery(monkeypatch, payload):
    monkeypatch.setattr(notices, "proven_recipient", lambda: "real")
    read = AsyncMock()
    monkeypatch.setattr(notices, "read_notices", read)
    result = [TextContent(type="text", text=json.dumps(payload))]
    assert await notices.attach_notices("use_tool", {}, SimpleNamespace(), result) is result
    read.assert_not_called()


@pytest.mark.asyncio
async def test_size_receipt_includes_added_notices(monkeypatch):
    monkeypatch.setattr(notices, "proven_recipient", lambda: "real")
    monkeypatch.setattr(notices, "read_notices", AsyncMock(return_value={
        "notices": [{"revision": uid()}], "muted": False, "has_more": False}))
    result = [TextContent(type="text", text=json.dumps({"success": True,
        "_response_size": {"approx_bytes": 10, "measured_without_self": True}}))]
    output = await notices.attach_notices("identity", {}, SimpleNamespace(), result)
    payload = json.loads(output[0].text)
    size = payload.pop("_response_size")
    assert size["approx_bytes"] == len(json.dumps(payload, ensure_ascii=False).encode())
