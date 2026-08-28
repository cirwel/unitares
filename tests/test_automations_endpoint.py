"""Covers the read-only /api/automations census-snapshot endpoint.

The endpoint reads the snapshot written by `unitares-automations census --write`
(path overridable via UNITARES_AUTOMATION_CENSUS_PATH) and passes it through with
freshness metadata. It must NOT shell out and must degrade gracefully when the
snapshot is missing. Auth is exercised elsewhere; patched True here so the test
focuses on the snapshot logic.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.http_routes import access
from src.http_api import http_automations


def _req():
    return SimpleNamespace(headers={})


@pytest.mark.asyncio
async def test_passthrough_with_freshness(tmp_path, monkeypatch):
    monkeypatch.setattr(access, "_check_http_auth", lambda *a, **k: True)
    snap = tmp_path / "census.json"
    snap.write_text(json.dumps({
        "schema": "unitares.automation_census.v1",
        "summary": {"total": 2, "by_source": {"launchd": 2}, "by_kind": {"dogfood": 1},
                    "needs_attention": ["a"], "warnings": []},
        "automations": [
            {"id": "a", "name": "A", "source": "launchd", "kind": "dogfood"},
            {"id": "b", "name": "B", "source": "launchd", "kind": "test"},
        ],
    }))
    monkeypatch.setenv("UNITARES_AUTOMATION_CENSUS_PATH", str(snap))

    resp = await http_automations(_req())
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["summary"]["total"] == 2
    assert len(body["automations"]) == 2
    assert body["snapshot_path"] == str(snap)
    assert body["snapshot_age_seconds"] is not None
    assert body["stale"] is False


@pytest.mark.asyncio
async def test_missing_snapshot_degrades(tmp_path, monkeypatch):
    monkeypatch.setattr(access, "_check_http_auth", lambda *a, **k: True)
    monkeypatch.setenv("UNITARES_AUTOMATION_CENSUS_PATH", str(tmp_path / "absent.json"))

    resp = await http_automations(_req())
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["automations"] == []
    assert body["summary"]["total"] == 0
    assert body["stale"] is True
    assert any("census" in w.lower() for w in body["warnings"])


@pytest.mark.asyncio
async def test_summary_view_omits_the_item_list_and_precomputes_ungated(tmp_path, monkeypatch):
    """?view=summary serves the Overview card without the census body.

    The card reads the summary block, `stale`, and a COUNT of ungated entries.
    The full census was ~206 KB of per-automation detail (228 items live on
    2026-08-28) for ~641 B of consumed fields — 99.7% discarded, on the DEFAULT
    page, every load. Cheap on loopback; the whole render cost over a tunnel.

    The ungated count is computed server-side precisely because counting is the
    only thing the caller ever did with those notes arrays.
    """
    monkeypatch.setattr(access, "_check_http_auth", lambda *a, **k: True)
    snap = tmp_path / "census.json"
    snap.write_text(json.dumps({
        "schema": "unitares.automation_census.v1",
        "summary": {"total": 3, "by_source": {"launchd": 3}, "by_kind": {"dogfood": 1},
                    "needs_attention": ["a"], "warnings": []},
        "automations": [
            {"id": "a", "notes": ["gate:ungated"]},
            {"id": "b", "notes": ["gate:reviewed"]},
            {"id": "c", "notes": ["gate:ungated"]},
        ],
    }))
    monkeypatch.setenv("UNITARES_AUTOMATION_CENSUS_PATH", str(snap))

    resp = await http_automations(SimpleNamespace(headers={}, query_params={"view": "summary"}))
    assert resp.status_code == 200
    body = json.loads(resp.body)

    assert body["view"] == "summary"
    assert body["summary"]["total"] == 3
    assert body["ungated"] == 2
    assert "stale" in body
    # The point of the view: the payload nobody reads does not ship.
    assert "automations" not in body


@pytest.mark.asyncio
async def test_default_response_is_unchanged_for_the_automations_tab(tmp_path, monkeypatch):
    """The tab renders every item, so the default shape must not shrink."""
    monkeypatch.setattr(access, "_check_http_auth", lambda *a, **k: True)
    snap = tmp_path / "census.json"
    snap.write_text(json.dumps({
        "schema": "unitares.automation_census.v1",
        "summary": {"total": 1, "by_source": {}, "by_kind": {}, "needs_attention": [], "warnings": []},
        "automations": [{"id": "a", "notes": ["gate:ungated"]}],
    }))
    monkeypatch.setenv("UNITARES_AUTOMATION_CENSUS_PATH", str(snap))

    for req in (SimpleNamespace(headers={}),
                SimpleNamespace(headers={}, query_params={}),
                SimpleNamespace(headers={}, query_params={"view": "nonsense"})):
        body = json.loads((await http_automations(req)).body)
        assert len(body["automations"]) == 1, "default shape must carry the items"
        assert "view" not in body
