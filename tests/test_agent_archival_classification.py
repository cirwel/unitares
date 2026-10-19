"""Archival candidates depend on lifecycle metadata, never agent branding."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src import agent_lifecycle


AGENT_ID = "00000000-0000-4000-8000-000000000001"


def _meta(*, label="Maple", tags=(), updates=2, trust_tier=None):
    return SimpleNamespace(
        label=label,
        display_name=label,
        tags=list(tags),
        total_updates=updates,
        trust_tier=trust_tier,
        status="active",
    )


@pytest.fixture(autouse=True)
def _eight_hours_since_activity(monkeypatch):
    monkeypatch.setattr(agent_lifecycle, "_agent_age_hours", lambda meta: 8.0)


@pytest.mark.parametrize(
    "label", ["claude_session_20261018", "codex_session_20261018", "Maple"]
)
@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        ((), False),
        (("ephemeral",), False),
        (("engaged_ephemeral",), False),
        (("archive-eligible",), True),
        (("ephemeral", "archive-eligible"), True),
        (("engaged_ephemeral", "archive-eligible"), True),
    ],
)
def test_labels_and_automatic_class_tags_do_not_authorize_archival(label, tags, expected):
    candidate, reason = agent_lifecycle.classify_for_archival(
        AGENT_ID, _meta(label=label, tags=tags)
    )

    assert candidate is expected
    if expected:
        assert "archive-eligible agent" in reason
    else:
        assert reason == ""


@pytest.mark.parametrize(
    ("updates", "hours"), [(0, 8.0), (6, 8.0), (2, 5.0)]
)
def test_explicit_archival_tag_preserves_activity_limits(monkeypatch, updates, hours):
    monkeypatch.setattr(agent_lifecycle, "_agent_age_hours", lambda meta: hours)

    assert agent_lifecycle.classify_for_archival(
        AGENT_ID, _meta(tags=("archive-eligible",), updates=updates)
    ) == (False, "")


@pytest.mark.parametrize("tags", [(), ("ephemeral",), ("archive-eligible",)])
def test_unlabeled_zero_update_agents_remain_initializing(tags):
    assert agent_lifecycle.classify_for_archival(
        AGENT_ID, _meta(label="", tags=tags, updates=0)
    ) == (False, "")


@pytest.mark.parametrize("updates", [1, 2])
def test_unlabeled_stale_agents_keep_existing_classification(updates):
    candidate, _ = agent_lifecycle.classify_for_archival(
        AGENT_ID, _meta(label="", updates=updates)
    )

    assert candidate is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tags", "trust_tier"),
    [
        (("archive-eligible", "persistent"), None),
        (("archive-eligible", "protected"), None),
        (("archive-eligible", "pioneer"), None),
        (("archive-eligible",), "verified"),
        (("archive-eligible",), "established"),
        (("archive-eligible",), "trusted"),
    ],
)
async def test_sweep_protections_win_over_archival_opt_in(monkeypatch, tags, trust_tier):
    monkeypatch.setenv("UNITARES_ENABLE_AUTO_AGENT_ARCHIVAL", "1")
    meta = _meta(tags=tags, trust_tier=trust_tier)

    result = await agent_lifecycle.auto_archive_orphan_agents(
        dry_run=True, _metadata={AGENT_ID: meta}, _monitors={}
    )

    assert result == []
    assert meta.status == "active"


@pytest.mark.asyncio
async def test_explicit_candidate_does_not_enable_archival(monkeypatch):
    monkeypatch.delenv("UNITARES_ENABLE_AUTO_AGENT_ARCHIVAL", raising=False)
    meta = _meta(tags=("archive-eligible",))

    result = await agent_lifecycle.auto_archive_orphan_agents(
        _metadata={AGENT_ID: meta}, _monitors={}
    )

    assert len(result) == 1
    assert result[0]["would_archive"] is True
    assert result[0]["archived"] is False
    assert result[0]["auto_archival_enabled"] is False
    assert meta.status == "active"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "label", ["claude_session_20261018", "codex_session_20261018", "Maple"]
)
@pytest.mark.parametrize("opt_in", [False, True])
async def test_onboarding_and_promotion_do_not_change_archival_opt_in(monkeypatch, label, opt_in):
    """Exercise real tag producers with storage mocked; never connect to a DB."""
    from src import agent_storage
    from src.grounding.class_promotion import promote_engaged_ephemeral
    from src.grounding.onboard_classifier import stamp_default_class_tags
    from src.mcp_handlers import shared

    meta = _meta(label=label)
    persist = AsyncMock()
    monkeypatch.setattr(agent_storage, "update_agent", persist)
    stamped = await stamp_default_class_tags(AGENT_ID, name=label, meta=meta)

    assert stamped == meta.tags == ["ephemeral"]
    persist.assert_awaited_once_with(agent_id=AGENT_ID, tags=["ephemeral"])
    assert agent_lifecycle.classify_for_archival(AGENT_ID, meta) == (False, "")

    if opt_in:
        meta.tags.append("archive-eligible")
    assert agent_lifecycle.classify_for_archival(AGENT_ID, meta)[0] is opt_in

    meta.total_updates = 3
    connection = SimpleNamespace(fetchrow=AsyncMock(return_value={
        "promoted_count": 1,
        "promoted_ids": [AGENT_ID],
        "sample": [],
    }))
    db = MagicMock()
    db.acquire.return_value.__aenter__.return_value = connection
    monkeypatch.setattr(shared, "lazy_mcp_server", SimpleNamespace(agent_metadata={AGENT_ID: meta}))

    result = await promote_engaged_ephemeral(db=db)

    assert result["threshold"] == 3
    assert result["promoted"] == 1
    connection.fetchrow.assert_awaited_once()
    assert "ephemeral" not in meta.tags
    assert "engaged_ephemeral" in meta.tags
    assert ("archive-eligible" in meta.tags) is opt_in
    assert agent_lifecycle.classify_for_archival(AGENT_ID, meta)[0] is opt_in

    # Promotion must not turn the existing five-update archival limit into
    # an effective two-update limit by removing the automatic class tag.
    meta.total_updates = 5
    assert agent_lifecycle.classify_for_archival(AGENT_ID, meta)[0] is opt_in
    meta.total_updates = 6
    assert agent_lifecycle.classify_for_archival(AGENT_ID, meta) == (False, "")
