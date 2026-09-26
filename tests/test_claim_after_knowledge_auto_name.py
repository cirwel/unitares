"""A name claimed after a knowledge write's auto-name is the name displayed.

A knowledge write by an agent with no meaningful label names it
``Agent_<uuid8>``: knowledge/handlers._check_display_name_required sets
``display_name``, ``label`` and ``auto_label`` together, in memory. A later
claim (identity(client_session_id=..., name=...), which goes through
set_agent_label) updated ``label`` only. Every reader prefers ``display_name``
(agent_auth.compute_agent_signature, the knowledge display payload), so the
signature kept showing ``Agent_<uuid8>`` with ``label_source: "auto"``.

A claim now clears that in-memory ``display_name``
(identity/persistence.drop_stale_display_name), leaving the agent as one that
was never auto-named. ``display_name`` is not an AgentMetadata field and is
never persisted, and the cold-start loader never restores it, so there is no
stored copy to keep in step.

A ``name`` on a check-in is not a claim path: every route sets ``agent_id``
to the bound UUID before the check-in's identity phase reads its label
(params_step.inject_identity, and the REST prebind for the direct handler),
so that phase never reads ``name``.

The fixtures come from the real producers: the persisted mint path of
``resolve_session_identity``, the real knowledge auto-name and the real
``set_agent_label_resolved`` (with its collision rename). Only the database
is mocked.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from tests.no_live_redis import no_live_redis  # noqa: F401

pytestmark = pytest.mark.usefixtures("no_live_redis")


@pytest.fixture
def auto_named(monkeypatch):
    """Mint an agent with no label, then let a knowledge write name it."""
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

    patches = [
        patch("src.mcp_handlers.identity.persistence._redis_cache", None),
        patch("src.cache.get_session_cache", return_value=cache),
        patch("src.mcp_handlers.identity.resolution.get_db", return_value=db),
        patch("src.mcp_handlers.identity.persistence.get_db", return_value=db),
        patch("src.mcp_handlers.identity.handlers.get_db", return_value=db),
        patch("src.db.get_db", return_value=db),
        patch("src.mcp_handlers.identity.persistence.mcp_server", server),
        patch("src.mcp_handlers.shared.get_mcp_server", return_value=server),
        patch("src.mcp_handlers.identity.persistence._broadcaster", return_value=None),
    ]
    for p in patches:
        p.start()
    try:
        yield SimpleNamespace(db=db, registry=registry, server=server)
    finally:
        for p in reversed(patches):
            p.stop()


async def _auto_name(auto_named):
    from src.mcp_handlers.identity.handlers import resolve_session_identity
    from src.mcp_handlers.knowledge import handlers as knowledge

    result = await resolve_session_identity(
        session_key="unhinted-session", persist=True, force_new=True,
    )
    agent_uuid = result["agent_uuid"]
    with patch.object(knowledge, "mcp_server", auto_named.server), patch(
        "src.mcp_handlers.context.get_context_agent_id", return_value=agent_uuid,
    ):
        error, warning = knowledge._check_display_name_required(agent_uuid, {})
    assert error is None and warning
    meta = auto_named.registry[agent_uuid]
    assert meta.display_name == meta.label == meta.auto_label == f"Agent_{agent_uuid[:8]}"
    return agent_uuid, meta


def _signature(agent_uuid: str) -> dict:
    from src.mcp_handlers.support.agent_auth import compute_agent_signature

    with patch(
        "src.mcp_handlers.context.get_session_proof_origin",
        return_value="caller_asserted",
    ):
        return compute_agent_signature(agent_id=agent_uuid)


def _knowledge_display(agent_uuid: str, meta) -> dict:
    from src.mcp_handlers.knowledge.handlers import _build_agent_display_payload

    return _build_agent_display_payload(agent_uuid, meta, agent_uuid)


@pytest.mark.asyncio
async def test_a_claim_after_the_auto_name_is_displayed_and_reads_claimed(auto_named):
    from src.mcp_handlers.identity.persistence import set_agent_label_resolved

    agent_uuid, meta = await _auto_name(auto_named)

    applied = await set_agent_label_resolved(agent_uuid, "reviewer")

    assert applied == "reviewer"
    assert getattr(meta, "display_name", None) is None
    signature = _signature(agent_uuid)
    assert signature["display_name"] == "reviewer"
    assert signature["label_source"] == "claimed"
    display = _knowledge_display(agent_uuid, meta)
    assert display["display_name"] == "reviewer"
    assert display["label_source"] == "claimed"


@pytest.mark.asyncio
async def test_a_claim_renamed_by_a_collision_is_displayed_as_renamed(auto_named):
    from src.mcp_handlers.identity.persistence import set_agent_label_resolved

    agent_uuid, _meta = await _auto_name(auto_named)
    auto_named.db.find_agent_by_label = AsyncMock(return_value="another-agent-uuid")

    applied = await set_agent_label_resolved(agent_uuid, "reviewer")

    assert applied == f"reviewer_{agent_uuid[:8]}"
    signature = _signature(agent_uuid)
    assert signature["display_name"] == applied
    assert signature["label_source"] == "claimed"


def test_a_label_equal_to_the_displayed_name_changes_nothing():
    from src.mcp_handlers.identity.persistence import drop_stale_display_name

    meta = SimpleNamespace(display_name="Agent_1856bb5c", label="Agent_1856bb5c")
    drop_stale_display_name(meta, "Agent_1856bb5c")
    assert meta.display_name == "Agent_1856bb5c"

    never_auto_named = SimpleNamespace(label="reviewer")
    drop_stale_display_name(never_auto_named, "worker")
    assert not hasattr(never_auto_named, "display_name")
