"""A label the server assigned at mint reads as label_source "auto".

agent_signature reported ``label_source: "claimed"`` for the label the mint
assigns (``claude_code-opus_1856bb5c``, logged as [AUTO_NAME]): the old rule
called any label claimed unless it equalled the public_agent_id or the
structured_id, and the auto label equals neither.

The label's shape cannot tell the two apart. A claimed name that collides with
another agent's label is renamed ``{name}_{uuid8}``, the same form, and on
2026-09-26 more than a thousand live labels had that form because of a claim
(``memory-kg-sync_<uuid8>``, ``canary_dialectic_<uuid8>``, ...). So the mint
now records the label it chose (``auto_label``, written once to
core.identities.metadata and to the in-memory entry), and the label is
server-assigned exactly while it still equals that record.

The fixtures come from the real producers: the persisted mint path of
``resolve_session_identity``, ``register_minted_agent_in_dict`` and the real
``set_agent_label_resolved`` collision rename. Only the database is mocked.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tests.no_live_redis import no_live_redis  # noqa: F401

pytestmark = pytest.mark.usefixtures("no_live_redis")


@pytest.fixture
def minted(monkeypatch):
    """Mint one agent through the persisted path and return its pieces."""
    import src.agent_metadata_persistence as amp

    registry: dict = {}
    monkeypatch.setattr(amp, "agent_metadata", registry)
    server = SimpleNamespace(agent_metadata=registry)

    db = AsyncMock()
    db.get_session = AsyncMock(return_value=None)
    db.get_identity = AsyncMock(
        return_value=SimpleNamespace(identity_id="ident-1", metadata={})
    )
    db.get_agent = AsyncMock(return_value=None)
    db.find_agent_by_label = AsyncMock(return_value=None)
    db.update_agent_fields = AsyncMock(return_value=True)
    db.agent_has_tag = AsyncMock(return_value=False)
    cache = AsyncMock()
    cache.get = AsyncMock(return_value=None)
    raw = AsyncMock()
    raw.get = AsyncMock(return_value=None)

    async def _raw():
        return raw

    patches = [
        patch("src.mcp_handlers.identity.persistence._redis_cache", None),
        patch("src.cache.get_session_cache", return_value=cache),
        patch("src.mcp_handlers.identity.resolution.get_db", return_value=db),
        patch("src.mcp_handlers.identity.persistence.get_db", return_value=db),
        patch("src.mcp_handlers.identity.handlers.get_db", return_value=db),
        patch("src.cache.redis_client.get_redis", new=_raw),
        patch("src.mcp_handlers.identity.persistence.mcp_server", server),
        patch("src.mcp_handlers.shared.get_mcp_server", return_value=server),
        patch("src.mcp_handlers.identity.persistence._broadcaster", return_value=None),
    ]
    for p in patches:
        p.start()
    try:
        yield SimpleNamespace(db=db, registry=registry)
    finally:
        for p in reversed(patches):
            p.stop()


async def _mint(minted):
    from src.mcp_handlers.identity.handlers import resolve_session_identity

    result = await resolve_session_identity(
        session_key="auto-label-session",
        persist=True,
        force_new=True,
        client_hint="claude_code",
        model_type="claude-opus-5-5",
    )
    return result, minted.registry[result["agent_uuid"]]


def _signature(agent_uuid: str) -> dict:
    from src.mcp_handlers.support.agent_auth import compute_agent_signature

    with patch(
        "src.mcp_handlers.context.get_session_proof_origin",
        return_value="caller_asserted",
    ):
        return compute_agent_signature(agent_id=agent_uuid)


@pytest.mark.asyncio
async def test_the_mint_records_the_label_it_assigned(minted):
    result, meta = await _mint(minted)

    label = result["label"]
    assert label == f"claude_code-opus_{result['agent_uuid'][:8]}"
    written = minted.db.upsert_identity.await_args.kwargs["metadata"]
    assert written["label"] == written["auto_label"] == label
    assert meta.label == meta.auto_label == label


@pytest.mark.asyncio
async def test_a_server_assigned_label_reads_auto(minted):
    result, _meta = await _mint(minted)

    signature = _signature(result["agent_uuid"])

    assert signature["display_name"] == result["label"]
    assert signature["label_source"] == "auto"


@pytest.mark.asyncio
async def test_a_claimed_name_renamed_by_a_collision_still_reads_claimed(minted):
    """The rename produces the auto label's own shape, <name>_<uuid8>."""
    from src.mcp_handlers.identity.persistence import set_agent_label_resolved

    result, meta = await _mint(minted)
    agent_uuid = result["agent_uuid"]
    minted.db.find_agent_by_label = AsyncMock(return_value="another-agent-uuid")

    applied = await set_agent_label_resolved(agent_uuid, "worker")

    assert applied == f"worker_{agent_uuid[:8]}"
    assert meta.label == applied
    assert meta.auto_label == result["label"]
    signature = _signature(agent_uuid)
    assert signature["display_name"] == applied
    assert signature["label_source"] == "claimed"


