"""Transitive succession-reachability FLIP (shadow -> active archival).

``UNITARES_LINEAGE_TRANSITIVE_ARCHIVAL`` gates whether transitive ancestors drive
archival. Pins:
  * default OFF  -> shadow (single-hop only; a transitive ancestor is NOT archived)
  * ON           -> expand: a deep-chain EXITED ancestor the flat pass misses IS
                    retired as lineage_succession
  * SAFETY       -> a live ancestor (lease/binding) is NEVER archived even when
                    transitively reached — the liveness guard still wins.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from unittest.mock import patch


def _meta(status="active", last_update=None, total_updates=5, tags=None,
          parent_agent_id=None, agent_uuid=None, spawn_reason=None):
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        status=status,
        last_update=(last_update or now).isoformat(),
        created_at=now.isoformat(),
        total_updates=total_updates,
        tags=tags or [],
        parent_agent_id=parent_agent_id,
        agent_uuid=agent_uuid,
        spawn_reason=spawn_reason,
    )


async def _ok(*a, **k):
    return True


async def _no_bindings(*a, **k):
    return []


async def _false(*a, **k):
    return False


async def _ret_gp1(*a, **k):
    return {"gp1"}


async def _lease_live_for_gp1(uuid):
    return uuid == "gp1"


def _set_flag(monkeypatch, on: bool):
    if on:
        monkeypatch.setenv("UNITARES_LINEAGE_TRANSITIVE_ARCHIVAL", "true")
    else:
        monkeypatch.delenv("UNITARES_LINEAGE_TRANSITIVE_ARCHIVAL", raising=False)


def test_flag_parsing(monkeypatch):
    from src.mcp_handlers.lifecycle.stuck import _transitive_archival_enabled
    monkeypatch.delenv("UNITARES_LINEAGE_TRANSITIVE_ARCHIVAL", raising=False)
    assert _transitive_archival_enabled() is False
    for v in ("true", "1", "YES", "on", " True "):
        monkeypatch.setenv("UNITARES_LINEAGE_TRANSITIVE_ARCHIVAL", v)
        assert _transitive_archival_enabled() is True
    monkeypatch.setenv("UNITARES_LINEAGE_TRANSITIVE_ARCHIVAL", "false")
    assert _transitive_archival_enabled() is False


async def _run_archive(srv_metadata, lease_fn=_false):
    from src.mcp_handlers.lifecycle.stuck import _archive_superseded_parents
    with patch("src.mcp_handlers.lifecycle.stuck.mcp_server") as srv, \
         patch("src.mcp_handlers.lifecycle.helpers._archive_one_agent") as arch, \
         patch("src.mcp_handlers.identity.process_binding.get_live_bindings",
               side_effect=_no_bindings), \
         patch("src.mcp_handlers.identity.process_binding.has_live_agent_lease",
               side_effect=lease_fn), \
         patch("src.mcp_handlers.lifecycle.lineage_reachability.reachable_ancestors",
               side_effect=_ret_gp1):
        srv.agent_metadata = srv_metadata
        srv.monitors = {}
        arch.side_effect = _ok
        await _archive_superseded_parents(datetime.now(timezone.utc))
        return [c.args[0] for c in arch.call_args_list]


def _chain_metadata():
    old = datetime.now(timezone.utc) - timedelta(minutes=45)
    recent = datetime.now(timezone.utc) - timedelta(minutes=2)
    # p1 superseded single-hop (live child c1); gp1 reachable only transitively.
    return {
        "p1": _meta(last_update=old, agent_uuid="p1"),
        "c1": _meta(last_update=recent, parent_agent_id="p1"),
        "gp1": _meta(last_update=old, agent_uuid="gp1"),
    }


@pytest.mark.asyncio
async def test_shadow_off_does_not_expand(monkeypatch):
    """Flag OFF (default): gp1 reachable transitively but NOT archived (shadow).

    gp1 is exited (no lease) so the ONLY thing sparing it is the flag being off.
    """
    _set_flag(monkeypatch, False)
    archived = await _run_archive(_chain_metadata(), lease_fn=_false)
    assert archived == ["p1"]


@pytest.mark.asyncio
async def test_flip_on_archives_exited_transitive_ancestor(monkeypatch):
    """Flag ON: an EXITED transitive ancestor (gp1) is retired alongside p1."""
    _set_flag(monkeypatch, True)
    archived = await _run_archive(_chain_metadata(), lease_fn=_false)
    assert archived == ["p1", "gp1"]
    assert "c1" not in archived  # the live child is never archived


@pytest.mark.asyncio
async def test_flip_on_live_ancestor_is_protected(monkeypatch):
    """Flag ON SAFETY: gp1 is transitively reached but holds a live lease -> the
    liveness guard wins and gp1 is NOT archived. Only the exited p1 is retired.
    This is the invariant that makes the flip safe post-lease-wire."""
    _set_flag(monkeypatch, True)
    archived = await _run_archive(_chain_metadata(), lease_fn=_lease_live_for_gp1)
    assert archived == ["p1"]
    assert "gp1" not in archived
