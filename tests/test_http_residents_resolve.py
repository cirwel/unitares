"""Tests for _resolve_resident_labels precedence.

The dashboard's /v1/residents endpoint resolves which agents to surface as
residents. Precedence (operator override wins):
    1. UNITARES_RESIDENT_AGENTS env var (comma-separated labels) → source "env"
    2. agent_metadata[*].resident == True                        → source "metadata"
    3. KNOWN_RESIDENT_LABELS ∩ labels present in agent_metadata  → source "known-residents"
    4. otherwise empty                                           → source "none"

Path 3 is the auto-detect fallback: the known-resident list already exists
in src/grounding/class_indicator.KNOWN_RESIDENT_LABELS for calibration class
assignment, so the dashboard reuses it instead of requiring a duplicated env
var. We intersect with the current fleet so a fresh install doesn't advertise
residents that aren't running.
"""
import os
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.http_api import _resolve_resident_labels


def _meta(label=None, resident=False):
    return SimpleNamespace(label=label, display_name=label, resident=resident)


def _resident_meta(
    label="Lumen",
    *,
    last_update="2026-04-28T10:06:04+00:00",
    total_updates=10,
    tags=None,
    status="active",
    resident=False,
):
    return SimpleNamespace(
        label=label,
        display_name=label,
        resident=resident,
        status=status,
        last_update=last_update,
        total_updates=total_updates,
        tags=tags or ["persistent", "autonomous", "cadence.5min"],
    )


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("UNITARES_RESIDENT_AGENTS", raising=False)


class TestResolveResidentLabels:
    def test_env_var_wins(self, monkeypatch):
        monkeypatch.setenv("UNITARES_RESIDENT_AGENTS", "Alpha, Beta ,Gamma")
        server = SimpleNamespace(agent_metadata={
            "a1": _meta("Vigil"),  # would match path 3, ignored
        })
        labels, source = _resolve_resident_labels(server)
        assert labels == ["Alpha", "Beta", "Gamma"]
        assert source == "env"

    def test_metadata_resident_flag(self):
        server = SimpleNamespace(agent_metadata={
            "a1": _meta("Custom", resident=True),
            "a2": _meta("Vigil", resident=False),
        })
        labels, source = _resolve_resident_labels(server)
        assert labels == ["Custom"]
        assert source == "metadata"

    def test_known_residents_intersected_with_fleet(self):
        # Vigil, Sentinel present; Steward/Watcher/Lumen absent → only present ones surface.
        server = SimpleNamespace(agent_metadata={
            "a1": _meta("Vigil"),
            "a2": _meta("Sentinel"),
            "a3": _meta("some-random-agent"),  # not a known resident, ignored
        })
        labels, source = _resolve_resident_labels(server)
        assert set(labels) == {"Vigil", "Sentinel"}
        assert source == "known-residents"

    def test_known_residents_preserves_canonical_order(self):
        # Order should be canonical (Vigil, Sentinel, Watcher, Steward,
        # Chronicler, Lumen), not metadata-dict insertion order, so the
        # dashboard layout is stable.
        server = SimpleNamespace(agent_metadata={
            "a1": _meta("Lumen"),
            "a2": _meta("Vigil"),
            "a3": _meta("Watcher"),
        })
        labels, source = _resolve_resident_labels(server)
        assert labels == ["Vigil", "Watcher", "Lumen"]
        assert source == "known-residents"

    def test_chronicler_sorts_between_steward_and_lumen(self):
        # Chronicler is a daily scraper resident; it belongs with the other
        # background residents, before Lumen (which is the embodied agent).
        server = SimpleNamespace(agent_metadata={
            "a1": _meta("Lumen"),
            "a2": _meta("Chronicler"),
            "a3": _meta("Steward"),
        })
        labels, source = _resolve_resident_labels(server)
        assert labels == ["Steward", "Chronicler", "Lumen"]
        assert source == "known-residents"

    def test_empty_when_fleet_has_no_known_residents(self):
        # Fresh install — no residents in fleet, no env, no metadata flag.
        server = SimpleNamespace(agent_metadata={
            "a1": _meta("ad-hoc-session-agent"),
        })
        labels, source = _resolve_resident_labels(server)
        assert labels == []
        assert source == "none"

    def test_empty_when_metadata_empty(self):
        server = SimpleNamespace(agent_metadata={})
        labels, source = _resolve_resident_labels(server)
        assert labels == []
        assert source == "none"

    def test_metadata_flag_beats_known_residents(self):
        # If an operator flags an agent resident=True, that wins over the
        # auto-detect path even if known-resident labels are also present.
        server = SimpleNamespace(agent_metadata={
            "a1": _meta("Vigil"),
            "a2": _meta("Custom", resident=True),
        })
        labels, source = _resolve_resident_labels(server)
        assert labels == ["Custom"]
        assert source == "metadata"


