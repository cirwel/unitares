"""Archival candidates depend on lifecycle metadata, never agent branding."""

from types import SimpleNamespace

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
    [((), False), (("ephemeral",), True), (("engaged_ephemeral",), False)],
)
def test_labels_do_not_determine_ephemeral_archival(label, tags, expected):
    candidate, reason = agent_lifecycle.classify_for_archival(
        AGENT_ID, _meta(label=label, tags=tags)
    )

    assert candidate is expected
    if expected:
        assert "ephemeral session agent" in reason
    else:
        assert reason == ""


@pytest.mark.parametrize(
    ("updates", "hours"), [(0, 8.0), (6, 8.0), (2, 5.0)]
)
def test_explicit_ephemeral_tag_preserves_activity_limits(monkeypatch, updates, hours):
    monkeypatch.setattr(agent_lifecycle, "_agent_age_hours", lambda meta: hours)

    assert agent_lifecycle.classify_for_archival(
        AGENT_ID, _meta(tags=("ephemeral",), updates=updates)
    ) == (False, "")


@pytest.mark.parametrize("tags", [(), ("ephemeral",)])
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
        (("ephemeral", "persistent"), None),
        (("ephemeral", "protected"), None),
        (("ephemeral", "pioneer"), None),
        (("ephemeral",), "verified"),
        (("ephemeral",), "established"),
        (("ephemeral",), "trusted"),
    ],
)
async def test_sweep_protections_win_over_ephemeral_tag(monkeypatch, tags, trust_tier):
    monkeypatch.setenv("UNITARES_ENABLE_AUTO_AGENT_ARCHIVAL", "1")
    meta = _meta(tags=tags, trust_tier=trust_tier)

    result = await agent_lifecycle.auto_archive_orphan_agents(
        dry_run=True, _metadata={AGENT_ID: meta}, _monitors={}
    )

    assert result == []
    assert meta.status == "active"


@pytest.mark.asyncio
async def test_explicit_ephemeral_candidate_does_not_enable_archival(monkeypatch):
    monkeypatch.delenv("UNITARES_ENABLE_AUTO_AGENT_ARCHIVAL", raising=False)
    meta = _meta(tags=("ephemeral",))

    result = await agent_lifecycle.auto_archive_orphan_agents(
        _metadata={AGENT_ID: meta}, _monitors={}
    )

    assert len(result) == 1
    assert result[0]["would_archive"] is True
    assert result[0]["archived"] is False
    assert result[0]["auto_archival_enabled"] is False
    assert meta.status == "active"