@pytest.mark.asyncio
async def test_a_plain_claim_reads_claimed(minted):
    from src.mcp_handlers.identity.persistence import set_agent_label_resolved

    result, _meta = await _mint(minted)
    await set_agent_label_resolved(result["agent_uuid"], "reviewer")

    assert _signature(result["agent_uuid"])["label_source"] == "claimed"


@pytest.mark.asyncio
async def test_the_cold_start_loader_carries_the_record(monkeypatch):
    """After a restart the in-memory entry is rebuilt from the identity
    metadata the mint wrote, so the reading survives it."""
    import src.agent_metadata_persistence as amp
    from src import agent_storage

    label = "claude_code-opus_1856bb5c"
    record = SimpleNamespace(
        agent_id="1856bb5c-2553-4523-809b-a5d26bbd58d1",
        status="active",
        created_at=None,
        last_activity_at=None,
        updated_at=None,
        tags=[],
        notes="",
        purpose=None,
        parent_agent_id=None,
        spawn_reason=None,
        health_status="unknown",
        metadata={"label": label, "auto_label": label, "public_agent_id": "Claude_Opus_5_5_20260926"},
    )
    monkeypatch.setattr(agent_storage, "list_agents", AsyncMock(return_value=[record]))
    db = MagicMock()
    db.get_identities_batch = AsyncMock(return_value={})
    db.get_provisional_lineage_set = AsyncMock(return_value=set())
    with patch("src.db.get_db", return_value=db):
        loaded = await amp._load_metadata_from_postgres_async()

    meta = loaded[record.agent_id]
    assert meta.label == meta.auto_label == label


def test_label_source_rules():
    from src.services.identity_payloads import label_source_for

    auto = "claude_code-opus_1856bb5c"
    common = {"public_agent_id": "Claude_Opus_5_5_20260926", "structured_id": None}

    assert label_source_for(auto, auto_label=auto, **common) == "auto"
    assert label_source_for("worker_1856bb5c", auto_label=auto, **common) == "claimed"
    assert label_source_for("Claude_Opus_5_5_20260926", **common) == "auto"
    assert label_source_for(None, has_public_handle=True, **common) == "auto"
    assert label_source_for(None, public_agent_id=None, structured_id=None) == "uuid"
    # Without the mint's record there is nothing to tell an auto label from a
    # claim, and the older reading stands.
    assert label_source_for(auto, **common) == "claimed"


def test_knowledge_display_payload_uses_the_same_rule():
    from src.mcp_handlers.knowledge.handlers import _build_agent_display_payload

    auto = "claude_code-opus_1856bb5c"
    meta = SimpleNamespace(
        public_agent_id="Claude_Opus_5_5_20260926",
        structured_id=None,
        label=auto,
        display_name=None,
        auto_label=auto,
    )
    assert _build_agent_display_payload("u", meta, "h")["label_source"] == "auto"
    meta.label = "worker_1856bb5c"
    assert _build_agent_display_payload("u", meta, "h")["label_source"] == "claimed"


@pytest.mark.asyncio
async def test_the_name_a_knowledge_write_assigns_reads_auto(minted):
    """A mint with no client or model hint gets no label. The first
    knowledge write then names the agent Agent_<uuid8>, which the server
    chose, so it reads as server-assigned too."""
    from src.mcp_handlers.identity.handlers import resolve_session_identity
    from src.mcp_handlers.knowledge import handlers as knowledge

    result = await resolve_session_identity(
        session_key="unhinted-session", persist=True, force_new=True,
    )
    agent_uuid = result["agent_uuid"]
    meta = minted.registry[agent_uuid]
    assert not meta.label

    server = SimpleNamespace(agent_metadata=minted.registry)
    with patch.object(knowledge, "mcp_server", server), patch(
        "src.mcp_handlers.context.get_context_agent_id", return_value=agent_uuid,
    ):
        error, warning = knowledge._check_display_name_required(agent_uuid, {})

    assert error is None and warning
    assert meta.label == f"Agent_{agent_uuid[:8]}"
    signature = _signature(agent_uuid)
    assert signature["display_name"] == meta.label
    assert signature["label_source"] == "auto"
