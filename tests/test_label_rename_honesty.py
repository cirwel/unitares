"""A collision rename must not be invisible to the agent it happened to.

`set_agent_label` renames on collision — the caller asks for `Doctor` and the
row becomes `Doctor_7dea7dcb` — but it returns only a bool, so the onboard
handler read "success" as "I got the name I asked for" and echoed the
REQUESTED label back. Verified against the live server on 2026-08-26: a second
mint of the same name returned `display_name="ZzTestCollision"` while
`core.agents` held `ZzTestCollision_4dc58779`.

Every resident surface resolves by exact label, so that divergence is how a
renamed resident goes missing with nothing anywhere reporting it. Watcher sat
in that shape from 2026-04-19 to 2026-06-14.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.mcp_handlers.identity import persistence


UUID = "7dea7dcb-e887-4c90-8c8a-4f3433da102b"
OTHER = "dc94fa70-6186-4862-aeb6-3fc9801263c8"


class _DB:
    def __init__(self, incumbent_is_resident=False):
        self._incumbent_is_resident = incumbent_is_resident
        self.written = None

    async def get_identity(self, _uuid):
        return SimpleNamespace(metadata={})

    async def agent_has_tag(self, _uuid, _tag):
        return self._incumbent_is_resident

    async def update_agent_fields(self, _uuid, label=None):
        self.written = label
        return True


def _wire(monkeypatch, db, incumbent):
    """Exercise the REAL resolver; stub only the DB and the label lookup."""
    monkeypatch.setattr(persistence, "get_db", lambda: db)
    monkeypatch.setattr(persistence, "_find_agent_by_label",
                        AsyncMock(return_value=incumbent))
    monkeypatch.setattr(persistence, "mcp_server",
                        SimpleNamespace(agent_metadata={}), raising=False)
    monkeypatch.setattr(persistence, "_broadcaster", lambda: None)


@pytest.mark.asyncio
async def test_no_collision_applies_the_requested_label(monkeypatch):
    db = _DB()
    _wire(monkeypatch, db, incumbent=None)
    assert await persistence.set_agent_label_resolved(UUID, "Doctor") == "Doctor"
    assert db.written == "Doctor"


@pytest.mark.asyncio
async def test_collision_returns_the_renamed_label_not_the_request(monkeypatch):
    """⛔The bug. The caller asked for `Doctor` and the row became
    `Doctor_7dea7dcb`; returning the request is what let the mint response
    tell the agent it holds a label the database does not have."""
    db = _DB()
    _wire(monkeypatch, db, incumbent=OTHER)
    got = await persistence.set_agent_label_resolved(UUID, "Doctor")
    assert got == "Doctor_7dea7dcb"
    assert db.written == "Doctor_7dea7dcb", "the DB and the return must agree"


@pytest.mark.asyncio
async def test_resident_incumbent_still_renames_the_newcomer(monkeypatch):
    """A fork colliding with a live resident must still yield — unchanged."""
    db = _DB(incumbent_is_resident=True)
    _wire(monkeypatch, db, incumbent=OTHER)
    assert await persistence.set_agent_label_resolved(UUID, "Vigil") == "Vigil_7dea7dcb"


@pytest.mark.asyncio
async def test_self_collision_is_not_a_collision(monkeypatch):
    """Re-setting your own label must not suffix it."""
    db = _DB()
    _wire(monkeypatch, db, incumbent=UUID)
    assert await persistence.set_agent_label_resolved(UUID, "Doctor") == "Doctor"


@pytest.mark.asyncio
async def test_write_failure_returns_none_not_a_label(monkeypatch):
    db = _DB()

    async def _fail(_uuid, label=None):
        return False

    db.update_agent_fields = _fail
    _wire(monkeypatch, db, incumbent=None)
    assert await persistence.set_agent_label_resolved(UUID, "Doctor") is None


@pytest.mark.asyncio
async def test_bool_wrapper_is_true_even_when_renamed():
    """The bool contract is unchanged — that is exactly why it was misleading.

    Callers that only need "did the write land" keep working; callers that
    feed a response must use the resolved form.
    """
    with patch.object(persistence, "set_agent_label_resolved",
                      AsyncMock(return_value="Doctor_7dea7dcb")):
        assert await persistence.set_agent_label("uuid", "Doctor") is True


@pytest.mark.asyncio
async def test_bool_wrapper_is_false_on_failure():
    with patch.object(persistence, "set_agent_label_resolved",
                      AsyncMock(return_value=None)):
        assert await persistence.set_agent_label("uuid", "Doctor") is False


@pytest.mark.asyncio
async def test_empty_inputs_return_none_not_a_label():
    assert await persistence.set_agent_label_resolved("", "Doctor") is None
    assert await persistence.set_agent_label_resolved("uuid", "") is None


def test_resolved_and_bool_are_not_the_same_function():
    """⛔A future refactor that collapses these back into one bool re-creates
    the exact silent divergence this file exists to prevent."""
    assert persistence.set_agent_label is not persistence.set_agent_label_resolved
    import inspect
    assert "may DIFFER" in inspect.getdoc(persistence.set_agent_label)
