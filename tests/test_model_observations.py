"""Recorded model context must not become identity or current-model proof."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from mcp.types import TextContent

from src.model_harness_provenance import build_runtime_provenance_from_values
from src.services import model_observations as observations


def provenance(model="test-model-v2", source="harness_reported"):
    return build_runtime_provenance_from_values(
        model_identifier=model, model_source=source, model_exact=True,
        model_channel="host_hook_payload", harness_type="test-harness",
        harness_type_source="harness_reported",
    )


@pytest.fixture(autouse=True)
def clear_cache():
    observations._cache.clear()
    yield
    observations._cache.clear()


@pytest.fixture
def caller(monkeypatch):
    uid = str(uuid4())
    monkeypatch.setattr("src.mcp_handlers.context.get_session_proof_origin", lambda: "caller_asserted")
    monkeypatch.setattr("src.mcp_handlers.context.get_context_resolved_agent_id", lambda: uid)
    return uid


def row(raw):
    return dict(provenance=raw, observed_at=datetime.now(timezone.utc),
                record_id="record-1", record_kind="audit_event")


def database(rows):
    fetch = AsyncMock(return_value=rows)
    return SimpleNamespace(
        read_audit_model_observation_candidates=fetch,
        read_state_model_observation_candidates=AsyncMock(return_value=[]),
    ), fetch


@pytest.mark.asyncio
async def test_normalizes_without_promoting_family_or_caller_claim(caller):
    for source in ("caller_declared", "transport_inferred", "harness_reported", "provider_reported"):
        db, _ = database([row(provenance(source=source))])
        result = await observations.read_model_observation(caller, db=db)
        assert result["model"]["source"] == source
        assert result["model"]["exact"] == (source in {"harness_reported", "provider_reported"})
        assert result["agent_uuid"] == caller
        assert result["harness"]["type"] == "test-harness"


@pytest.mark.asyncio
async def test_skips_invalid_and_legacy_without_leaking_rejected_values(caller):
    invalid = provenance()
    invalid["model"]["identifier"] = "https://example.test/secret"
    future = {**provenance(), "schema": "future.v99"}
    db, _ = database([row(invalid), row(future), row({"model": "guessed-model"}), row(provenance())])
    result = await observations.read_model_observation(caller, db=db)
    assert result["model"]["identifier"] == "test-model-v2"
    assert "secret" not in json.dumps(result)


@pytest.mark.asyncio
async def test_durable_sources_order_models_not_delivery_or_missing_events(live_postgres_backend):
    """Real SQL covers UUID scoping, model switches, state and hook writers."""
    db = live_postgres_backend
    caller, other = str(uuid4()), str(uuid4())
    now = datetime.now(timezone.utc)

    async def event(agent, at, raw, kind="activity_rollup"):
        async with db.acquire() as conn:
            await conn.execute(
                "INSERT INTO audit.events(ts,event_id,event_type,agent_id,payload) VALUES($1,$2,$3,$4,$5::jsonb)",
                at, str(uuid4()), "runtime_observation." + kind, agent,
                json.dumps({"runtime_provenance": raw}),
            )

    await event(caller, now - timedelta(minutes=3), provenance("new-model"))
    # Delayed delivery of an older event must not overwrite the newer one.
    await event(caller, now - timedelta(minutes=5), provenance("old-model"))
    await event(other, now, provenance("other-agents-model"))
    await event(caller, now + timedelta(hours=1), provenance("future-model"))
    for _ in range(observations.CANDIDATE_LIMIT + 2):
        await event(caller, now, provenance(None), "context_compaction")
    result = await observations.read_model_observation(caller, db=db)
    assert result["model"]["identifier"] == "new-model"
    assert result["observed_at"] == (now - timedelta(minutes=3)).isoformat()
    assert result["record"]["kind"] == "audit_event"

    # Non-hook clients report through the persisted check-in envelope.
    async with db.acquire() as conn:
        await conn.execute("INSERT INTO core.agents(id,api_key) VALUES($1,'test')", caller)
        identity = await conn.fetchval(
            "INSERT INTO core.identities(agent_id,api_key_hash) VALUES($1,'test') RETURNING identity_id", caller
        )
        await conn.execute(
            "INSERT INTO core.agent_state(identity_id,recorded_at,state_json) VALUES($1,$2,$3::jsonb)",
            identity, now - timedelta(minutes=1),
            json.dumps({"provenance_context": {"runtime_provenance": provenance("declared-newer", "caller_declared")}}),
        )
        await conn.execute(
            "INSERT INTO core.agent_state(identity_id,recorded_at,state_json,synthetic) VALUES($1,$2,$3::jsonb,true)",
            identity, now,
            json.dumps({"provenance_context": {"runtime_provenance": provenance("synthetic-model")}}),
        )
    result = await observations.read_model_observation(caller, db=db)
    assert result["model"]["identifier"] == "declared-newer"
    assert result["model"]["exact"] is False
    assert result["record"]["kind"] == "agent_state"
    assert await observations.read_model_observation(str(uuid4()), db=db) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["canonical", "identity", "friendly"])
async def test_response_keeps_identity_and_subject_separate(caller, monkeypatch, shape):
    model = {"agent_uuid": caller, "model": provenance()["model"], "observed_at": "2026-10-20T00:00:00+00:00"}
    lookup = AsyncMock(return_value=model)
    monkeypatch.setattr(observations, "read_model_observation", lookup)
    payload = {"success": True, "agent_uuid": str(uuid4())}
    if shape == "canonical":
        payload["agent_signature"] = {"uuid": caller, "agent_id": "Cosmetic", "identity_assurance": {"caller_proven": True}}
    elif shape == "identity":
        payload["identity_context"] = {"harness_context": {"model": None}}
    else:
        payload["_response_size"] = {}
    original = deepcopy(payload)
    tail = TextContent(type="text", text="additional content")
    result = await observations.attach_model_observation([TextContent(type="text", text=json.dumps(payload)), tail])
    enriched = json.loads(result[0].text)
    assert enriched.pop("caller_model_observation") == model
    if shape == "friendly":
        size = enriched.pop("_response_size")
        original.pop("_response_size")
        measured = json.loads(result[0].text)
        measured.pop("_response_size")
        assert size["approx_bytes"] == len(json.dumps(measured, ensure_ascii=False).encode())
    assert enriched == original
    assert result[1] is tail
    lookup.assert_awaited_once_with(caller)
    # Nested use_tool dispatch preserves the already attributed record.
    assert await observations.attach_model_observation(result) is result


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, RuntimeError("database unavailable"), "timeout"])
async def test_missing_or_failed_optional_lookup_preserves_response(caller, monkeypatch, failure):
    async def lookup(_):
        if failure == "timeout":
            await asyncio.sleep(1)
        elif isinstance(failure, Exception):
            raise failure
        return None
    monkeypatch.setattr(observations, "read_model_observation", lookup)
    monkeypatch.setattr(observations, "LOOKUP_TIMEOUT_SECONDS", 0.01)
    raw = [TextContent(type="text", text='{"success":true}')]
    assert await observations.attach_model_observation(raw) is raw


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{"success": False}, {"error": "denied"}, {"status": "identity_required"}, {"refused": True}, []])
async def test_refusals_are_not_enriched(caller, monkeypatch, payload):
    lookup = AsyncMock()
    monkeypatch.setattr(observations, "read_model_observation", lookup)
    raw = [TextContent(type="text", text=json.dumps(payload))]
    assert await observations.attach_model_observation(raw) is raw
    lookup.assert_not_called()


@pytest.mark.asyncio
async def test_unproven_or_unresolved_context_does_not_read_models(caller, monkeypatch):
    lookup = AsyncMock()
    monkeypatch.setattr(observations, "read_model_observation", lookup)
    raw = [TextContent(type="text", text='{"success":true}')]
    monkeypatch.setattr("src.mcp_handlers.context.get_session_proof_origin", lambda: "server_inferred")
    assert await observations.attach_model_observation(raw) is raw
    monkeypatch.setattr("src.mcp_handlers.context.get_session_proof_origin", lambda: "caller_asserted")
    monkeypatch.setattr("src.mcp_handlers.context.get_context_resolved_agent_id", lambda: None)
    assert await observations.attach_model_observation(raw) is raw
    lookup.assert_not_called()


@pytest.mark.asyncio
async def test_cache_is_bounded_isolated_expires_and_does_not_mutate(caller, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(observations.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(observations, "CACHE_LIMIT", 2)
    lookup = AsyncMock(side_effect=lambda uid: {"agent_uuid": uid, "model": {"identifier": "original"}})
    monkeypatch.setattr(observations, "read_model_observation", lookup)
    first = await observations._cached_observation(caller)
    first["model"]["identifier"] = "mutated"
    assert (await observations._cached_observation(caller))["model"]["identifier"] == "original"
    assert lookup.await_count == 1
    clock[0] += observations.CACHE_SECONDS + 1
    await observations._cached_observation(caller)
    assert lookup.await_count == 2
    for _ in range(2):
        other = str(uuid4())
        assert (await observations._cached_observation(other))["agent_uuid"] == other
    assert len(observations._cache) == 2
    assert caller not in observations._cache


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["identity", "get_governance_metrics"])
async def test_existing_post_execution_seam_delivers_observation(caller, monkeypatch, name):
    from src.mcp_handlers.middleware.envelope_step import apply_experience_envelope
    from src.mcp_handlers.middleware import DispatchContext
    monkeypatch.setattr(observations, "read_model_observation", AsyncMock(return_value={"agent_uuid": caller, "model": provenance()["model"]}))
    monkeypatch.setattr("src.knowledge_notices.attach_notices", AsyncMock(side_effect=lambda n, a, c, r: r))
    raw = [TextContent(type="text", text='{"success":true}')]
    result = await apply_experience_envelope(name, {}, DispatchContext(original_name=name), raw)
    assert json.loads(result[0].text)["caller_model_observation"]["model"]["identifier"] == "test-model-v2"
