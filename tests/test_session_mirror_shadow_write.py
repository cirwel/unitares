"""Unit tests for the Redis-retirement Phase 1A shadow dual-write.

When UNITARES_SESSION_MIRROR_SHADOW is off (default), the mirror helpers must be
no-ops. When on, they must dual-write to the PG mirror (best-effort: a DB failure
is swallowed and never propagates to the live identity path).

See docs/proposals/redis-retirement-phase-1-plan.md.
"""

import sys
from pathlib import Path
from unittest.mock import patch, AsyncMock

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.mcp_handlers.identity import persistence, session as session_mod

_SK = "agent-abc123def456"
_AU = "11111111-1111-4111-8111-111111111111"


# ---------------------------------------------------------------------------
# config flags default off
# ---------------------------------------------------------------------------

def test_flags_default_off(monkeypatch):
    from config import governance_config as gc
    monkeypatch.delenv("UNITARES_SESSION_MIRROR_SHADOW", raising=False)
    monkeypatch.delenv("UNITARES_SESSION_MIRROR_APPLY", raising=False)
    assert gc.session_mirror_shadow_enabled() is False
    assert gc.session_mirror_apply_enabled() is False
    monkeypatch.setenv("UNITARES_SESSION_MIRROR_SHADOW", "1")
    assert gc.session_mirror_shadow_enabled() is True


# ---------------------------------------------------------------------------
# session-binding mirror
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_binding_mirror_noop_when_flag_off():
    db = AsyncMock()
    with patch("config.governance_config.session_mirror_shadow_enabled", return_value=False), \
         patch("src.mcp_handlers.identity.persistence.get_db", return_value=db):
        await persistence._shadow_mirror_session_binding(_SK, _AU, bind_ip_ua="1.2.3.4:ab")
    db.upsert_session_binding.assert_not_called()


@pytest.mark.asyncio
async def test_binding_mirror_writes_when_flag_on():
    db = AsyncMock()
    db.upsert_session_binding = AsyncMock(return_value="inserted")
    with patch("config.governance_config.session_mirror_shadow_enabled", return_value=True), \
         patch("src.mcp_handlers.identity.persistence.get_db", return_value=db):
        await persistence._shadow_mirror_session_binding(
            _SK, _AU, display_agent_id="Claude_Code", spawn_reason="new_session",
            bind_ip_ua="1.2.3.4:ab", trajectory_required=True, mint_guard=True,
        )
    db.upsert_session_binding.assert_awaited_once()
    args, kwargs = db.upsert_session_binding.call_args
    assert args[0] == _SK and args[1] == _AU
    assert kwargs["public_agent_id"] == "Claude_Code"
    assert kwargs["bind_ip_ua"] == "1.2.3.4:ab"
    assert kwargs["trajectory_required"] is True
    assert kwargs["mint_guard"] is True


@pytest.mark.asyncio
async def test_binding_mirror_swallows_db_failure():
    db = AsyncMock()
    db.upsert_session_binding = AsyncMock(side_effect=Exception("pg down"))
    with patch("config.governance_config.session_mirror_shadow_enabled", return_value=True), \
         patch("src.mcp_handlers.identity.persistence.get_db", return_value=db):
        # must not raise
        await persistence._shadow_mirror_session_binding(_SK, _AU)


@pytest.mark.asyncio
async def test_binding_mirror_logs_on_blocked(caplog):
    db = AsyncMock()
    db.upsert_session_binding = AsyncMock(return_value="blocked")
    with patch("config.governance_config.session_mirror_shadow_enabled", return_value=True), \
         patch("src.mcp_handlers.identity.persistence.get_db", return_value=db), \
         caplog.at_level("WARNING"):
        await persistence._shadow_mirror_session_binding(_SK, _AU)
    assert any("S21A_PG_BINDING_BLOCKED" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# onboard-pin mirror
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pin_mirror_noop_when_flag_off():
    db = AsyncMock()
    with patch("config.governance_config.session_mirror_shadow_enabled", return_value=False), \
         patch("src.db.get_db", return_value=db):
        await session_mod._shadow_mirror_onboard_pins(["ua:aa", "ua:bb"], _AU, "agent-csid")
    db.set_onboard_pin_pg.assert_not_called()


@pytest.mark.asyncio
async def test_pin_mirror_writes_each_candidate_when_on():
    db = AsyncMock()
    db.set_onboard_pin_pg = AsyncMock(return_value=True)
    with patch("config.governance_config.session_mirror_shadow_enabled", return_value=True), \
         patch("src.db.get_db", return_value=db):
        await session_mod._shadow_mirror_onboard_pins(
            ["ua:aa|claude", "ua:bb"], _AU, "agent-csid", if_absent=True,
        )
    assert db.set_onboard_pin_pg.await_count == 2
    seen_fps = {c.args[0] for c in db.set_onboard_pin_pg.call_args_list}
    assert seen_fps == {"ua:aa|claude", "ua:bb"}
    for c in db.set_onboard_pin_pg.call_args_list:
        assert c.kwargs["if_absent"] is True


@pytest.mark.asyncio
async def test_pin_mirror_swallows_per_candidate_failure():
    db = AsyncMock()
    db.set_onboard_pin_pg = AsyncMock(side_effect=Exception("pg down"))
    with patch("config.governance_config.session_mirror_shadow_enabled", return_value=True), \
         patch("src.db.get_db", return_value=db):
        # must not raise even though every candidate write fails
        await session_mod._shadow_mirror_onboard_pins(["ua:aa"], _AU, "agent-csid")
