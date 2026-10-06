"""#2682: the substrate HTTP-reject gates failed open when the claims lookup
raised, so an enrolled resident's UUID could resume over HTTP during a database
fault. A UUID last seen with a claim (any lookup that found one, or the startup
load) is now refused with `substrate_check_unavailable`; any other UUID still
falls through, so an unreachable claims table does not block ordinary agents.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.substrate import verification

RESIDENT = "f92dcea8-4786-412a-a0eb-362c273382f5"
ORDINARY = "11111111-2222-4333-8444-555555555555"


@pytest.fixture(autouse=True)
def _clean_known(monkeypatch):
    """Each test starts with the table loaded and no claims known."""
    verification._known_claimed.clear()
    monkeypatch.setattr(verification, "_claims_loaded", True)
    yield
    verification._known_claimed.clear()


def _db(*, fetchrow=None, fetch=None):
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=fetchrow)
    conn.fetch = AsyncMock(return_value=fetch or [])
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=False)
    db = MagicMock()
    db.acquire = MagicMock(return_value=acquire)
    return db


def _http():
    return patch(
        "src.mcp_handlers.context.get_session_signals",
        return_value=SimpleNamespace(peer_pid=None, ip_ua_fingerprint="ip:ua"),
    )


def _lookup_fails():
    return patch.object(
        verification, "fetch_substrate_claim", AsyncMock(side_effect=ConnectionError("pg down")),
    )


@pytest.mark.asyncio
async def test_lookups_record_and_drop_claimed_uuids():
    row = {
        "agent_id": RESIDENT, "expected_launchd_label": "com.unitares.sentinel",
        "expected_executable_path": "/x", "enrolled_at": None,
        "enrolled_by_operator": True, "notes": None,
    }
    with patch("src.db.get_db", return_value=_db(fetchrow=row)):
        assert await verification.fetch_substrate_claim(RESIDENT) is not None
    assert verification.known_substrate_claimed(RESIDENT)

    with patch("src.db.get_db", return_value=_db(fetchrow=None)):
        assert await verification.fetch_substrate_claim(RESIDENT) is None
    assert not verification.known_substrate_claimed(RESIDENT)


@pytest.mark.asyncio
async def test_the_startup_load_records_every_claim():
    with patch("src.db.get_db", return_value=_db(fetch=[{"agent_id": RESIDENT}])):
        assert await verification.load_known_substrate_claims() == 1
    assert verification.known_substrate_claimed(RESIDENT)
    assert not verification.known_substrate_claimed(ORDINARY)


@pytest.mark.asyncio
@pytest.mark.parametrize("uuid, refused", [(RESIDENT, True), (ORDINARY, False)])
async def test_the_session_gate_refuses_a_known_resident_when_the_lookup_fails(uuid, refused):
    from src.mcp_handlers.identity import resolution

    verification._known_claimed.add(RESIDENT)
    with _http(), _lookup_fails():
        out = await resolution._substrate_http_reject(uuid, "unit")

    if refused:
        assert out["resume_failed"] is True
        assert out["error"] == "substrate_check_unavailable"
        assert out["agent_uuid"] == RESIDENT
    else:
        assert out is None


@pytest.mark.asyncio
async def test_a_uds_caller_is_never_refused_by_a_failed_lookup():
    from src.mcp_handlers.identity import resolution

    verification._known_claimed.add(RESIDENT)
    with patch(
        "src.mcp_handlers.context.get_session_signals",
        return_value=SimpleNamespace(peer_pid=4321, ip_ua_fingerprint=None),
    ), _lookup_fails():
        assert await resolution._substrate_http_reject(RESIDENT, "unit") is None


@pytest.mark.asyncio
async def test_the_token_gate_refuses_a_known_resident_when_the_lookup_fails():
    from src.mcp_handlers.identity import resolution

    verification._known_claimed.add(RESIDENT)
    with _http(), _lookup_fails(), \
         patch.object(resolution, "_get_redis", side_effect=AssertionError("no PATH 1")):
        out = await resolution.resolve_session_identity(
            "agent-test-session", resume=True, token_agent_uuid=RESIDENT,
        )

    assert out["resume_failed"] is True
    assert out["error"] == "substrate_check_unavailable"
    assert out["token_agent_uuid"] == RESIDENT


@pytest.mark.asyncio
async def test_before_the_table_loads_a_failed_lookup_refuses_any_uuid(monkeypatch):
    """A restart during a database outage starts with nothing known; until the
    table loads, a failed lookup cannot tell a resident from anyone else, so it
    refuses rather than admits."""
    from src.mcp_handlers.identity import resolution

    monkeypatch.setattr(verification, "_claims_loaded", False)
    with _http(), _lookup_fails():
        out = await resolution._substrate_http_reject(ORDINARY, "unit")
    assert out["error"] == "substrate_check_unavailable"
    assert "not loaded yet" in out["message"]

    with patch("src.db.get_db", return_value=_db(fetch=[{"agent_id": RESIDENT}])):
        await verification.load_known_substrate_claims()
    with _http(), _lookup_fails():
        assert await resolution._substrate_http_reject(ORDINARY, "unit") is None
        assert (await resolution._substrate_http_reject(RESIDENT, "unit"))["resume_failed"]


class _Stop(Exception):
    pass


@pytest.mark.asyncio
async def test_the_refresh_retries_the_first_load_then_reloads_every_interval(monkeypatch):
    """The first load backs off until it succeeds; after that the table is
    reloaded at the interval, so a later enrollment becomes known."""
    from src import background_tasks

    monkeypatch.setattr(verification, "_claims_loaded", False)
    load = AsyncMock(side_effect=[ConnectionError("down"), ConnectionError("down"), 1, 2])
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 5:
            raise _Stop

    with patch.object(verification, "load_known_substrate_claims", load), \
         patch.object(background_tasks.asyncio, "sleep", sleep):
        with pytest.raises(_Stop):
            await background_tasks.substrate_claims_refresh(interval_s=60.0)

    assert load.await_count == 4
    # startup wait, two backoffs, then the reload interval twice
    assert sleeps == [2, 5.0, 10.0, 60.0, 60.0]


@pytest.mark.asyncio
async def test_a_reload_only_adds_so_it_cannot_erase_a_concurrent_lookup():
    """A reload's snapshot may predate a claim a lookup just recorded; it must
    not erase it. Only a lookup that finds no claim removes a UUID."""
    verification._known_claimed.add(RESIDENT)  # recorded by a concurrent lookup
    with patch("src.db.get_db", return_value=_db(fetch=[{"agent_id": ORDINARY}])):
        await verification.load_known_substrate_claims()  # older snapshot
    assert verification.known_substrate_claimed(RESIDENT)
    assert verification.known_substrate_claimed(ORDINARY)

    with patch("src.db.get_db", return_value=_db(fetchrow=None)):
        await verification.fetch_substrate_claim(ORDINARY)  # unenrolled
    assert not verification.known_substrate_claimed(ORDINARY)

    failing = MagicMock()
    failing.acquire = MagicMock(side_effect=ConnectionError("down"))
    with patch("src.db.get_db", return_value=failing), pytest.raises(ConnectionError):
        await verification.load_known_substrate_claims()
    assert verification.known_substrate_claimed(RESIDENT)


@pytest.mark.asyncio
async def test_unreadable_signals_count_as_not_uds():
    """Only a caller shown to be UDS skips the gate; if the signals cannot be
    read, a known resident is still refused."""
    from src.mcp_handlers.identity import resolution

    verification._known_claimed.add(RESIDENT)
    with patch("src.mcp_handlers.context.get_session_signals", side_effect=RuntimeError("ctx")):
        out = await resolution._substrate_http_reject(RESIDENT, "unit")
    assert out["error"] == "substrate_check_unavailable"
