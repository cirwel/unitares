"""Tests for /v1/residents/tag_audit.

Regression guard for the 2026-04-20 class of bug where Steward ran three days
with `persistent` only (missing `autonomous`) because its onboarding path
stamped a single tag. This endpoint surfaces the gap in one cycle instead.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


def _meta(label=None, tags=None, status="active"):
    return SimpleNamespace(label=label, tags=tags or [], status=status)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("UNITARES_HTTP_API_TOKEN", raising=False)


@pytest.fixture
def audit_request():
    """Minimal Starlette Request stand-in sufficient for _check_http_auth.

    client.host must be a trusted-network address (localhost) for the auth
    short-circuit to let the request through without a bearer token.
    """
    return SimpleNamespace(
        headers={},
        query_params={},
        url=SimpleNamespace(path="/v1/residents/tag_audit"),
        client=SimpleNamespace(host="127.0.0.1"),
    )


async def _run(server_stub, audit_request):
    from src import http_api
    with patch("src.mcp_handlers.shared.lazy_mcp_server", server_stub):
        return await http_api.http_resident_tag_audit(audit_request)


@pytest.mark.asyncio
async def test_healthy_fleet_returns_empty_missing(audit_request):
    server = SimpleNamespace(agent_metadata={
        "uuid-vigil":    _meta("Vigil",    ["persistent", "autonomous", "cadence.30min"]),
        "uuid-sentinel": _meta("Sentinel", ["persistent", "autonomous", "cadence.10min"]),
        "uuid-watcher":  _meta("Watcher",  ["persistent", "autonomous"]),
        "uuid-steward":  _meta("Steward",  ["persistent", "autonomous"]),
        "uuid-lumen":    _meta("Lumen",    ["persistent", "autonomous", "embodied"]),
    })
    resp = await _run(server, audit_request)
    body = resp.body.decode()

    import json as _json
    data = _json.loads(body)
    assert data["success"] is True
    assert data["missing"] == {}
    assert data["ok_count"] == 5
    assert sorted(data["checked"]) == ["Lumen", "Sentinel", "Steward", "Vigil", "Watcher"]
    assert sorted(data["required_tags"]) == ["autonomous", "persistent"]


@pytest.mark.asyncio
async def test_single_tag_stamp_regression_surfaces(audit_request):
    """The original Steward bug: stamped 'persistent' only."""
    server = SimpleNamespace(agent_metadata={
        "uuid-steward": _meta("Steward", ["persistent"]),
        "uuid-vigil":   _meta("Vigil",   ["persistent", "autonomous"]),
    })
    resp = await _run(server, audit_request)
    import json as _json
    data = _json.loads(resp.body.decode())
    assert data["missing"] == {"Steward": ["autonomous"]}
    assert data["ok_count"] == 1


@pytest.mark.asyncio
async def test_empty_tags_reports_both_missing(audit_request):
    """Watcher's original state: empty tags → both required tags missing."""
    server = SimpleNamespace(agent_metadata={
        "uuid-watcher": _meta("Watcher", []),
    })
    resp = await _run(server, audit_request)
    import json as _json
    data = _json.loads(resp.body.decode())
    assert data["missing"] == {"Watcher": ["autonomous", "persistent"]}


@pytest.mark.asyncio
async def test_archived_residents_excluded(audit_request):
    """Archived duplicates (ghost rows) must not trigger gap alerts."""
    server = SimpleNamespace(agent_metadata={
        "uuid-vigil-active":   _meta("Vigil", ["persistent", "autonomous"], status="active"),
        "uuid-vigil-archived": _meta("Vigil", [], status="archived"),
    })
    resp = await _run(server, audit_request)
    import json as _json
    data = _json.loads(resp.body.decode())
    assert data["missing"] == {}
    assert data["checked"] == ["Vigil"]