@pytest.mark.asyncio
async def test_residents_use_metadata_last_update_when_broadcaster_event_is_stale(monkeypatch):
    """Resident strip must not lag behind the agent list when metadata is newer.

    The dashboard agent list reads ``meta.last_update``. The resident strip used
    to prefer the latest broadcaster event unconditionally, so it could show
    Lumen as 5+ minutes silent while the agent list showed a newer check-in.
    """
    from src import http_api

    request = SimpleNamespace(
        headers={},
        query_params={},
        url=SimpleNamespace(path="/v1/residents"),
        client=SimpleNamespace(host="127.0.0.1"),
    )
    server = SimpleNamespace(agent_metadata={
        "uuid-lumen": _resident_meta(last_update="2026-04-28T10:06:04+00:00"),
    })

    http_api.broadcaster_instance.event_history.clear()
    http_api.broadcaster_instance.event_history.append({
        "type": "eisv_update",
        "agent_id": "uuid-lumen",
        "timestamp": "2026-04-28T10:01:56+00:00",
        "eisv": {"E": 0.1, "I": 0.2, "S": 0.3, "V": 0.4},
        "metrics": {"coherence": 0.5, "risk_score": 0.1, "verdict": "proceed"},
    })

    with patch("src.mcp_handlers.shared.lazy_mcp_server", server), \
            patch("src.http_api._recent_writes_for_agent", AsyncMock(return_value=[])):
        resp = await http_api.http_residents(request)

    data = json.loads(resp.body.decode())
    lumen = data["residents"][0]
    assert lumen["last_checkin_at"] == "2026-04-28T10:06:04+00:00"
    assert lumen["last_checkin_source"] == "agent_metadata"
    assert lumen["latest_eisv_at"] == "2026-04-28T10:01:56+00:00"


@pytest.mark.asyncio
async def test_residents_use_broadcaster_event_when_it_is_newer(monkeypatch):
    """Keep live websocket updates authoritative when they are the newest signal."""
    from src import http_api

    request = SimpleNamespace(
        headers={},
        query_params={},
        url=SimpleNamespace(path="/v1/residents"),
        client=SimpleNamespace(host="127.0.0.1"),
    )
    server = SimpleNamespace(agent_metadata={
        "uuid-lumen": _resident_meta(last_update="2026-04-28T10:01:56+00:00"),
    })

    http_api.broadcaster_instance.event_history.clear()
    http_api.broadcaster_instance.event_history.append({
        "type": "eisv_update",
        "agent_id": "uuid-lumen",
        "timestamp": "2026-04-28T10:06:04+00:00",
        "eisv": {"E": 0.1, "I": 0.2, "S": 0.3, "V": 0.4},
        "metrics": {"coherence": 0.5, "risk_score": 0.1, "verdict": "proceed"},
    })

    with patch("src.mcp_handlers.shared.lazy_mcp_server", server), \
            patch("src.http_api._recent_writes_for_agent", AsyncMock(return_value=[])):
        resp = await http_api.http_residents(request)

    data = json.loads(resp.body.decode())
    lumen = data["residents"][0]
    assert lumen["last_checkin_at"] == "2026-04-28T10:06:04+00:00"
    assert lumen["last_checkin_source"] == "broadcaster_eisv"
    assert lumen["metadata_last_update"] == "2026-04-28T10:01:56+00:00"


