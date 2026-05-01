"""Unit tests for S8a Phase-1 default-stamp rule.

Covers `src/grounding/onboard_classifier.py:default_tags_for_onboard`
and the handler-side wiring `_stamp_default_tags_on_onboard`.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.grounding.onboard_classifier import (
    EPHEMERAL_DEFAULT_TAGS,
    RESIDENT_DEFAULT_TAGS,
    default_tags_for_onboard,
)


class TestDefaultTagsForOnboard:
    def test_resident_name_returns_resident_tags(self):
        for name in ("Lumen", "Vigil", "Sentinel", "Watcher", "Steward", "Chronicler"):
            assert default_tags_for_onboard(name, existing_tags=None) == RESIDENT_DEFAULT_TAGS

    def test_unknown_name_returns_ephemeral(self):
        assert default_tags_for_onboard("some-agent", existing_tags=None) == EPHEMERAL_DEFAULT_TAGS

    def test_none_name_returns_ephemeral(self):
        assert default_tags_for_onboard(None, existing_tags=None) == EPHEMERAL_DEFAULT_TAGS

    def test_empty_name_returns_ephemeral(self):
        assert default_tags_for_onboard("", existing_tags=None) == EPHEMERAL_DEFAULT_TAGS

    def test_resident_name_with_existing_tags_returns_none(self):
        assert default_tags_for_onboard("Lumen", existing_tags=["custom"]) is None

    def test_unknown_name_with_existing_tags_returns_none(self):
        assert default_tags_for_onboard("some-agent", existing_tags=["persistent"]) is None

    def test_empty_list_is_treated_as_no_tags(self):
        assert default_tags_for_onboard("Lumen", existing_tags=[]) == RESIDENT_DEFAULT_TAGS
        assert default_tags_for_onboard("other", existing_tags=[]) == EPHEMERAL_DEFAULT_TAGS

    def test_return_value_is_a_new_list_not_shared_reference(self):
        a = default_tags_for_onboard("Lumen", existing_tags=None)
        b = default_tags_for_onboard("Lumen", existing_tags=None)
        a.append("mutated")
        assert b == RESIDENT_DEFAULT_TAGS

    def test_resident_label_matching_is_case_sensitive(self):
        assert default_tags_for_onboard("lumen", existing_tags=None) == EPHEMERAL_DEFAULT_TAGS
        assert default_tags_for_onboard("LUMEN", existing_tags=None) == EPHEMERAL_DEFAULT_TAGS

    def test_structured_agent_id_does_not_match_resident(self):
        assert default_tags_for_onboard("Lumen_abc123", existing_tags=None) == EPHEMERAL_DEFAULT_TAGS


def _make_meta(tags=None, label=None):
    class _Meta:
        pass
    m = _Meta()
    m.tags = tags
    m.label = label
    return m


async def _run_stamp(name, existing_tags, *, missing_meta=False, meta_label=None):
    meta = None if missing_meta else _make_meta(tags=existing_tags, label=meta_label)
    agent_uuid = "uuid-1234"
    fake_update = AsyncMock()

    with patch("src.agent_storage.update_agent", fake_update):
        from src.grounding.onboard_classifier import stamp_default_class_tags
        result = await stamp_default_class_tags(agent_uuid, name, meta=meta)

    return meta, fake_update, result


@pytest.mark.asyncio
async def test_stamp_unknown_name_gets_ephemeral_and_persists():
    meta, fake_update, result = await _run_stamp("some-agent", existing_tags=None)
    assert meta.tags == ["ephemeral"]
    assert result == ["ephemeral"]
    fake_update.assert_awaited_once_with(agent_id="uuid-1234", tags=["ephemeral"])


@pytest.mark.asyncio
async def test_stamp_resident_name_gets_persistent_autonomous():
    meta, fake_update, result = await _run_stamp("Sentinel", existing_tags=None)
    assert meta.tags == ["persistent", "autonomous"]
    assert result == ["persistent", "autonomous"]
    fake_update.assert_awaited_once_with(
        agent_id="uuid-1234", tags=["persistent", "autonomous"]
    )


@pytest.mark.asyncio
async def test_stamp_preexisting_tags_are_not_overwritten():
    meta, fake_update, result = await _run_stamp("Sentinel", existing_tags=["custom"])
    assert meta.tags == ["custom"]
    assert result is None
    fake_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_stamp_missing_metadata_object_still_writes_tags_to_db():
    """Identity with no in-memory metadata entry yet — DB write is still
    source of truth; next load_metadata_async picks it up."""
    _, fake_update, result = await _run_stamp(
        "some-agent", existing_tags=None, missing_meta=True
    )
    assert result == ["ephemeral"]
    fake_update.assert_awaited_once_with(agent_id="uuid-1234", tags=["ephemeral"])


# Phase-2 additions: name=None fallback to meta.label so the auto-create
# path in src/mcp_handlers/updates/phases.py (recovery branch, where the
# label is not threaded through as a parameter) still classifies correctly.

@pytest.mark.asyncio
async def test_stamp_name_none_falls_back_to_meta_label_resident():
    meta, fake_update, result = await _run_stamp(
        None, existing_tags=None, meta_label="Sentinel"
    )
    assert result == ["persistent", "autonomous"]
    assert meta.tags == ["persistent", "autonomous"]
    fake_update.assert_awaited_once_with(
        agent_id="uuid-1234", tags=["persistent", "autonomous"]
    )


@pytest.mark.asyncio
async def test_stamp_name_none_falls_back_to_meta_label_unknown():
    meta, _, result = await _run_stamp(
        None, existing_tags=None, meta_label="claude_desktop-claude"
    )
    assert result == ["ephemeral"]
    assert meta.tags == ["ephemeral"]


@pytest.mark.asyncio
async def test_stamp_name_none_with_no_label_still_stamps_ephemeral():
    """The 441-update claude_desktop-claude row would be untagged today
    if name=None and meta.label=None; we still stamp ``ephemeral`` so the
    promotion sweep can subsequently lift to session_like."""
    _, _, result = await _run_stamp(None, existing_tags=None, meta_label=None)
    assert result == ["ephemeral"]


@pytest.mark.asyncio
async def test_explicit_name_takes_precedence_over_meta_label():
    """When the caller threads a name explicitly, that wins over meta.label.
    Protects the onboard path from a stale label clobbering an updated
    name argument in the same request."""
    meta = _make_meta(tags=None, label="something_old")
    fake_update = AsyncMock()
    with patch("src.agent_storage.update_agent", fake_update):
        from src.grounding.onboard_classifier import stamp_default_class_tags
        result = await stamp_default_class_tags("uuid-x", "Sentinel", meta=meta)
    assert result == ["persistent", "autonomous"]


@pytest.mark.asyncio
async def test_stamp_does_not_mutate_meta_when_persist_fails():
    """P011 regression: persist must come before in-memory mutation, so a
    failed DB write does not leave meta.tags pointing at a value the DB has
    no record of (which would be clobbered on the next metadata reload).
    """
    meta = _make_meta(tags=None)
    fake_update = AsyncMock(side_effect=RuntimeError("PG unavailable"))
    with patch("src.agent_storage.update_agent", fake_update):
        from src.grounding.onboard_classifier import stamp_default_class_tags
        with pytest.raises(RuntimeError):
            await stamp_default_class_tags("uuid-1234", "Sentinel", meta=meta)
    assert meta.tags is None
    fake_update.assert_awaited_once()


@pytest.mark.asyncio
async def test_onboard_wrapper_still_calls_through():
    """The thin ``_stamp_default_tags_on_onboard`` wrapper in identity/handlers.py
    must still produce the same end-state for backward compat."""
    meta = _make_meta(tags=None)
    fake_server = MagicMock()
    fake_server.agent_metadata = {"uuid-x": meta}
    fake_update = AsyncMock()
    with patch(
        "src.mcp_handlers.identity.handlers.mcp_server", fake_server
    ), patch("src.agent_storage.update_agent", fake_update):
        from src.mcp_handlers.identity.handlers import _stamp_default_tags_on_onboard
        await _stamp_default_tags_on_onboard("uuid-x", "Watcher")
    assert meta.tags == ["persistent", "autonomous"]
    fake_update.assert_awaited_once()