@pytest.mark.asyncio
async def test_non_resident_agents_ignored(audit_request):
    """Ephemeral / session-bound agents aren't residents and must not appear."""
    server = SimpleNamespace(agent_metadata={
        "uuid-ephemeral": _meta("some-random-session-agent", []),
        "uuid-watcher":   _meta("Watcher", ["persistent", "autonomous"]),
    })
    resp = await _run(server, audit_request)
    import json as _json
    data = _json.loads(resp.body.decode())
    assert data["checked"] == ["Watcher"]
    assert data["missing"] == {}


@pytest.mark.asyncio
async def test_duplicate_active_resident_audited_once(audit_request):
    """If two active rows share a label, only the first is checked — match
    the label_to_meta dedup http_residents uses. Prevents a single resident
    from appearing twice in the audit result."""
    server = SimpleNamespace(agent_metadata={
        "uuid-a": _meta("Vigil", ["persistent", "autonomous"]),
        "uuid-b": _meta("Vigil", []),
    })
    resp = await _run(server, audit_request)
    import json as _json
    data = _json.loads(resp.body.decode())
    assert data["checked"].count("Vigil") == 1


@pytest.mark.asyncio
async def test_ghost_holding_the_clean_name_does_not_shadow_the_real_resident(audit_request):
    """⛔The 2026-08-26 case, live on the fleet for ~40 minutes.

    An untagged ghost held the exact label `Watcher`; the real resident had
    been renamed to `Watcher_<its own uuid8>` by the collision rename. The
    audit resolved by exact label, found the ghost, and reported a correctly
    tagged resident as missing BOTH required tags — which is precisely what
    Vigil alarms on, every cycle, with no way to distinguish a real gap from a
    shadowed one.

    `Watcher_7bf970d4` sat in that shape from 2026-04-19 to 2026-06-14.
    """
    server = SimpleNamespace(agent_metadata={
        "dc94fa70-6186-4862-aeb6-3fc9801263c8": _meta("Watcher", ["ephemeral"]),
        "7bf970d4-0000-4000-8000-000000000000": _meta(
            "Watcher_7bf970d4", ["persistent", "autonomous"]),
    })
    resp = await _run(server, audit_request)
    import json as _json
    data = _json.loads(resp.body.decode())
    assert data["missing"] == {}, "the tagged resident must win the label"
    assert data["checked"] == ["Watcher"]


@pytest.mark.asyncio
async def test_a_renamed_resident_is_still_audited_when_no_ghost_exists(audit_request):
    """A renamed resident must not vanish from the audit entirely.

    Before the fix its label was not on the roster, so it was skipped — a
    resident with a real tag gap could go unreported indefinitely.
    """
    server = SimpleNamespace(agent_metadata={
        "7bf970d4-0000-4000-8000-000000000000": _meta("Watcher_7bf970d4", ["persistent"]),
    })
    resp = await _run(server, audit_request)
    import json as _json
    data = _json.loads(resp.body.decode())
    assert data["checked"] == ["Watcher"]
    assert data["missing"] == {"Watcher": ["autonomous"]}


@pytest.mark.asyncio
async def test_unrelated_underscore_names_are_not_folded_into_a_resident(audit_request):
    """⛔`Sentinel_backup` is its own agent, not a renamed Sentinel.

    Only a suffix equal to the row's OWN uuid8 is a collision rename; matching
    any trailing `_xxxxxxxx` would merge two real agents into one audit row.
    """
    server = SimpleNamespace(agent_metadata={
        "aaaaaaaa-0000-4000-8000-000000000000": _meta("Sentinel", ["persistent", "autonomous"]),
        "bbbbbbbb-0000-4000-8000-000000000000": _meta("Sentinel_backup", []),
        "cccccccc-0000-4000-8000-000000000000": _meta("Sentinel_deadbeef", []),
    })
    resp = await _run(server, audit_request)
    import json as _json
    data = _json.loads(resp.body.decode())
    assert data["checked"] == ["Sentinel"]
    assert data["missing"] == {}
