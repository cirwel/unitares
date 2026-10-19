"""Where GET /api/automations looks for the census snapshot.

The writer (`scripts/ops/unitares-automations`) puts `last.json` under
UNITARES_AUTOMATION_STATE_DIR, defaulting to ~/.local/state/unitares-automations.
The route must follow the same variable, keep UNITARES_AUTOMATION_CENSUS_PATH as
the explicit override that wins, and keep the unset default unchanged.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.http_routes import access
from src.http_routes.packs import resolve_handler

http_automations = resolve_handler("scripts/ops/automation_census_route.py:http_automations")


def _req():
    return SimpleNamespace(headers={})


def _write_snapshot(path, total):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema": "unitares.automation_census.v1",
        "summary": {"total": total, "by_source": {}, "by_kind": {},
                    "needs_attention": [], "warnings": []},
        "automations": [],
    }))


@pytest.fixture
def _clean_env(tmp_path, monkeypatch):
    monkeypatch.setattr(access, "_check_http_auth", lambda *a, **k: True)
    monkeypatch.delenv("UNITARES_AUTOMATION_CENSUS_PATH", raising=False)
    monkeypatch.delenv("UNITARES_AUTOMATION_STATE_DIR", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.mark.asyncio
async def test_state_dir_sets_default_snapshot(tmp_path, monkeypatch, _clean_env):
    state_dir = tmp_path / "state"
    snap = state_dir / "last.json"
    _write_snapshot(snap, total=3)
    monkeypatch.setenv("UNITARES_AUTOMATION_STATE_DIR", str(state_dir))

    resp = await http_automations(_req())
    body = json.loads(resp.body)
    assert resp.status_code == 200
    assert body["snapshot_path"] == str(snap)
    assert body["summary"]["total"] == 3


@pytest.mark.asyncio
async def test_unset_default_is_home_state_path(_clean_env):
    expected = _clean_env / ".local/state/unitares-automations/last.json"

    resp = await http_automations(_req())
    body = json.loads(resp.body)
    assert body["snapshot_path"] == str(expected)
    assert body["stale"] is True  # absent: degraded response

    _write_snapshot(expected, total=5)
    resp = await http_automations(_req())
    body = json.loads(resp.body)
    assert body["snapshot_path"] == str(expected)
    assert body["summary"]["total"] == 5


@pytest.mark.asyncio
async def test_empty_state_dir_counts_as_unset(monkeypatch, _clean_env):
    monkeypatch.setenv("UNITARES_AUTOMATION_STATE_DIR", "")

    resp = await http_automations(_req())
    body = json.loads(resp.body)
    assert body["snapshot_path"] == str(
        _clean_env / ".local/state/unitares-automations/last.json")


@pytest.mark.asyncio
async def test_census_path_beats_state_dir(tmp_path, monkeypatch, _clean_env):
    state_dir = tmp_path / "state"
    _write_snapshot(state_dir / "last.json", total=1)
    explicit = tmp_path / "explicit" / "census.json"
    _write_snapshot(explicit, total=7)
    monkeypatch.setenv("UNITARES_AUTOMATION_STATE_DIR", str(state_dir))
    monkeypatch.setenv("UNITARES_AUTOMATION_CENSUS_PATH", str(explicit))

    resp = await http_automations(_req())
    body = json.loads(resp.body)
    assert body["snapshot_path"] == str(explicit)
    assert body["summary"]["total"] == 7