@pytest.mark.asyncio
async def test_residents_duplicate_label_prefers_fresh_real_metadata(monkeypatch):
    """A stale 0-update resident fork must not shadow the active resident."""
    from src import http_api

    request = SimpleNamespace(
        headers={},
        query_params={},
        url=SimpleNamespace(path="/v1/residents"),
        client=SimpleNamespace(host="127.0.0.1"),
    )
    server = SimpleNamespace(agent_metadata={
        "sentinel-stale": _resident_meta(
            label="Sentinel",
            last_update="2026-05-01T01:58:39+00:00",
            total_updates=0,
        ),
        "sentinel-active": _resident_meta(
            label="Sentinel",
            last_update="2026-05-18T21:32:04+00:00",
            total_updates=1,
        ),
    })

    http_api.broadcaster_instance.event_history.clear()
    http_api.broadcaster_instance.event_history.append({
        "type": "eisv_update",
        "agent_id": "sentinel-active",
        "agent_name": "Sentinel",
        "timestamp": "2026-05-18T21:32:04+00:00",
        "eisv": {"E": 0.7, "I": 0.6, "S": 0.2, "V": 0.1},
        "metrics": {"coherence": 0.5, "risk_score": 0.2, "verdict": "safe"},
    })

    with patch("src.mcp_handlers.shared.lazy_mcp_server", server), \
            patch("src.http_api._recent_writes_for_agent", AsyncMock(return_value=[])):
        resp = await http_api.http_residents(request)

    data = json.loads(resp.body.decode())
    sentinel = data["residents"][0]
    assert sentinel["agent_id"] == "sentinel-active"
    assert sentinel["last_checkin_at"] == "2026-05-18T21:32:04+00:00"


@pytest.mark.asyncio
async def test_residents_rebinds_stale_metadata_to_newer_label_event(monkeypatch):
    """A live EISV event can recover a resident whose metadata label points at a stale UUID."""
    from src import http_api

    request = SimpleNamespace(
        headers={},
        query_params={},
        url=SimpleNamespace(path="/v1/residents"),
        client=SimpleNamespace(host="127.0.0.1"),
    )
    server = SimpleNamespace(agent_metadata={
        "sentinel-stale": _resident_meta(
            label="Sentinel",
            last_update="2026-05-01T01:58:39+00:00",
            total_updates=0,
        ),
    })

    http_api.broadcaster_instance.event_history.clear()
    http_api.broadcaster_instance.event_history.append({
        "type": "eisv_update",
        "agent_id": "sentinel-real",
        "agent_name": "Sentinel",
        "timestamp": "2026-05-18T21:32:04+00:00",
        "eisv": {"E": 0.74, "I": 0.68, "S": 0.24, "V": 0.08},
        "metrics": {"coherence": 0.496, "risk_score": 0.6535, "verdict": "high-risk"},
    })
    monkeypatch.setattr(
        http_api.time,
        "time",
        lambda: datetime(2026, 5, 18, 21, 33, 0, tzinfo=timezone.utc).timestamp(),
    )

    with patch("src.mcp_handlers.shared.lazy_mcp_server", server), \
            patch("src.http_api._recent_writes_for_agent", AsyncMock(return_value=[])):
        resp = await http_api.http_residents(request)

    data = json.loads(resp.body.decode())
    sentinel = data["residents"][0]
    assert sentinel["agent_id"] == "sentinel-real"
    assert sentinel["status"] == "healthy"
    assert sentinel["last_checkin_source"] == "broadcaster_eisv"
    assert sentinel["latest_eisv_at"] == "2026-05-18T21:32:04+00:00"


@pytest.mark.asyncio
async def test_residents_event_driven_flag_authoritative_for_watcher():
    """Endpoint exposes event_driven from the registry, not from a heuristic.

    Watcher's pill was rendering as silent/healthy between firings because the
    dashboard inferred event-driven from "no last_checkin_at AND >=12h threshold,"
    which Watcher doesn't satisfy (it does check in per fire). Surfacing the
    registry signal directly is the source fix.
    """
    from src import http_api

    request = SimpleNamespace(
        headers={},
        query_params={},
        url=SimpleNamespace(path="/v1/residents"),
        client=SimpleNamespace(host="127.0.0.1"),
    )
    server = SimpleNamespace(agent_metadata={
        "uuid-watcher": _resident_meta(
            label="Watcher",
            last_update="2026-04-28T10:06:04+00:00",
            tags=[],
        ),
        "uuid-vigil": _resident_meta(
            label="Vigil",
            last_update="2026-04-28T10:06:04+00:00",
            tags=[],
        ),
    })

    http_api.broadcaster_instance.event_history.clear()

    with patch("src.mcp_handlers.shared.lazy_mcp_server", server), \
            patch("src.http_api._recent_writes_for_agent", AsyncMock(return_value=[])):
        resp = await http_api.http_residents(request)

    data = json.loads(resp.body.decode())
    by_label = {r["label"]: r for r in data["residents"]}
    assert by_label["Watcher"]["event_driven"] is True
    assert by_label["Vigil"]["event_driven"] is False


