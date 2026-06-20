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

from src import http_api
from src.http_api import http_automations


def _req():
    return SimpleNamespace(headers={})


@pytest.mark.asyncio
async def test_passthrough_with_freshness(tmp_path, monkeypatch):
    monkeypatch.setattr(http_api, "_check_http_auth", lambda *a, **k: True)
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
    monkeypatch.setattr(http_api, "_check_http_auth", lambda *a, **k: True)
    monkeypatch.setenv("UNITARES_AUTOMATION_CENSUS_PATH", str(tmp_path / "absent.json"))

    resp = await http_automations(_req())
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["automations"] == []
    assert body["summary"]["total"] == 0
    assert body["stale"] is True
    assert any("census" in w.lower() for w in body["warnings"])