@pytest.mark.asyncio
async def test_residents_durable_eisv_fallback_populates_daily_resident():
    """A daily resident's EISV must survive the broadcaster ring rotating out.

    Chronicler checks in once per 24h; its eisv_update has almost always
    rotated out of the broadcaster's ~6h in-memory ring by the time the panel
    loads, so the in-memory lookups return None and its EISV would render
    blank. The durable core.agent_state fallback fills it. Here the ring is
    empty (post-rotation), and the durable read supplies the latest check-in.
    """
    from src import http_api

    request = SimpleNamespace(
        headers={},
        query_params={},
        url=SimpleNamespace(path="/v1/residents"),
        client=SimpleNamespace(host="127.0.0.1"),
    )
    server = SimpleNamespace(agent_metadata={
        "uuid-chron": _resident_meta(
            label="Chronicler",
            last_update="2026-06-29T00:19:38+00:00",
            tags=[],
        ),
    })

    http_api.broadcaster_instance.event_history.clear()  # ring rotated out

    durable = {
        "type": "eisv_update",
        "agent_id": "uuid-chron",
        "agent_name": "Chronicler",
        "timestamp": "2026-06-29T00:19:38+00:00",
        "eisv": {"E": 0.767, "I": 0.727, "S": 0.269, "V": 0.028},
        "coherence": 0.517,
        "metrics": {"risk_score": 0.0, "verdict": "approve"},
        "decision": {"action": "approve"},
    }

    with patch("src.mcp_handlers.shared.lazy_mcp_server", server), \
            patch("src.http_api._recent_writes_for_agent", AsyncMock(return_value=[])), \
            patch("src.http_api._durable_latest_eisv_for_agent",
                  AsyncMock(return_value=durable)) as durable_mock:
        resp = await http_api.http_residents(request)

    data = json.loads(resp.body.decode())
    chron = next(r for r in data["residents"] if r["label"] == "Chronicler")
    # The core fix: EISV is populated from the durable store, not blank.
    assert chron["eisv"] == {"E": 0.767, "I": 0.727, "S": 0.269, "V": 0.028}
    assert chron["coherence"] == 0.517
    assert chron["verdict"] == "approve"
    # Fallback only fires once, and only because the ring had nothing.
    durable_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_residents_durable_eisv_not_consulted_when_ring_has_event():
    """The durable fallback is a backstop, not the primary path. When the
    broadcaster ring already has a live event for the agent, the DB read must
    be skipped — live signal wins and we don't pay a query per resident."""
    from src import http_api

    request = SimpleNamespace(
        headers={},
        query_params={},
        url=SimpleNamespace(path="/v1/residents"),
        client=SimpleNamespace(host="127.0.0.1"),
    )
    server = SimpleNamespace(agent_metadata={
        "uuid-vigil": _resident_meta(
            label="Vigil",
            last_update="2026-04-28T10:01:56+00:00",
            tags=[],
        ),
    })

    http_api.broadcaster_instance.event_history.clear()
    http_api.broadcaster_instance.event_history.append({
        "type": "eisv_update",
        "agent_id": "uuid-vigil",
        "timestamp": "2026-04-28T10:06:04+00:00",
        "eisv": {"E": 0.8, "I": 0.8, "S": 0.1, "V": 0.0},
        "metrics": {"coherence": 0.49, "risk_score": 0.0, "verdict": "proceed"},
    })

    with patch("src.mcp_handlers.shared.lazy_mcp_server", server), \
            patch("src.http_api._recent_writes_for_agent", AsyncMock(return_value=[])), \
            patch("src.http_api._durable_latest_eisv_for_agent",
                  AsyncMock(return_value=None)) as durable_mock:
        resp = await http_api.http_residents(request)

    data = json.loads(resp.body.decode())
    vigil = next(r for r in data["residents"] if r["label"] == "Vigil")
    assert vigil["last_checkin_source"] == "broadcaster_eisv"
    durable_mock.assert_not_awaited()
